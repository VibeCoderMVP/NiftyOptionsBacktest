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

Four jobs, checked every 30s in IST, config from D:\\Trading\\options_config.json:

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

4. Strike-band monitor (added 2026-07-08, no Telegram, no journal/position writes) —
   runs every day unconditionally, NOT gated on being flat like (3). Once per day, from
   Nifty's session open, computes ATM and subscribes a 13-strike band (ATM +/- 300,
   step 50 = 26 legs CE+PE) via request_subscription(), requester name
   "nifty_strike_band_monitor". Pure visibility: gives premium coverage across a wide
   band regardless of position state, and wide enough (+/-6 strikes) that a mid-day
   scheduler restart (which recomputes "session open" from spot-at-restart-time, not
   the true 09:15 open) still lands inside the covered range.

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

_PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_DIR))

import zmq
from loguru import logger

from src import paper_trade
from src.config import LADDER_VARIANTS, lot_size_for_date, settings
from src.dhan_instruments import resolve_option_ids
from src.signal import (
    ACTIVE_OPTIONS_PATH, compute_atm, get_nifty_open, get_nifty_spot,
    listed_expiry, run_signal, send_telegram,
)
from trading_core.journal import append_event, build_event_record
from trading_core.market_calendar import is_fo_trading_day
from trading_core.market_structure import FO_TRADING_DECISION_CUTOFF
from trading_core.paths import fleet_path
from trading_core.session_shutdown import NSE_SESSION_END, should_end_session
from trading_core.subscription_registry import request_subscription

# entry/exit flow log for LiveSignalDashboard's Flow tab (see D:\Trading\docs\
# SIGNAL_EVENT_LOG_AND_LIVE_DASHBOARD_DESIGN.md) -- sole writer is this process (shared by
# both ladder variants). Only the two REAL success paths (confirmed fill, confirmed
# buyback) are wired -- the various "AUTO-ENTRY/EXIT FAILED" alerts above each call site
# aren't real armed/entry/exit events (nothing filled), and run_signal()'s own proposed-
# order-slip alert (src/signal.py) is informational only, not a confirmed fill -- neither
# is forced into the 5-event vocabulary. No "armed" concept exists here at all: this
# project is calendar-scheduler-driven (Thursday/Tuesday wall-clock checks), not tick/
# bar-close-driven, so there's no resting/pending state between signal and fill.
_SIGNAL_EVENTS_PATH = fleet_path("NiftyOptionsBacktest", "data", "signal_events.jsonl")
_VID_TO_LABEL = {"3L-50": "OPT-3L50", "3L-100": "OPT-3L100"}


def _log_event(event: str, vid: str, **fields) -> None:
    label = _VID_TO_LABEL.get(vid, f"OPT-{vid}")
    record = build_event_record(event=event, symbol="NIFTY", strategy=label, tf=None, **fields)
    append_event(_SIGNAL_EVENTS_PATH, record)

_IST = timedelta(hours=5, minutes=30)
_CONFIG_PATH   = fleet_path("options_config.json")
_HEARTBEAT_PATH = _PROJECT_DIR / "data" / "scheduler_heartbeat.json"
_HEARTBEAT_INTERVAL = 60

_PRIMARY_PORT  = 5555
_FALLBACK_PORT = 5557
_TOPIC_PREFIX  = b"OPT_"

_ENTRY_LTP_TIMEOUT_S = 360   # 6 min — fresh legs, tick_service needs time to subscribe
_EXIT_LTP_TIMEOUT_S  = 120   # 2 min — legs have been ticking all week already
_LTP_POLL_INTERVAL_S = 10
_PREVIEW_HEARTBEAT_INTERVAL_S = 300   # 5 min — terminal-only premium heartbeat cadence
_PREVIEW_RETRY_COOLDOWN_S = 300   # 5 min — throttle retries after a failed daily-preview fetch;
                                  # without this, a single failure retried every 30s main-loop
                                  # tick all day, hammering Dhan's LTP/OHLC endpoints and
                                  # plausibly contributing to the 429 rate-limit seen 2026-07-07


def _ist_now() -> datetime:
    """Correct IST wall-clock VALUE, but must return it as a NAIVE datetime --
    every other service's heartbeat in this codebase writes naive local-IST
    timestamps (see TradingWebSockets/CLAUDE.md's _age_seconds() gotcha), and
    ET's own _age_seconds() branches on tzinfo: if present, it does
    `datetime.now(timezone.utc) - dt`, which silently went NEGATIVE here
    (found live 2026-07-06) since `datetime.now(timezone.utc) + _IST` keeps
    tzinfo=utc on a value that's actually ~5.5h ahead of real UTC -- ET read
    that negative age as "very fresh" and showed OK/GREEN even though the
    process had been dead for hours. Stripping tzinfo makes ET take the
    naive-local branch instead, comparing IST-to-IST correctly."""
    return (datetime.now(timezone.utc) + _IST).replace(tzinfo=None)


def _read_cfg() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_active_position(path: Path = ACTIVE_OPTIONS_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _set_position_status(path: Path, status: str, reason: str) -> None:
    """Move a position file out of "open" (every consumer -- tick_service, the REST LTP
    service, ET, this scheduler -- treats anything but "open" as inactive)."""
    pos = _read_active_position(path)
    if not pos:
        return
    pos["status"] = status
    pos["status_reason"] = reason
    pos["updated_at"] = _ist_now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(pos, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    logger.warning("Position file {} marked {} ({})", path.name, status, reason)


def _retire_stale_position(variant: dict) -> None:
    """An "open" position whose expiry_date is already past can never be exited by
    _try_auto_exit (it only fires ON expiry day) and blocks every future auto-entry.
    That is exactly how the aborted 2026-10-15 entry (expiry 2026-10-20, ID-less) froze
    both ladders for 3 weeks with nothing but a heartbeat line to show for it. Retire it
    loudly so the next entry day proceeds."""
    path = variant["active_path"]
    pos = _read_active_position(path)
    if not pos or pos.get("status") != "open":
        return
    try:
        expiry = date.fromisoformat(str(pos.get("expiry_date")))
    except ValueError:
        return
    today = _ist_now().date()
    if expiry >= today:
        return
    vid = variant["id"]
    had_fill = any(c.get("entry_ltp") is not None for c in pos.get("contracts", []))
    _set_position_status(path, "expired_stale", f"still open on {today}, expiry was {expiry}")
    send_telegram(
        f"NIFTY WEEKLY OPTIONS [{vid}] - STALE POSITION RETIRED\n"
        f"Position file was still 'open' on {today} but expiry was {expiry} "
        f"(entry {pos.get('entry_date')}, ATM {pos.get('atm')}). Marked expired_stale so "
        "auto-entry is unblocked."
        + ("\nIt HAD entry fills -- its journal record is still open; close it manually."
           if had_fill else "\nNo entry fills were recorded -- nothing to close in the journal.")
    )


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


class NiftySpotCollector:
    """Background ZMQ SUB thread tracking the live NIFTY index tick (topic b"NIFTY",
    published by TW's tick_service.py — added 2026-07-07). Replaces this file's prior
    direct REST calls to Dhan's /marketfeed/ltp|ohlc for Nifty spot/open: those calls
    ran on their own independent polling loop with no backoff on failure, contributing
    to (and repeatedly re-triggering) a 429 rate-limit seen 2026-07-06/07. Reusing
    tick_service's already-open, already-authenticated WebSocket connection means no
    additional Dhan API call is made for this purpose at all.

    Tracks the first tick seen each IST day as a proxy for the session open — close
    enough for this feature's actual purpose (a rough dry-run preview), not used for
    anything that touches real money."""

    def __init__(self) -> None:
        self._ltp: float | None = None
        self._session_open: float | None = None
        self._today: date | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="nifty-spot-collector")
        self._thread.start()

    def _run(self) -> None:
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://127.0.0.1:{_PRIMARY_PORT}")
        sub.setsockopt(zmq.SUBSCRIBE, b"NIFTY")
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
                    ltp = float(data["ltp"])
                except (KeyError, ValueError, json.JSONDecodeError):
                    continue
                today = _ist_now().date()
                with self._lock:
                    if self._today != today:
                        self._today = today
                        self._session_open = ltp
                    self._ltp = ltp
        finally:
            sub.close()
            ctx.term()

    def stop(self) -> None:
        self._stop.set()

    def spot(self) -> float | None:
        with self._lock:
            return self._ltp

    def session_open(self) -> float | None:
        with self._lock:
            return self._session_open


_STRIKE_BAND_OFFSETS = list(range(-300, 301, 50))  # 13 strikes: ATM-300..ATM+300 step 50


class StrikeBandState:
    """Today's 13-strike premium-monitoring band (ATM +/- 300, step 50), centered on
    Nifty's session open. Purely additive visibility — runs every day regardless of
    whether a real position is open, unlike PreviewState below (which only matters
    while flat, for the entry-decision dry-run). No journal/Telegram/position writes;
    subscribes legs onto P2's live feed via the same registry so ET's Options tab and
    this file's own LtpCollector can read premiums for the whole band, not just the
    6 legs of an actual position."""

    def __init__(self) -> None:
        self.computed_date: date | None = None
        self.atm: int | None = None
        self.strikes: list[int] = []
        self.last_attempt_mono: float | None = None


def _refresh_strike_band(band: StrikeBandState, nifty_spot: NiftySpotCollector) -> None:
    """Once per day: resolve and subscribe a 13-strike band centered on Nifty's session
    open, independent of position state. Same fetch/fallback/retry-cooldown shape as
    _refresh_daily_preview (ZMQ session open preferred, REST fallback, 5-min cooldown
    on failure) — kept as a separate function/state rather than merged into
    PreviewState so the entry-decision preview logic above is untouched by this."""
    today = _ist_now().date()
    if band.computed_date == today:
        return
    if today.weekday() >= 5:  # Sat/Sun — market shut
        return

    now_mono = time.monotonic()
    if (
        band.last_attempt_mono is not None
        and now_mono - band.last_attempt_mono < _PREVIEW_RETRY_COOLDOWN_S
    ):
        return
    band.last_attempt_mono = now_mono

    open_px = nifty_spot.session_open() or nifty_spot.spot()
    if open_px is None:
        open_px = get_nifty_open() or get_nifty_spot()
    if open_px is None:
        logger.warning(
            "STRIKE BAND: could not fetch Nifty open/spot from ZMQ or REST — retrying in {}s",
            _PREVIEW_RETRY_COOLDOWN_S,
        )
        return

    atm = compute_atm(open_px)
    strikes = [atm + off for off in _STRIKE_BAND_OFFSETS]
    expiry = listed_expiry(today, include_today=True)
    try:
        resolved = resolve_option_ids(strikes, expiry)
    except Exception as exc:
        logger.warning("STRIKE BAND: could not resolve option security IDs: {}", exc)
        return

    band.computed_date = today
    band.atm = atm
    band.strikes = strikes

    try:
        request_subscription("nifty_strike_band_monitor", [
            {
                "exchange_segment": "NSE_FNO",
                "security_id": str(c["security_id"]),
                "topic": f"OPT_{c['strike']}_{c['option_type']}",
                "strike": int(c["strike"]),
                "option_type": str(c["option_type"]),
            }
            for c in resolved
        ])
    except Exception as exc:
        logger.warning("STRIKE BAND: could not request live subscription: {}", exc)
        return

    logger.info(
        "STRIKE BAND: subscribed {} strikes ({} legs) centered on ATM {} (open {:.2f}): {}",
        len(strikes), len(resolved), atm, open_px, strikes,
    )


class PreviewState:
    """Today's dry-run 3L straddle (computed once/day from the session open) while flat.
    Never written to disk/journal/Telegram — terminal-only, purely a liveness signal."""

    def __init__(self) -> None:
        self.computed_date: date | None = None
        self.legs: list[tuple[int, str]] = []          # (strike, option_type) x 6
        self.security_ids: list[str] = []               # same order as legs
        self.open_spot: float | None = None
        self.atm: int | None = None
        self.last_attempt_mono: float | None = None    # throttle retries after a failure


def _refresh_daily_preview(preview: PreviewState, nifty_spot: NiftySpotCollector) -> None:
    """Once per day, while flat: get today's Nifty open, compute the would-be 3L
    straddle (ATM-50/ATM/ATM+50 x CE+PE) and resolve its security IDs, so the periodic
    heartbeat below has something concrete to re-price. Terminal-only — no side effects
    on active_options_position.json / options_journal.jsonl / Telegram.

    Prefers the live ZMQ NIFTY tick (nifty_spot, fed by TW's tick_service.py) over Dhan
    REST — added 2026-07-07 after the REST-only version's own retry logic contributed
    to a 429 rate-limit. REST is kept only as a fallback for when P2/ZMQ has no data yet
    (e.g. this process started before P2, or P2 is down) — this file shouldn't have a
    hard dependency on P2."""
    today = _ist_now().date()
    if preview.computed_date == today:
        return
    if today.weekday() >= 5:   # Sat/Sun — market shut, don't retry-storm a failing fetch all weekend
        return

    now_mono = time.monotonic()
    if (
        preview.last_attempt_mono is not None
        and now_mono - preview.last_attempt_mono < _PREVIEW_RETRY_COOLDOWN_S
    ):
        return   # failed recently — don't hammer Dhan every 30s main-loop tick
    preview.last_attempt_mono = now_mono

    open_px = nifty_spot.session_open()
    src = "zmq session open"
    if open_px is None:
        open_px = nifty_spot.spot()
        src = "zmq spot LTP (open unavailable)"
    if open_px is None:
        # ZMQ has nothing yet (P2 not up, or no tick received this session) — REST fallback
        open_px = get_nifty_open()
        src = "REST session open (zmq unavailable)"
        if open_px is None:
            open_px = get_nifty_spot()
            src = "REST spot LTP (zmq + open unavailable)"
    if open_px is None:
        logger.warning(
            "DRY-RUN PREVIEW: could not fetch Nifty open or spot from ZMQ or REST "
            "— retrying in {}s",
            _PREVIEW_RETRY_COOLDOWN_S,
        )
        return

    atm = compute_atm(open_px)
    expiry = listed_expiry(today, include_today=True)
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

    # Subscribe these preview-only legs on P2's live feed via the generic dynamic-
    # subscription registry (added 2026-07-07) — same OPT_{strike}_{type} topic
    # convention real positions use, so the existing LtpCollector (already subscribed
    # to that prefix) picks them up automatically. Previously these legs were fetched
    # via REST (_fetch_option_ltps_once) on every heartbeat cycle since nothing ever
    # subscribed them — fixed same day this was reported as still not working.
    try:
        request_subscription("nifty_options_preview", [
            {
                "exchange_segment": "NSE_FNO",
                "security_id": str(c["security_id"]),
                "topic": f"OPT_{c['strike']}_{c['option_type']}",
                "strike": int(c["strike"]),
                "option_type": str(c["option_type"]),
            }
            for c in resolved
        ])
    except Exception as exc:
        logger.warning("DRY-RUN PREVIEW: could not request live subscription for legs: {}", exc)

    logger.info(
        "DRY-RUN PREVIEW: today's would-be straddle from {} {:.2f} -> ATM {} | legs: {}",
        src, open_px, atm,
        ", ".join(f"{s}{t}" for s, t in preview.legs),
    )


def _log_premium_heartbeat(preview: PreviewState, collector: LtpCollector) -> None:
    """Terminal-only periodic print — proof the pipeline can still fetch real premiums,
    not just that the process is running. Position legs take priority over the dry-run
    preview whenever a real position is open.

    Reads both branches from `collector` (ZMQ, topic prefix OPT_) instead of REST — fixed
    2026-07-07. The open-position branch previously called _fetch_option_ltps_once() (REST)
    even though those exact legs are already flowing on ZMQ via the pre-existing
    active_options_position.json auto-subscribe mechanism (the same feed _try_auto_entry/
    _try_auto_exit already read via collector.wait_for()) — a pure redundant REST call for
    data already on the wire. The preview branch now requests its legs via
    trading_core.subscription_registry.request_subscription() (see _refresh_daily_preview)
    using the same OPT_{strike}_{type} topic convention real positions use, so this same
    collector picks them up automatically with no new subscriber needed."""
    pos = _read_active_position()
    if pos and pos.get("status") == "open":
        contracts = pos.get("contracts", [])
        expected  = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
        premiums  = [collector.get(k) for k in expected]
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

    if preview.computed_date != _ist_now().date() or not preview.legs:
        return  # no preview computed yet today (shouldn't normally happen — refreshed first)

    premiums = [collector.get(k) for k in preview.legs]
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


def _variant_entry_enabled(cfg: dict, variant: dict) -> bool:
    """3L-50 is gated by the top-level auto_entry flag (unchanged). 3L-100 is gated
    independently by ladder_100.enabled, so it can keep paper-testing even while
    live 3L-50 auto-entry is paused, and vice versa."""
    if variant["id"] == "3L-50":
        return bool(cfg.get("auto_entry"))
    return bool(cfg.get("ladder_100", {}).get("enabled", False))


def _variant_exit_enabled(cfg: dict, variant: dict) -> bool:
    if variant["id"] == "3L-50":
        return bool(cfg.get("auto_exit"))
    return bool(cfg.get("ladder_100", {}).get("enabled", False))


def _journal_path_for(variant: dict) -> Path:
    return settings.data_dir / variant["journal_filename"]


def _subscribe_variant_legs(variant: dict, pos: dict) -> None:
    """Request a dedicated live subscription for this variant's own 6 legs.

    3L-50's legs are already auto-subscribed by tick_service watching
    active_options_position.json's mtime. That file-watch mechanism only covers
    that one hardcoded path — a second variant's own active_path is invisible to
    it. Without this, 3L-100 would depend entirely on the daily strike-band
    monitor (ATM+/-300) for LTPs, with no independent subscription of its own; if
    that job ever died, 3L-100 would go dark silently. Requesting it directly
    here (same mechanism the strike-band monitor already uses) makes 3L-100's
    feed independent of that job's health. Harmless/idempotent for 3L-50 too."""
    contracts = pos.get("contracts", [])
    if not contracts:
        return
    requester = f"nifty_options_{variant['id'].replace('-', '_').lower()}"
    try:
        request_subscription(requester, [
            {
                "exchange_segment": c.get("exchange_segment", "NSE_FNO"),
                "security_id": str(c["security_id"]),
                "topic": f"OPT_{c['strike']}_{c['option_type']}",
                "strike": int(c["strike"]),
                "option_type": str(c["option_type"]),
            }
            for c in contracts
        ])
    except Exception as exc:
        logger.warning("{}: could not request dedicated leg subscription: {}", variant["id"], exc)


_HOLIDAY_SHIFT_TAG = "holiday_shift_next_day"
_calendar_gap_warned: set[date] = set()


def _entry_shift_today(today: date, entry_weekday: int) -> str | None | bool:
    """Is today an entry day? None = normal entry day, _HOLIDAY_SHIFT_TAG = the day after
    an entry day that was an NSE holiday (Fri for a Thursday holiday), False = no entry.

    Holidays only: a Thursday missed for any other reason (machine off, crash) is NOT made
    up on Friday -- a catch-up there would hide an outage. An entry-weekday holiday no
    longer fires at all (it used to run at 15:10 off the last close and abort on 0/6 LTPs).
    A year with no holiday list in trading_core.market_calendar never shifts (warned once)."""
    if today.weekday() == entry_weekday:
        return False if is_fo_trading_day(today) is False else None
    prev = today - timedelta(days=1)
    if prev.weekday() != entry_weekday or is_fo_trading_day(today) is False:
        return False
    prev_trading = is_fo_trading_day(prev)
    if prev_trading is None:
        if prev not in _calendar_gap_warned:
            _calendar_gap_warned.add(prev)
            logger.warning(
                "No NSE holiday list for {} in trading_core.market_calendar -- cannot tell if "
                "{} was a holiday; no holiday-shift entry today", prev.year, prev,
            )
            send_telegram(
                f"NIFTY WEEKLY OPTIONS - HOLIDAY CALENDAR MISSING FOR {prev.year}\n"
                f"Add NSE's {prev.year} F&O holiday list to trading_core/market_calendar.py. "
                "Until then a Thursday-holiday entry will not shift to Friday."
            )
        return False
    return _HOLIDAY_SHIFT_TAG if prev_trading is False else False


def _try_auto_entry(cfg: dict, variant: dict, collector: LtpCollector, fired: dict[str, date]) -> None:
    vid = variant["id"]
    if not _variant_entry_enabled(cfg, variant):
        return
    now = _ist_now()
    try:
        eh, em = (int(x) for x in cfg.get("entry_time_ist", FO_TRADING_DECISION_CUTOFF).split(":"))
    except Exception:
        eh, em = (int(x) for x in FO_TRADING_DECISION_CUTOFF.split(":"))
    entry_weekday = int(cfg.get("entry_weekday", 3))

    entry_shift = _entry_shift_today(now.date(), entry_weekday)
    if entry_shift is False:
        return
    if not (now.hour > eh or (now.hour == eh and now.minute >= em)):
        return
    if fired.get(f"entry_{vid}") == now.date():
        return

    active_path = variant["active_path"]
    journal_path = _journal_path_for(variant)

    pos = _read_active_position(active_path)
    if pos and pos.get("status") == "open":
        return  # already have an open position — nothing to do

    # mark attempted regardless of outcome — never retry-storm same day
    fired[f"entry_{vid}"] = now.date()
    logger.info("[{}] AUTO-ENTRY triggered at {} IST{}", vid, now.strftime("%H:%M:%S"),
                f" ({entry_shift}: {now.date() - timedelta(days=1)} was an NSE holiday)" if entry_shift else "")

    try:
        run_signal(
            force=True,
            offset=variant["offset"],
            active_path=active_path,
            variant_label=vid,
            paper_only=variant["paper_only"],
            save_signal=(vid == "3L-50"),
        )
    except Exception as exc:
        logger.error("[{}] AUTO-ENTRY: run_signal() failed: {}", vid, exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-ENTRY FAILED\nrun_signal() raised: {exc}")
        return

    pos = _read_active_position(active_path)
    if not pos or pos.get("status") != "open":
        logger.warning("[{}] AUTO-ENTRY: active position file not open after run_signal — aborting", vid)
        send_telegram(
            f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-ENTRY FAILED\n"
            "Active position file was not left in an open state after signal — "
            "check Dhan API / security ID resolution."
        )
        return

    contracts = pos.get("contracts", [])
    expected = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
    if len(expected) != 6:
        logger.warning("[{}] AUTO-ENTRY: expected 6 contracts, got {} — aborting", vid, len(expected))
        _set_position_status(active_path, "aborted", f"expected 6 contracts, got {len(expected)}")
        send_telegram(
            f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-ENTRY FAILED\n"
            f"Expected 6 resolved contracts, got {len(expected)}. No position opened."
        )
        return

    # Give this variant its own dedicated feed, independent of the strike-band monitor.
    _subscribe_variant_legs(variant, pos)

    ltps = collector.wait_for(expected, _ENTRY_LTP_TIMEOUT_S)
    if any(v is None for v in ltps):
        ready = sum(1 for v in ltps if v is not None)
        logger.warning("[{}] AUTO-ENTRY: only {}/6 leg LTPs arrived within {}s — aborting",
                        vid, ready, _ENTRY_LTP_TIMEOUT_S)
        _set_position_status(active_path, "aborted", f"only {ready}/6 leg LTPs within {_ENTRY_LTP_TIMEOUT_S}s")
        send_telegram(
            f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-ENTRY FAILED\n"
            f"Only {ready}/6 leg LTPs received within {_ENTRY_LTP_TIMEOUT_S // 60} minutes "
            f"of signal. No position opened — check tick_service / P2 status."
        )
        return

    atm  = int(pos["atm"])
    spot = float(pos["entry_spot"])
    try:
        record = paper_trade.log_entry(
            atm=atm, legs_ltps=ltps, entry_spot=spot,
            offset=variant["offset"], active_path=active_path,
            journal_path=journal_path, ladder_id=vid, entry_shift=entry_shift,
        )
    except Exception as exc:
        logger.error("[{}] AUTO-ENTRY: log_entry() failed: {}", vid, exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-ENTRY FAILED\nlog_entry() raised: {exc}")
        return

    total_premium = sum(ltps)
    lot_size = record.get("lot_size") or lot_size_for_date(date.fromisoformat(record["expiry_date"]))
    rs_collected = total_premium * lot_size

    leg_lines = [f"  {expected[i][0]:<8} {expected[i][1]:<3} @ {ltps[i]:.2f}" for i in range(6)]
    paper_tag = " [PAPER ONLY -- DO NOT PLACE REAL ORDERS]" if variant["paper_only"] else ""
    action_line = "Legs sold (SELL 1 lot each):" if not variant["paper_only"] else \
        "Legs tracked (paper SELL 1 lot each -- no real orders):"
    msg = "\n".join([
        f"NIFTY WEEKLY OPTIONS [{vid}] - 3L STRADDLE OPENED{paper_tag}",
        f"Entry : {now.strftime('%Y-%m-%d %H:%M')} IST ({now:%a})"
        + (" -- HOLIDAY-SHIFTED, previous day was an NSE holiday" if entry_shift else ""),
        f"Expiry: {record['expiry_date']}",
        f"Spot  : {spot:.2f}  |  ATM: {atm}",
        "",
        action_line,
        *leg_lines,
        "",
        f"Total premium collected: {total_premium:.2f} pts = Rs {rs_collected:,.0f}",
    ])
    send_telegram(msg)
    logger.info("[{}] AUTO-ENTRY complete | ATM={} premium={:.2f} pts Rs={:.0f}",
                vid, atm, total_premium, rs_collected)
    _log_event("entry", vid, atm=atm, entry_spot=spot, expiry_date=record["expiry_date"],
               total_premium_pts=total_premium, rs_collected=rs_collected,
               entry_time_str=now.strftime("%Y-%m-%d %H:%M"), entry_shift=entry_shift)


def _try_auto_exit(cfg: dict, variant: dict, collector: LtpCollector, fired: dict[str, date]) -> None:
    vid = variant["id"]
    if not _variant_exit_enabled(cfg, variant):
        return
    now = _ist_now()
    try:
        xh, xm = (int(x) for x in cfg.get("exit_time_ist", FO_TRADING_DECISION_CUTOFF).split(":"))
    except Exception:
        xh, xm = (int(x) for x in FO_TRADING_DECISION_CUTOFF.split(":"))

    if not (now.hour > xh or (now.hour == xh and now.minute >= xm)):
        return
    if fired.get(f"exit_{vid}") == now.date():
        return

    active_path = variant["active_path"]
    journal_path = _journal_path_for(variant)

    pos = _read_active_position(active_path)
    if not pos or pos.get("status") != "open":
        return
    if pos.get("expiry_date") != str(now.date()):
        return

    fired[f"exit_{vid}"] = now.date()
    logger.info("[{}] AUTO-EXIT triggered at {} IST", vid, now.strftime("%H:%M:%S"))

    contracts = pos.get("contracts", [])
    expected = [(int(c["strike"]), str(c["option_type"])) for c in contracts]
    ltps_raw = collector.wait_for(expected, _EXIT_LTP_TIMEOUT_S)
    # A leg with no LTP at all by the deadline (e.g. deep OTM, zero liquidity) is treated
    # as worthless rather than blocking the mandatory expiry-day close — matches ET's own
    # force-close convention for a missing leg.
    ltps = [v if v is not None else 0.05 for v in ltps_raw]
    missing = sum(1 for v in ltps_raw if v is None)
    if missing:
        logger.warning("[{}] AUTO-EXIT: {}/6 legs had no LTP by deadline — using Rs 0.05 fallback",
                        vid, missing)

    try:
        rec = paper_trade.log_exit(ltps, active_path=active_path, journal_path=journal_path)
    except Exception as exc:
        logger.error("[{}] AUTO-EXIT: log_exit() failed: {}", vid, exc)
        send_telegram(f"NIFTY WEEKLY OPTIONS [{vid}] - AUTO-EXIT FAILED\nlog_exit() raised: {exc}")
        return

    net_pnl = rec.get("net_pnl_rs")
    outcome = rec.get("outcome", "?")
    leg_lines = [f"  {expected[i][0]:<8} {expected[i][1]:<3} @ {ltps[i]:.2f}" for i in range(6)]
    paper_tag = " [PAPER ONLY]" if variant["paper_only"] else ""
    action_line = "Legs bought back:" if not variant["paper_only"] else "Legs tracked (paper buyback):"
    msg = "\n".join([
        f"NIFTY WEEKLY OPTIONS [{vid}] - 3L STRADDLE CLOSED (expiry day){paper_tag}",
        f"Exit: {now.strftime('%Y-%m-%d %H:%M')} IST",
        "",
        action_line,
        *leg_lines,
        "",
        f"Entry premium : {rec.get('total_entry_premium', 0):.2f} pts",
        f"Exit premium  : {rec.get('total_exit_premium', 0):.2f} pts",
        f"Net P&L       : Rs {net_pnl:+,.0f}  ({outcome})" if net_pnl is not None else "Net P&L: n/a",
    ])
    send_telegram(msg)
    logger.info("[{}] AUTO-EXIT complete | outcome={} net_pnl_rs={}", vid, outcome, net_pnl)
    _log_event("exit", vid, exit_reason=outcome, trigger="eod_forced",
               entry_premium_pts=rec.get("total_entry_premium"),
               exit_premium_pts=rec.get("total_exit_premium"), pnl=net_pnl,
               exit_time_str=now.strftime("%Y-%m-%d %H:%M"))


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
               level="INFO")
    logger.add(_PROJECT_DIR / "data" / "scheduler.log", rotation="5 MB", retention="14 days", level="DEBUG")

    from trading_core.single_instance import guard_or_exit  # dup-launch guard, 2026-10-07
    guard_or_exit("scheduler", _PROJECT_DIR / "data", logger=logger)

    started_at = _ist_now().isoformat(timespec="seconds")
    _write_heartbeat(started_at)
    logger.info("Options scheduler started")

    collector = LtpCollector()
    nifty_spot = NiftySpotCollector()

    # Give the ZMQ subscriber a real chance to receive its first NIFTY tick before
    # anything tries to read it — fixed 2026-07-07. Without this, the very first
    # _refresh_daily_preview() call fired within ~1-2s of process start, always found
    # nifty_spot empty (regardless of how long P2 had already been running — the
    # scheduler's OWN subscriber socket only starts this moment), and fell through to
    # the REST fallback on literally every restart. This wait is capped so a genuinely
    # down/unreachable P2 doesn't hang startup — REST fallback still runs after it.
    _NIFTY_TICK_WAIT_S = 15
    deadline = time.monotonic() + _NIFTY_TICK_WAIT_S
    while nifty_spot.spot() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    if nifty_spot.spot() is None:
        logger.warning(
            "No NIFTY tick received via ZMQ within {}s of startup — "
            "first preview cycle will fall back to REST", _NIFTY_TICK_WAIT_S,
        )

    fired: dict[str, date] = {}
    preview = PreviewState()
    band = StrikeBandState()
    last_hb = time.monotonic()
    last_preview_hb = 0.0

    try:
        while True:
            for variant in LADDER_VARIANTS:
                _retire_stale_position(variant)
            cfg = _read_cfg()
            if cfg:
                for variant in LADDER_VARIANTS:
                    _try_auto_entry(cfg, variant, collector, fired)
                    _try_auto_exit(cfg, variant, collector, fired)

            pos = _read_active_position()
            if not (pos and pos.get("status") == "open"):
                _refresh_daily_preview(preview, nifty_spot)

            # Strike-band monitor runs every day regardless of position state — pure
            # visibility, not gated on being flat like the entry-decision preview above.
            _refresh_strike_band(band, nifty_spot)

            now_mono = time.monotonic()
            if now_mono - last_preview_hb >= _PREVIEW_HEARTBEAT_INTERVAL_S:
                _log_premium_heartbeat(preview, collector)
                last_preview_hb = now_mono

            if now_mono - last_hb >= _HEARTBEAT_INTERVAL:
                _write_heartbeat(started_at)
                last_hb = now_mono

            if should_end_session(NSE_SESSION_END):
                logger.info(
                    "SESSION_END | wall-clock reached {} — exiting for the day", NSE_SESSION_END,
                )
                break

            time.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        nifty_spot.stop()
        logger.info("Options scheduler stopped")


if __name__ == "__main__":
    main()
