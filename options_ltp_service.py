"""
options_ltp_service.py — Polls Dhan REST for active Nifty option LTPs.

Run alongside EasyTerminal to power the live Options tab.

Architecture
------------
  signal.py    writes  D:\\Trading\\active_options_position.json
  this service reads   the same file (watches for changes)
  this service polls   Dhan REST /v2/marketfeed/ltp every POLL_INTERVAL seconds
  this service pub     ZMQ port 5557  topic=b"OPT_<strike>_<type>"
  EasyTerminal sub     port 5557 — ZmqOptionsWorker updates the Options panel

Topic format: b"OPT_24000_CE"
Payload:      JSON {"strike": 24000, "option_type": "CE", "ltp": 127.15}

Stops polling when active_options_position.json has "status": "closed".
Resumes when a new "open" position is written (next Thursday signal).

Usage
-----
  uv run python options_ltp_service.py

Environment (optional — all have sensible defaults)
-----
  ACTIVE_OPTIONS_PATH  path to shared position file
                       default: D:\\Trading\\active_options_position.json
  ZMQ_OPTIONS_PORT     ZMQ PUB port
                       default: 5557
  OPTIONS_POLL_SECS    seconds between Dhan REST polls
                       default: 15
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
import truststore
import zmq
from dotenv import load_dotenv
from loguru import logger

load_dotenv(Path(__file__).resolve().parent / ".env")
truststore.inject_into_ssl()

ACTIVE_OPTIONS_PATH = Path(
    os.environ.get("ACTIVE_OPTIONS_PATH", r"D:\Trading\active_options_position.json")
)
ZMQ_PORT   = int(os.environ.get("ZMQ_OPTIONS_PORT", 5557))
POLL_SECS  = float(os.environ.get("OPTIONS_POLL_SECS", 15))
DHAN_BASE  = "https://api.dhan.co/v2"


def _dhan_headers() -> dict:
    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    token     = os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not token:
        raise RuntimeError(
            "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set in .env"
        )
    return {
        "access-token": token,
        "client-id":    client_id,
        "Content-Type": "application/json",
    }


def _read_position() -> dict | None:
    """Read active_options_position.json. Returns None if file missing or unreadable."""
    if not ACTIVE_OPTIONS_PATH.exists():
        return None
    try:
        return json.loads(ACTIVE_OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fetch_ltps(
    security_ids: list[str],
    headers: dict,
) -> dict[str, float]:
    """
    POST /v2/marketfeed/ltp with NSE_FNO security IDs.
    Returns {security_id_str: ltp_float}, empty dict on any failure.
    """
    try:
        resp = httpx.post(
            f"{DHAN_BASE}/marketfeed/ltp",
            json={"NSE_FNO": security_ids},
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
        logger.warning("LTP fetch failed: {}", exc)
        return {}


def _publish(pub: zmq.Socket, strike: int, option_type: str, ltp: float) -> None:
    topic   = f"OPT_{strike}_{option_type}".encode()
    payload = json.dumps({
        "strike":      strike,
        "option_type": option_type,
        "ltp":         ltp,
    }).encode()
    try:
        pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
    except zmq.Again:
        pass  # no subscriber — drop silently


def main() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
        level="INFO",
    )
    logger.add(
        Path(__file__).resolve().parent / "data" / "options_ltp_service.log",
        rotation="5 MB",
        retention="14 days",
        level="DEBUG",
    )

    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    pub.setsockopt(zmq.LINGER, 0)
    pub.bind(f"tcp://127.0.0.1:{ZMQ_PORT}")
    logger.info("OPTIONS_LTP | ZMQ PUB bound on tcp://127.0.0.1:{}", ZMQ_PORT)
    logger.info("OPTIONS_LTP | Watching {}", ACTIVE_OPTIONS_PATH)
    logger.info("OPTIONS_LTP | Poll interval: {}s", POLL_SECS)

    headers: dict | None = None
    try:
        headers = _dhan_headers()
    except RuntimeError as exc:
        logger.error("OPTIONS_LTP | {}", exc)
        sys.exit(1)

    last_mtime: float | None = None
    contracts: list[dict] = []
    position_status = "none"

    try:
        while True:
            # --- check for position file changes ---
            if ACTIVE_OPTIONS_PATH.exists():
                mtime = ACTIVE_OPTIONS_PATH.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    pos = _read_position()
                    if pos is None:
                        contracts = []
                        position_status = "none"
                    else:
                        position_status = pos.get("status", "none")
                        if position_status == "open":
                            raw = pos.get("contracts", [])
                            # Only include contracts with a valid resolved security_id
                            contracts = [c for c in raw if c.get("security_id")]
                            logger.info(
                                "OPTIONS_LTP | New open position | ATM={} expiry={} | {} contracts with IDs",
                                pos.get("atm"), pos.get("expiry_date"), len(contracts),
                            )
                        else:
                            contracts = []
                            logger.info(
                                "OPTIONS_LTP | Position status={} — polling paused",
                                position_status,
                            )

            # --- poll LTPs for open position ---
            if contracts:
                sec_ids = [c["security_id"] for c in contracts]
                ltps = _fetch_ltps(sec_ids, headers)

                published = 0
                for c in contracts:
                    sid = c["security_id"]
                    ltp = ltps.get(sid)
                    if ltp is not None:
                        _publish(pub, c["strike"], c["option_type"], ltp)
                        published += 1

                if published:
                    logger.debug(
                        "OPTIONS_LTP | Published {}/{} LTPs | sample: {} @ {}",
                        published,
                        len(contracts),
                        f"{contracts[0]['strike']} {contracts[0]['option_type']}",
                        ltps.get(contracts[0]["security_id"]),
                    )
                else:
                    logger.debug("OPTIONS_LTP | No LTPs received this poll")

            time.sleep(POLL_SECS)

    except KeyboardInterrupt:
        logger.info("OPTIONS_LTP | Interrupted — shutting down")
    finally:
        pub.close()
        ctx.term()
        logger.info("OPTIONS_LTP | Stopped")


if __name__ == "__main__":
    main()
