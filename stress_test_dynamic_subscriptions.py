"""
stress_test_dynamic_subscriptions.py — ad hoc stress test for the generic P2 subscription
registry (trading_core.subscription_registry). Added 2026-07-07.

Every --interval seconds (default 60), picks a random Nifty strike (step 50, offset -500..+500
from current spot), requests both legs (CE+PE) of that strike via request_subscription() if not
already tracked, and waits for their live premiums to arrive over ZMQ — proving P2 can handle a
continuously growing, unpredictable set of dynamically-requested security IDs with no restart
and no P2 code change, exactly the "future strategy needs new security IDs on the fly" scenario
this registry was built for.

Uses its own topic prefix (STRESS_) so its ticks never mix with real trading data (LtpCollector
only ever subscribes to OPT_) -- but it still subscribes real security IDs on the ONE live P2
WebSocket, since there's only ever one Dhan connection. There is no isolated "test P2" to run
this against; running it means it is live on the same feed every other strategy reads.

RUN THIS MANUALLY, FOR VALIDATION ONLY -- DO NOT LEAVE IT RUNNING (changed 2026-07-08).
Originally documented as a permanent background utility; downgraded after it was found still
running during real market hours, injecting fake STRESS_* subscriptions into the live P2 feed
without that being anyone's intent. Run it after a change to the subscription registry itself
to confirm the mechanism still works end-to-end, then Ctrl+C it and delete
D:\\Trading\\dynamic_subscriptions\\stress_test_dynamic_subscriptions.json -- killing the
process does NOT un-subscribe its legs or stop P2 from re-reading that stale request file on
its next restart (see TradingWebSockets/CLAUDE.md's "Dynamic Subscriptions" gotcha).

Usage:
  uv run python stress_test_dynamic_subscriptions.py                 # forever, 60s interval
  uv run python stress_test_dynamic_subscriptions.py --interval 30 --iterations 20
  Ctrl+C to stop, then delete its dynamic_subscriptions/*.json request file (see above).

Logs to stderr and data/stress_test_dynamic_subscriptions.log.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from datetime import date
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_DIR))

import zmq
from loguru import logger

from src.dhan_instruments import resolve_option_ids
from src.signal import next_expiry_tuesday
from trading_core.subscription_registry import request_subscription

_PRIMARY_PORT = 5555
_STRESS_PREFIX = b"STRESS_"
_REQUESTER = "stress_test_dynamic_subscriptions"


class NiftySpotReader:
    """Minimal ZMQ reader for the NIFTY index topic — no REST call, ever."""

    def __init__(self) -> None:
        self._ltp: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="stress-nifty-reader").start()

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
                    with self._lock:
                        self._ltp = float(data["ltp"])
                except Exception:
                    continue
        finally:
            sub.close()
            ctx.term()

    def stop(self) -> None:
        self._stop.set()

    def spot(self) -> float | None:
        with self._lock:
            return self._ltp


class StressLegCollector:
    """Collects premiums for STRESS_-prefixed topics, keyed by full topic string."""

    def __init__(self) -> None:
        self._ltps: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="stress-leg-collector").start()

    def _run(self) -> None:
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(f"tcp://127.0.0.1:{_PRIMARY_PORT}")
        sub.setsockopt(zmq.SUBSCRIBE, _STRESS_PREFIX)
        try:
            while not self._stop.is_set():
                if not sub.poll(timeout=500):
                    continue
                try:
                    topic, raw = sub.recv_multipart(flags=zmq.NOBLOCK)
                except zmq.Again:
                    continue
                try:
                    data = json.loads(raw.decode())
                    with self._lock:
                        self._ltps[topic.decode()] = float(data["ltp"])
                except Exception:
                    continue
        finally:
            sub.close()
            ctx.term()

    def stop(self) -> None:
        self._stop.set()

    def get(self, topic: str) -> float | None:
        with self._lock:
            return self._ltps.get(topic)


def _round_to_50(x: float) -> int:
    return int(round(x / 50) * 50)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
        level="INFO",
    )
    logger.add(
        _PROJECT_DIR / "data" / "stress_test_dynamic_subscriptions.log",
        rotation="5 MB", retention="3 days", level="DEBUG",
    )

    logger.info("=" * 60)
    logger.info("DYNAMIC SUBSCRIPTION STRESS TEST")
    logger.info("Random Nifty straddle every {}s, offset -500..+500 (step 50)", args.interval)
    logger.info("=" * 60)

    nifty = NiftySpotReader()
    legs = StressLegCollector()

    logger.info("Waiting for first NIFTY tick via ZMQ...")
    deadline = time.time() + 15
    while nifty.spot() is None and time.time() < deadline:
        time.sleep(0.5)
    spot = nifty.spot()
    if spot is None:
        logger.error("No NIFTY tick received within 15s — is P2 running? Aborting.")
        return
    logger.info("NIFTY spot = {:.2f}", spot)

    requested: list[dict] = []       # accumulated across iterations — additive, like real usage
    seen_strikes: set[int] = set()
    expiry = next_expiry_tuesday(date.today())

    i = 0
    try:
        while args.iterations == 0 or i < args.iterations:
            i += 1
            spot = nifty.spot() or spot
            offset = random.choice(range(-500, 501, 50))
            strike = _round_to_50(spot) + offset

            if strike not in seen_strikes:
                seen_strikes.add(strike)
                try:
                    resolved = resolve_option_ids([strike], expiry)
                except Exception as exc:
                    logger.warning("Iter {}: could not resolve strike {}: {}", i, strike, exc)
                    time.sleep(args.interval)
                    continue
                for c in resolved:
                    requested.append({
                        "exchange_segment": "NSE_FNO",
                        "security_id": str(c["security_id"]),
                        "topic": f"STRESS_{c['strike']}_{c['option_type']}",
                        "strike": int(c["strike"]),
                        "option_type": str(c["option_type"]),
                    })
                request_subscription(_REQUESTER, requested)
                logger.info(
                    "Iter {}: NEW strike {} (offset {:+d} from spot {:.2f}) requested — "
                    "{} distinct strikes tracked so far",
                    i, strike, offset, spot, len(seen_strikes),
                )
            else:
                logger.info(
                    "Iter {}: strike {} (offset {:+d}) already tracked — reusing subscription",
                    i, strike, offset,
                )

            ce_topic = f"STRESS_{strike}_CE"
            pe_topic = f"STRESS_{strike}_PE"
            wait_deadline = time.time() + 20
            ce = pe = None
            while time.time() < wait_deadline:
                ce = legs.get(ce_topic)
                pe = legs.get(pe_topic)
                if ce is not None and pe is not None:
                    break
                time.sleep(1)

            if ce is not None and pe is not None:
                logger.info(
                    "Iter {}: STRADDLE {} | CE={:.2f} PE={:.2f} | total premium={:.2f} pts",
                    i, strike, ce, pe, ce + pe,
                )
            else:
                logger.warning(
                    "Iter {}: STRADDLE {} premium incomplete — CE={} PE={}",
                    i, strike, ce, pe,
                )

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        nifty.stop()
        legs.stop()
        logger.info(
            "Stress test stopped after {} iteration(s); {} distinct strikes tracked total: {}",
            i, len(seen_strikes), sorted(seen_strikes),
        )


if __name__ == "__main__":
    main()
