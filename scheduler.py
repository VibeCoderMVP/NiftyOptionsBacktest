"""
scheduler.py — Standalone auto-pilot for the Nifty weekly options paper strategy.

Runs independent of EasyTerminal. Previously the entry/exit trigger lived inside
EasyTerminal's app.py as a 30s Textual timer (_check_options_schedule) — which only
ever fires while ET's TUI is actually open. That's why the 2026-07-02 (Thursday)
entry never happened: active_options_position.json and options_ltp_cache.json both
sat frozen at the prior cycle's 2026-06-30 exit with zero activity for the rest of
that week, while ET itself only got restarted 2026-07-05. Moving the trigger to its
own always-on process (same pattern as TW's P1/P2/P3 and every strategy monitor in
this codebase) means it fires whether or not anyone has ET open.

Three jobs, checked every 30s in IST, config from D:\\Trading\\options_config.json:

1. Auto-entry — configured weekday (default Thursday) at/after entry_time_ist
   (default 15:20), no open position:
     a. run_signal(force=True) — fetches the most recent Nifty spot, computes ATM,
        resolves security IDs for ATM-50/ATM/ATM+50 (3 strikes x CE+PE = 6 legs),
        writes active_options_position.json (status=open), sends the existing
        order-slip Telegram alert (unchanged behaviour).
     b. Waits (up to 6 min, polling every 10s) for all 6 leg LTPs to arrive over
        ZMQ — tick_service auto-subscribes the resolved security IDs within ~10s
        of the file write and starts publishing OPT_<strike>_<type> ticks on port
        5555 (fallback 5557 via options_ltp_service.py's REST poll).
     c. paper_trade.log_entry() with those live LTPs, then sends a NEW Telegram
        message with the confirmed total premium collected — this is the message
        that was missing; the order-slip alert in (a) only has the *proposed*
        legs, not what they actually sold for.

2. Auto-exit — position open, expiry_date == today, at/after exit_time_ist
   (default 15:25): same LTP wait (positions have been ticking all week so this
   should resolve almost immediately), then paper_trade.log_exit() and a Telegram
   exit summary (entry/exit premium, net P&L). No SL — per the backtest, this batch
   is only ever unwound at the mandatory expiry-day close, never intraday.

3. Terminal-only heartbeat (no Telegram, no journal/position writes) — every
   _PREVIEW_HEARTBEAT_INTERVAL_S while flat (no open position, i.e. Wed through Thu
   pre-15:20): once per day, fetch that day's Nifty open (not LTP), compute what the
   3L straddle would be, resolve its 6 legs, and periodically re-print their current
   cumulative premium — a "the pipeline is alive and can actually compute this" signal,
   distinct from the file-based heartbeat.json (which only proves the process is up,
   not that spot-fetch/ATM/security-ID-resolution/premium-fetch are all still working).
   Once a real position is open (Thu 15:25 through Tue 15:25), the same periodic print
   switches to the real position's legs instead (via the live ZMQ ticks already being
   collected for the exit check) — this is a pure logging read, it never touches
   auto-entry/exit state.

Usage
-----
  uv run python scheduler.py

Writes data/scheduler_heartbeat.json (startup + every 60s) for ET's Services tab.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

_PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_DIR))

import zmq
from loguru import logger

from src import paper_trade
from src.config import DHAN_API_BASE, settings
from src.dhan_instruments import resolve_option_ids
from src.signal import (
    ACTIVE_OPTIONS_PATH, build_order_slip, compute_atm, get_nifty_open,
    get_nifty_spot, next_expiry_tuesday, run_signal, send_telegram,
)

_IST = timedelta(hours=5, minutes=30)
_CONFIG_PATH   = Path(r"D:\Trading\options_config.json")
_HEARTBEAT_PATH = _PROJECT_DIR / "data" / "scheduler_heartbeat.json"
_HEARTBEAT_INTERVAL = 60

_PRIMARY_PORT  = 5555
_FALLBACK_PORT = 5557
_TOPIC_PREFIX  = b"OPT_"

_ENTRY_LTP_TIMEOUT_S = 360   # 6 min — fresh legs, tick_service needs time to subscribe
_EXIT_LTP_TIMEOUT_S  = 120   # 2 min — legs have been ticking all week already
_LTP_POLL_INTERVAL_S = 10
_PREVIEW_HEARTBEAT_INTERVAL_S = 300   # 5 min — terminal-only premium heartbeat cadence


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + _IST


def _read_cfg() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_active_position() -> dict | None:
    if not ACTIVE_OPTIONS_PATH.exists():
        return None
    try:
        return json.loads(ACTIVE_OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fetch_option_ltps_once(security_ids: list[str]) -> dict[str, float]:
    """One-shot REST fetch of current LTPs for arbitrary NSE_FNO security IDs.

    Used for the pre-entry dry-run preview, where the legs aren't part of any real
    position yet, so tick_service has no reason to auto-subscribe them over ZMQ.
    Same endpoint/shape as options_ltp_service.py's _fetch_ltps — kept as a separate,
    smaller one-shot helper here rather than importing that long-running service.
    """
    headers = {
        "access-token": settings.dhan_access_token.get_secret_value(),
        "client-id":    settings.dhan_client_id,
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            f"{DHAN_API_BASE}/marketfeed/ltp",
            json={"NSE_FNO": [int(sid) for sid in security_ids]},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        fno_data = data.get("data", {}).get("NSE_FNO", {})
        result = {}
        for sid, entry in fno_data.items():
            ltp = entry.get("last_price") or entry.get("LTP")
            if ltp is not None:
                result[str(sid)] = float(ltp)
        return result
    except Exception as exc:
        logger.warning("Dry-run LTP fetch failed: {}", exc)
        return {}


def _write_heartbeat(started_at: str) -> None:
    try:
        payload = json.dumps({
            "last_seen":  _ist_now().isoformat(timespec="seconds"),
            "started_at": started_at,
        })
        tmp = _HEARTBEAT_PATH.with_suffix(".json.tmp")
        _HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(_HEARTBEAT_PATH)
    except Exception:
        pass


class LtpCollector:
    """Background ZMQ SUB thread collecting OPT_ ticks into a dict for the main loop."""

    def __init__(self) -> None:
        self._ltps: dict[tuple[int, str], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ltp-collector")
        self._thread.start()

    def _run(self) -> None:
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://127.0.0.1:{_PRIMARY_PORT}")
        sub.connect(f"tcp://127.0.0.1:{_FALLBACK_PORT}")
        sub.setsockopt(zmq.SUBSCRIBE, _TOPIC_PREFIX)
        try:
            while not self._stop.is_set():
                if not sub.poll(timeout=500):
                    continue
                try:
                    _, raw = sub.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue
                try:
                    data = json.loads(raw.decode())
                    key = (int(data["strike"]), str(data["option_type"]))
                    ltp = float(data["ltp"])
                except (KeyError, ValueError, json.JSONDecodeError):
                    continue
                with self._lock:
                    self._ltps[key] = ltp
        finally:
            sub.close()
            ctx.term()

    def stop(self) -> None:
        self._stop.set()

    def get(self, key: tuple[int, str]) -> float | None:
        with self._lock:
            return self._ltps.get(key)

    def wait_for(self, keys: list[tuple[int, str]], timeout_s: float) -> list[float | None]:
        """Poll every _LTP_POLL_INTERVAL_S until all keys have an LTP or timeout_s elapses.
        Returns whatever's collected (may include None entries) at the end."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            vals = [self.get(k) for k in keys]
            if all(v is not None for v in vals):
                return vals
            ready = sum(1 for v in vals if v is not None)
            logger.info("Waiting for leg LTPs: {}/{} received...", ready, len(keys))
            time.sleep(_LTP_POLL_INTERVAL_S)
        return [self.get(k) for k in keys]


class PreviewState:
    """Today's dry-run 3L straddle (computed once/day from the session open) while flat.
    Never written to disk/journal/Telegram — terminal-only, purely a liveness signal."""

    def __init__(self) -> None:
        self.computed_date: date | None = None
        self.legs: list[tuple[int, str]] = []          # (strike, option_type) x 6
        self.security_ids: list[str] = []               # same order as legs
        self.open_spot: float | None = None
        self.atm: int | None = None


def _refresh_daily_preview(preview: PreviewState) -> None:
    """Once per day, while flat: fetch today's Nifty open, compute the would-be 3L
    straddle (ATM-50/ATM/ATM+50 x CE+PE) and resolve its security IDs, so the periodic
    heartbeat below has something concrete to re-price. Terminal-only — no side effects
    on active_options_position.json / options_journal.jsonl / Telegram."""
    today = _ist_now().date()
    if preview.computed_date == today:
        return
    if today.weekday() >= 5:   # Sat/Sun — market shut, don't retry-storm a failing fetch all weekend
        return

    open_px = get_nifty_open()
    used_fallback = False
    if open_px is None:
        open_px = get_nifty_spot()
        used_fallback = True
    if open_px is None:
        logger.warning("DRY-RUN PREVIEW: could not fetch Nifty open or spot — retrying next cycle")
        return

    atm = compute_atm(open_px)
    legs_ct = build_order_slip(atm)   # [{"strike":.., "type":..}, ...] x 6, ATM-50/ATM/ATM+50 order
    expiry = next_expiry_tuesday(today)
    try:
        resolved = resolve_option_ids([atm - 50, atm, atm + 50], expiry)
    except Exception as exc:
        logger.warning("DRY-RUN PREVIEW: could not resolve option security IDs: {}", exc)
        return

    preview.computed_date = today
    preview.open_spot     = open_px
    preview.atm           = atm
    preview.legs          = [(int(c["strike"]), str(c["option_type"])) for c in resolved]
    preview.security_ids  = [str(c["security_id"]) for c in resolved]

    src_label = "spot LTP (open unavailable)" if used_fallback else "session open"
    logger.info(
        "DRY-RUN PREVIEW: today's would-be straddle from {} {:.2f} -> ATM {} | legs: {}",
        src_label, open_px, atm,
        ", ".join(f"{s}{t}" for s, t in preview.legs),
    )


def _log_premium_heartbeat(preview: PreviewState) -> None:
    """Terminal-only periodic print — proof the pipeline can still fetch real premiums,
    not just that the process is running. Position legs take priority over the dry-run
    preview whenever a real position is open."""
    pos = _read_active_position()
    if pos and pos.get("status") == "open":
        contracts = pos.get("contracts", [])
        expected  = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
        sec_ids   = [c["security_id"] for c in contracts if c.get("security_id")]
        ltps      = _fetch_option_ltps_once(sec_ids) if sec_ids else {}
        by_sid    = {c["security_id"]: (int(c["strike"]), str(c["option_type"])) for c in contracts}
        premiums  = [ltps.get(sid) for sid in sec_ids]
        if premiums and all(v is not None for v in premiums):
            total = sum(premiums)
            logger.info(
                "POSITION HEARTBEAT: open straddle ATM={} expiry={} | current premium {:.2f} pts "
                "(entry was {:.2f} pts)",
                pos.get("atm"), pos.get("expiry_date"), total, sum(c.get("entry_ltp") or 0 for c in contracts),
            )
        else:
            logger.info("POSITION HEARTBEAT: open straddle ATM={} — premium fetch incomplete this cycle",
                        pos.get("atm"))
        return

    if preview.computed_date != _ist_now().date() or not preview.security_ids:
        return  # no preview computed yet today (shouldn't normally happen — refreshed first)

    ltps = _fetch_option_ltps_once(preview.security_ids)
    premiums = [ltps.get(sid) for sid in preview.security_ids]
    if not premiums or any(v is None for v in premiums):
        logger.info("DRY-RUN HEARTBEAT: ATM {} — premium fetch incomplete this cycle", preview.atm)
        return
    total = sum(premiums)
    leg_str = ", ".join(f"{s}{t}@{p:.2f}" for (s, t), p in zip(preview.legs, premiums))
    logger.info(
        "DRY-RUN HEARTBEAT [NOT A TRADE]: ATM {} (from open {:.2f}) | current cumulative "
        "straddle premium {:.2f} pts | {}",
        preview.atm, preview.open_spot, total, leg_str,
    )


def _try_auto_entry(cfg: dict, collector: LtpCollector, fired: dict[str, date]) -> None:
    if not cfg.get("auto_entry"):
        return
    now = _ist_now()
    try:
        eh, em = (int(x) for x in cfg.get("entry_time_ist", "15:20").split(":"))
    except Exception:
        eh, em = 15, 20
    entry_weekday = int(cfg.get("entry_weekday", 3))

    if now.weekday() != entry_weekday:
        return
    if not (now.hour > eh or (now.hour == eh and now.minute >= em)):
        return
    if fired.get("entry") == now.date():
        return

    pos = _read_active_position()
    if pos and pos.get("status") == "open":
        return  # already have an open position — nothing to do

    fired["entry"] = now.date()  # mark attempted regardless of outcome — never retry-storm same day
    logger.info("AUTO-ENTRY triggered at {} IST", now.strftime("%H:%M:%S"))

    try:
        run_signal(force=True)
    except Exception as exc:
        logger.error("AUTO-ENTRY: run_signal() failed: {}", exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS - AUTO-ENTRY FAILED\nrun_signal() raised: {exc}")
        return

    pos = _read_active_position()
    if not pos or pos.get("status") != "open":
        logger.warning("AUTO-ENTRY: active_options_position.json not open after run_signal — aborting")
        send_telegram(
            "NIFTY WEEKLY OPTIONS - AUTO-ENTRY FAILED\n"
            "active_options_position.json was not left in an open state after signal — "
            "check Dhan API / security ID resolution."
        )
        return

    contracts = pos.get("contracts", [])
    expected = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
    if len(expected) != 6:
        logger.warning("AUTO-ENTRY: expected 6 contracts, got {} — aborting", len(expected))
        send_telegram(
            f"NIFTY WEEKLY OPTIONS - AUTO-ENTRY FAILED\n"
            f"Expected 6 resolved contracts, got {len(expected)}. No paper position opened."
        )
        return

    ltps = collector.wait_for(expected, _ENTRY_LTP_TIMEOUT_S)
    if any(v is None for v in ltps):
        ready = sum(1 for v in ltps if v is not None)
        logger.warning("AUTO-ENTRY: only {}/6 leg LTPs arrived within {}s — aborting",
                        ready, _ENTRY_LTP_TIMEOUT_S)
        send_telegram(
            f"NIFTY WEEKLY OPTIONS - AUTO-ENTRY FAILED\n"
            f"Only {ready}/6 leg LTPs received within {_ENTRY_LTP_TIMEOUT_S // 60} minutes "
            f"of signal. No paper position opened — check tick_service / P2 status."
        )
        return

    atm  = int(pos["atm"])
    spot = float(pos["entry_spot"])
    try:
        record = paper_trade.log_entry(atm=atm, legs_ltps=ltps, entry_spot=spot)
    except Exception as exc:
        logger.error("AUTO-ENTRY: log_entry() failed: {}", exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS - AUTO-ENTRY FAILED\nlog_entry() raised: {exc}")
        return

    total_premium = sum(ltps)
    lot_size = record.get("lot_size", 75)
    rs_collected = total_premium * lot_size

    leg_lines = [f"  {expected[i][0]:<8} {expected[i][1]:<3} @ {ltps[i]:.2f}" for i in range(6)]
    msg = "\n".join([
        "NIFTY WEEKLY OPTIONS - 3L STRADDLE OPENED",
        f"Entry : {now.strftime('%Y-%m-%d %H:%M')} IST",
        f"Expiry: {record['expiry_date']}",
        f"Spot  : {spot:.2f}  |  ATM: {atm}",
        "",
        "Legs sold (SELL 1 lot each):",
        *leg_lines,
        "",
        f"Total premium collected: {total_premium:.2f} pts = Rs {rs_collected:,.0f}",
    ])
    send_telegram(msg)
    logger.info("AUTO-ENTRY complete | ATM={} premium={:.2f} pts Rs={:.0f}",
                atm, total_premium, rs_collected)


def _try_auto_exit(cfg: dict, collector: LtpCollector, fired: dict[str, date]) -> None:
    if not cfg.get("auto_exit"):
        return
    now = _ist_now()
    try:
        xh, xm = (int(x) for x in cfg.get("exit_time_ist", "15:25").split(":"))
    except Exception:
        xh, xm = 15, 25

    if not (now.hour > xh or (now.hour == xh and now.minute >= xm)):
        return
    if fired.get("exit") == now.date():
        return

    pos = _read_active_position()
    if not pos or pos.get("status") != "open":
        return
    if pos.get("expiry_date") != str(now.date()):
        return

    fired["exit"] = now.date()
    logger.info("AUTO-EXIT triggered at {} IST", now.strftime("%H:%M:%S"))

    contracts = pos.get("contracts", [])
    expected = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
    ltps_raw = collector.wait_for(expected, _EXIT_LTP_TIMEOUT_S)
    # A leg with no LTP at all by the deadline (e.g. deep OTM, zero liquidity) is treated
    # as worthless rather than blocking the mandatory expiry-day close — matches ET's own
    # force-close convention for a missing leg.
    ltps = [v if v is not None else 0.05 for v in ltps_raw]
    missing = sum(1 for v in ltps_raw if v is None)
    if missing:
        logger.warning("AUTO-EXIT: {}/6 legs had no LTP by deadline — using Rs 0.05 fallback", missing)

    try:
        rec = paper_trade.log_exit(ltps)
    except Exception as exc:
        logger.error("AUTO-EXIT: log_exit() failed: {}", exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS - AUTO-EXIT FAILED\nlog_exit() raised: {exc}")
        return

    net_pnl = rec.get("net_pnl_rs")
    outcome = rec.get("outcome", "?")
    leg_lines = [f"  {expected[i][0]:<8} {expected[i][1]:<3} @ {ltps[i]:.2f}" for i in range(6)]
    msg = "\n".join([
        "NIFTY WEEKLY OPTIONS - 3L STRADDLE CLOSED (expiry day)",
        f"Exit: {now.strftime('%Y-%m-%d %H:%M')} IST",
        "",
        "Legs bought back:",
        *leg_lines,
        "",
        f"Entry premium : {rec.get('total_entry_premium', 0):.2f} pts",
        f"Exit premium  : {rec.get('total_exit_premium', 0):.2f} pts",
        f"Net P&L       : Rs {net_pnl:+,.0f}  ({outcome})" if net_pnl is not None else "Net P&L: n/a",
    ])
    send_telegram(msg)
    logger.info("AUTO-EXIT complete | outcome={} net_pnl_rs={}", outcome, net_pnl)


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
               level="INFO")
    logger.add(_PROJECT_DIR / "data" / "scheduler.log", rotation="5 MB", retention="14 days", level="DEBUG")

    started_at = _ist_now().isoformat(timespec="seconds")
    _write_heartbeat(started_at)
    logger.info("Options scheduler started")

    collector = LtpCollector()
    fired: dict[str, date] = {}
    preview = PreviewState()
    last_hb = time.monotonic()
    last_preview_hb = 0.0

    try:
        while True:
            cfg = _read_cfg()
            if cfg:
                _try_auto_entry(cfg, collector, fired)
                _try_auto_exit(cfg, collector, fired)

            pos = _read_active_position()
            if not (pos and pos.get("status") == "open"):
                _refresh_daily_preview(preview)

            now_mono = time.monotonic()
            if now_mono - last_preview_hb >= _PREVIEW_HEARTBEAT_INTERVAL_S:
                _log_premium_heartbeat(preview)
                last_preview_hb = now_mono

            if now_mono - last_hb >= _HEARTBEAT_INTERVAL:
                _write_heartbeat(started_at)
                last_hb = now_mono

            time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        logger.info("Options scheduler stopped")


if __name__ == "__main__":
    main()
