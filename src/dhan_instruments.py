"""
Dhan instruments master CSV — resolves absolute Nifty option security IDs.

Downloads the public instruments CSV once per day and caches it locally.
Used by signal.py to write security IDs into active_options_position.json
so the LTP service and EasyTerminal can monitor live prices.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

import httpx
import pandas as pd
import truststore

truststore.inject_into_ssl()

_INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"
_CACHE_FILE = _CACHE_DIR / "dhan_instruments.csv"
_CACHE_TTL_HOURS = 20   # re-download if older than this


def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age_hours = (time.time() - _CACHE_FILE.stat().st_mtime) / 3600
    return age_hours < _CACHE_TTL_HOURS


def download_instruments_csv(force: bool = False) -> Path:
    """
    Download the Dhan instruments master CSV to data/dhan_instruments.csv.
    Skips the download if the cached file is <20 hours old unless force=True.
    Returns the path to the (possibly cached) CSV.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not force and _cache_is_fresh():
        return _CACHE_FILE

    try:
        resp = httpx.get(_INSTRUMENTS_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        _CACHE_FILE.write_bytes(resp.content)
        return _CACHE_FILE
    except Exception as exc:
        if _CACHE_FILE.exists():
            # Stale cache is better than nothing
            return _CACHE_FILE
        raise RuntimeError(
            f"Cannot download instruments master and no cache exists: {exc}"
        ) from exc


def resolve_option_ids(
    strikes: list[int],
    expiry_date: date,
    underlying: str = "NIFTY",
) -> list[dict]:
    """
    Look up Dhan security IDs for a set of NIFTY option strikes.

    Returns a list of dicts (one per leg, in the same order as the input strike list
    × ['CE', 'PE'] pairs):
      [{
        "strike": 24000,
        "option_type": "CE",
        "security_id": "49081",
        "exchange_segment": "NSE_FNO",
      }, ...]

    Raises RuntimeError if the instruments CSV cannot be loaded or no matching
    contracts are found for the given expiry.
    """
    csv_path = download_instruments_csv()

    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception as exc:
        raise RuntimeError(f"Cannot read instruments CSV at {csv_path}: {exc}") from exc

    # Normalise column names — Dhan has used several casing conventions historically
    df.columns = df.columns.str.strip()

    # Identify column names defensively
    def _col(candidates: list[str]) -> str | None:
        for c in candidates:
            if c in df.columns:
                return c
        return None

    col_exch   = _col(["SEM_EXM_EXCH_ID", "EXCHANGE", "exchange"])
    col_secid  = _col(["SEM_SMST_SECURITY_ID", "SECURITY_ID", "security_id"])
    col_expiry = _col(["SEM_EXPIRY_DATE", "EXPIRY_DATE", "expiry_date"])
    col_strike = _col(["SEM_STRIKE_PRICE", "STRIKE_PRICE", "strike_price"])
    col_type   = _col(["SEM_OPTION_TYPE", "OPTION_TYPE", "option_type"])
    col_name   = _col(["SM_SYMBOL_NAME", "SYMBOL_NAME", "symbol_name", "SEM_TRADING_SYMBOL"])

    missing = [n for n, c in [
        ("exchange", col_exch), ("security_id", col_secid),
        ("expiry_date", col_expiry), ("strike", col_strike),
        ("option_type", col_type), ("symbol_name", col_name),
    ] if c is None]
    if missing:
        raise RuntimeError(
            f"Instruments CSV is missing expected columns: {missing}. "
            f"Found columns: {list(df.columns[:20])}"
        )

    # Filter: NSE_FNO + NIFTY underlying
    fno = df[df[col_exch] == "NSE_FNO"].copy()
    nifty = fno[fno[col_name].astype(str).str.upper() == underlying.upper()].copy()

    if nifty.empty:
        raise RuntimeError(
            f"No NSE_FNO {underlying} rows found in instruments master. "
            f"Check the CSV is current."
        )

    # Normalise expiry date column — Dhan uses "YYYY-MM-DD" but has varied
    expiry_str = expiry_date.strftime("%Y-%m-%d")

    def _try_parse_expiry(s: str) -> str:
        """Return YYYY-MM-DD regardless of input format."""
        s = str(s).strip()
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return s  # leave as-is if we can't parse

    nifty = nifty.copy()
    nifty["_expiry_norm"] = nifty[col_expiry].astype(str).apply(_try_parse_expiry)
    nifty = nifty[nifty["_expiry_norm"] == expiry_str]

    if nifty.empty:
        raise RuntimeError(
            f"No {underlying} options found for expiry {expiry_str} in instruments master. "
            f"The CSV may not yet include next-week contracts (download again on Thursday)."
        )

    # Filter: options only (exclude futures)
    nifty = nifty[nifty[col_type].astype(str).isin(["CE", "PE"])]

    # Build lookup: (strike_int, option_type) -> security_id
    lookup: dict[tuple[int, str], str] = {}
    for _, row in nifty.iterrows():
        try:
            s = int(float(row[col_strike]))
            t = str(row[col_type]).strip().upper()
            sid = str(int(float(row[col_secid])))
            lookup[(s, t)] = sid
        except (ValueError, TypeError):
            continue

    results = []
    missing_legs = []
    for strike in strikes:
        for otype in ("CE", "PE"):
            sid = lookup.get((strike, otype))
            if sid is None:
                missing_legs.append(f"{strike} {otype}")
                sid = ""   # still include the leg; caller decides what to do
            results.append({
                "strike":           strike,
                "option_type":      otype,
                "security_id":      sid,
                "exchange_segment": "NSE_FNO",
            })

    if missing_legs:
        import warnings
        warnings.warn(
            f"Security IDs not found for legs: {missing_legs}. "
            f"LTP polling will skip these legs.",
            stacklevel=2,
        )

    return results
