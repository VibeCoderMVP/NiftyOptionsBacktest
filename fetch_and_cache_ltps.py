r"""
One-shot: fetch current option LTPs from Dhan and write options_ltp_cache.json.
Also recreates active_options_position.json.

Run from D:\Trading\TradingWebSockets\ (uses TW venv which has dhanhq):
    uv run python ..\NiftyOptionsBacktest\fetch_and_cache_ltps.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TW = Path(__file__).resolve().parent.parent / "TradingWebSockets"
sys.path.insert(0, str(TW))

import truststore
truststore.inject_into_ssl()

from dotenv import load_dotenv
load_dotenv(TW / ".env")

from brokers.dhan import DhanBroker

ACTIVE_PATH = Path(r"D:\Trading\active_options_position.json")
LTP_CACHE   = Path(r"D:\Trading\options_ltp_cache.json")

CONTRACTS = [
    {"strike": 24000, "option_type": "CE", "security_id": "71472", "entry_ltp": 158.35},
    {"strike": 24000, "option_type": "PE", "security_id": "71473", "entry_ltp":  65.55},
    {"strike": 24050, "option_type": "CE", "security_id": "79730", "entry_ltp": 127.15},
    {"strike": 24050, "option_type": "PE", "security_id": "79731", "entry_ltp":  83.60},
    {"strike": 24100, "option_type": "CE", "security_id": "79732", "entry_ltp":  99.00},
    {"strike": 24100, "option_type": "PE", "security_id": "79733", "entry_ltp": 105.35},
]


def recreate_active_position() -> None:
    pos = {
        "status":      "open",
        "updated_at":  "2026-06-27T15:01:00",
        "entry_date":  "2026-06-25",
        "expiry_date": "2026-06-30",
        "atm":         24050,
        "entry_spot":  24046.25,
        "contracts": [
            {**c, "exchange_segment": "NSE_FNO"} for c in CONTRACTS
        ],
    }
    tmp = ACTIVE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pos, indent=2), encoding="utf-8")
    tmp.replace(ACTIVE_PATH)
    print(f"Recreated {ACTIVE_PATH}")


def fetch_and_cache() -> None:
    broker = DhanBroker()
    client = broker._get_client()

    sids_int = [int(c["security_id"]) for c in CONTRACTS]
    print(f"Fetching LTPs for NSE_FNO SIDs: {sids_int}")

    resp = client.ticker_data({"NSE_FNO": sids_int})
    print(f"Response: {json.dumps(resp, indent=2)[:600]}")

    # dhanhq wraps: resp["data"]["data"]["NSE_FNO"]
    inner = (resp or {}).get("data", {})
    if "data" in inner:
        inner = inner["data"]
    fno_data = inner.get("NSE_FNO", {})
    if not fno_data:
        print("No NSE_FNO data — cannot populate cache")
        return

    cache: dict[str, float] = {}
    total_ltp = 0.0
    for c in CONTRACTS:
        sid = c["security_id"]
        entry = fno_data.get(sid) or fno_data.get(str(sid)) or fno_data.get(int(sid))
        if entry is None:
            print(f"  {c['strike']} {c['option_type']} SID={sid}: missing")
            continue
        ltp = entry.get("last_price") or entry.get("LTP") or entry.get("ltp")
        if ltp is None:
            print(f"  {c['strike']} {c['option_type']}: no ltp field in {entry}")
            continue
        ltp = float(ltp)
        total_ltp += ltp
        pnl = round(c["entry_ltp"] - ltp, 2)
        cache[f"{c['strike']}_{c['option_type']}"] = ltp
        print(f"  {c['strike']} {c['option_type']}: LTP={ltp:.2f}  entry={c['entry_ltp']}  P&L={pnl:+.2f} pts")

    if not cache:
        print("No LTPs parsed — cache not updated")
        return

    tmp = LTP_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(LTP_CACHE)

    total_entry = sum(c["entry_ltp"] for c in CONTRACTS)
    pnl_pts = round(total_entry - total_ltp, 2)
    pnl_rs  = round(pnl_pts * 75, 0)
    print(f"\nTotal: entry={total_entry:.2f}  curr={total_ltp:.2f}  "
          f"P&L={pnl_pts:+.2f} pts = Rs {pnl_rs:+,.0f}")
    print(f"Written {len(cache)} LTPs -> {LTP_CACHE}")
    print("Switch to ET Options tab (F4) to see prices as LAST.")


if __name__ == "__main__":
    recreate_active_position()
    fetch_and_cache()
