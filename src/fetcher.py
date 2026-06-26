"""
Fetches rolling options data from Dhan's POST /charts/rollingoption endpoint.

Actual response structure (confirmed from live API):
  {
    "data": {
      "ce": {"open":[], "high":[], ..., "timestamp":[], "strike":[], "spot":[]},  -- filled when drvOptionType="CALL"
      "pe": null   -- null when drvOptionType="CALL"; filled when "PUT"
    }
  }
  - drvOptionType matters: "CALL" fills ce, "PUT" fills pe. The other side is null.
  - "strike" = actual absolute strike (e.g. 21700.0) — varies per bar as ATM rolls.
  - "timestamp" = Unix seconds (UTC).
  - No top-level "status" field — check for the non-null data side.

Strategy: 18 streams (9 relative strikes × 2 option types).
~648 API calls for 3 years; ~6 min at default rate limit.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
import truststore
from loguru import logger

from src.config import (
    DHAN_API_BASE,
    NIFTY_SECURITY_ID,
    OPTION_TYPES,
    RELATIVE_STRIKES,
    settings,
)

# Windows: inject system CA chain (Dhan's cert issuer not in Python's default store)
truststore.inject_into_ssl()


# ── Low-level HTTP ────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "access-token": settings.dhan_access_token.get_secret_value(),
        "client-id":    settings.dhan_client_id,
        "Content-Type": "application/json",
    }


def _post(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    url  = f"{DHAN_API_BASE}{endpoint}"
    resp = httpx.post(url, json=payload, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ── Rolling option fetch ──────────────────────────────────────────────────────

def fetch_rolling_option(
    relative_strike: str,
    option_type: str,
    from_date: str,
    to_date: str,
    interval: int = 5,
) -> dict | None:
    """
    Single call to /charts/rollingoption for one (relative_strike, option_type, date-range).

    relative_strike: "ATM", "ATM+1", "ATM-2", etc.
    option_type: "CALL" or "PUT"  (controls which side of the response is populated)
    from_date/to_date: "YYYY-MM-DD" (max 30 days per call)
    interval: bar size in minutes (1, 5, 15, 25, 60)

    Returns the raw response dict or None on error.
    """
    drv_key  = "ce" if option_type == "CALL" else "pe"
    payload = {
        "securityId":      NIFTY_SECURITY_ID,
        "exchangeSegment": "NSE_FNO",
        "instrument":      "OPTIDX",
        "expiryFlag":      "WEEK",
        "expiryCode":      1,        # 1 = nearest front-week expiry; 0 rejected as falsy by API
        "strike":          relative_strike,
        "drvOptionType":   option_type,
        "requiredData":    ["open", "high", "low", "close", "iv", "volume", "oi", "spot", "strike"],
        "fromDate":        from_date,
        "toDate":          to_date,
        "interval":        interval,
    }
    try:
        resp = _post("/charts/rollingoption", payload)
        data = resp.get("data", {})
        if not isinstance(data, dict) or data.get(drv_key) is None:
            logger.warning(
                "rollingoption: no {} data | strike={} {} -> {} | keys={}",
                drv_key, relative_strike, from_date, to_date, list(resp.keys()),
            )
            return None
        return resp

    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400] if exc.response else ""
        logger.warning(
            "rollingoption HTTP {} | strike={} {} {} -> {} | {}",
            exc.response.status_code if exc.response else "?",
            option_type, relative_strike, from_date, to_date, body,
        )
        return None
    except Exception as exc:
        logger.warning(
            "rollingoption error | {} {} {} -> {}: {}",
            option_type, relative_strike, from_date, to_date, exc,
        )
        return None


# ── Date-range chunking ───────────────────────────────────────────────────────

def _month_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """Split [start, end] into calendar-month windows (≤30 days each)."""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    ranges = []
    cur = s
    while cur <= e:
        if cur.month == 12:
            month_end = date(cur.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, e)
        ranges.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + timedelta(days=1)
    return ranges


# ── Caching helpers ───────────────────────────────────────────────────────────

def _raw_path(relative_strike: str, option_type: str, yyyymm: str) -> Path:
    """e.g. data/options/raw/ATMp1_CALL_2024-03.json"""
    safe = relative_strike.replace("+", "p").replace("-", "m")
    return settings.raw_dir / f"{safe}_{option_type}_{yyyymm}.json"


def _yyyymm(from_date: str) -> str:
    return from_date[:7]


# ── Main fetch entry points ───────────────────────────────────────────────────

def fetch_one_stream(
    relative_strike: str,
    option_type: str,
    start: str,
    end: str,
    force: bool = False,
    interval: int = 5,
) -> int:
    """
    Fetch all monthly chunks for one (relative_strike, option_type) stream.
    Skips chunks where the raw file already exists (unless force=True).
    Returns number of new chunks fetched.
    """
    ranges  = _month_ranges(start, end)
    fetched = 0
    for from_date, to_date in ranges:
        key  = _yyyymm(from_date)
        path = _raw_path(relative_strike, option_type, key)
        if path.exists() and not force:
            logger.debug("CACHE HIT | {} {} {}", option_type, relative_strike, key)
            continue
        logger.info("FETCH | {} {} {} -> {}", option_type, relative_strike, from_date, to_date)
        resp = fetch_rolling_option(relative_strike, option_type, from_date, to_date, interval)
        if resp is not None:
            path.write_text(json.dumps(resp), encoding="utf-8")
            fetched += 1
        time.sleep(settings.api_delay_s)
    return fetched


def fetch_all_streams(start: str, end: str, force: bool = False, interval: int = 5) -> None:
    """
    Fetch all 18 streams (9 relative strikes × 2 option types).
    ~648 API calls for 3 years at 0.5s/call ≈ 6 minutes.
    """
    total   = len(RELATIVE_STRIKES) * len(OPTION_TYPES)
    fetched = 0
    idx     = 0
    for rel_strike in RELATIVE_STRIKES:
        for opt_type in OPTION_TYPES:
            idx += 1
            n = fetch_one_stream(rel_strike, opt_type, start, end, force=force, interval=interval)
            fetched += n
            logger.info("Stream {}/{} | {} {} | {} new chunks", idx, total, opt_type, rel_strike, n)

    logger.success(
        "FETCH COMPLETE | {} new API calls | {} streams | raw files -> {}",
        fetched, total, settings.raw_dir,
    )


# ── Load cached raw data ──────────────────────────────────────────────────────

def load_raw_stream(relative_strike: str, option_type: str) -> list[dict]:
    """Load all cached monthly JSON files for one (relative_strike, option_type) stream."""
    safe    = relative_strike.replace("+", "p").replace("-", "m")
    pattern = f"{safe}_{option_type}_*.json"
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(settings.raw_dir.glob(pattern))
    ]
