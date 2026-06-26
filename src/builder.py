"""
Builds weekly parquet files from raw API responses.

Flow:
  1. Parse all raw JSON -> one consolidated DataFrame per stream
  2. Identify every Tuesday in the backtest range as an entry date
  3. For each Tuesday:
     a. Extract Tuesday's closing spot price -> round to nearest 50 -> ATM
     b. Determine 10 target absolute strikes (ATM-100, ATM-50, ATM, ATM+50, ATM+100 × CE+PE)
     c. Search across all streams for bars where actual `strike` matches each target
     d. Keep only Tue/Wed/Thu bars (Tuesday open -> Thursday close)
  4. Write one parquet per expiry Thursday: data/options/weekly/YYYY-MM-DD.parquet

The spot price comes from the `spot` column in the API response — no separate
Nifty spot API call needed.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from src.config import (
    LADDER_OFFSETS,
    NIFTY_LOT_SIZE,
    OPTION_TYPES,
    REGIME_CHANGE_DATE,
    RELATIVE_STRIKES,
    STRIKE_STEP,
    settings,
)
from src.fetcher import load_raw_stream


# ── Parse raw API response -> DataFrame ───────────────────────────────────────
# Actual Dhan rollingoption response structure:
#   {"data": {"ce": {...arrays...}, "pe": null}}  when drvOptionType="CALL"
#   {"data": {"ce": null, "pe": {...arrays...}}}  when drvOptionType="PUT"
# Timestamps are Unix seconds (UTC); strike is float (actual absolute strike).

def _parse_response(resp: dict, relative_strike: str, option_type: str) -> pd.DataFrame:
    """
    Parse one raw API response for one option_type ("CALL" or "PUT").
    Reads from data["ce"] for CALL, data["pe"] for PUT.
    """
    data     = resp.get("data", {})
    drv_key  = "ce" if option_type == "CALL" else "pe"
    arrays   = data.get(drv_key) if isinstance(data, dict) else None
    opt_label = "CE" if option_type == "CALL" else "PE"

    if not isinstance(arrays, dict) or "timestamp" not in arrays:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(arrays)
        df.columns = df.columns.str.lower().str.strip()

        # Timestamps: Unix seconds UTC -> IST datetime
        df["timestamp"] = (
            pd.to_datetime(df["timestamp"].astype(float), unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.tz_localize(None)
        )

        for col in ["open", "high", "low", "close", "iv", "spot"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ["volume", "oi"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        # "strike" column = actual absolute strike price (e.g. 21700.0 -> 21700)
        if "strike" in df.columns:
            df["strike"] = pd.to_numeric(df["strike"], errors="coerce").fillna(0).astype(int)

        df["relative_strike"] = relative_strike
        df["option_type"]     = opt_label
        df["date"]            = df["timestamp"].dt.date

        return df.dropna(subset=["timestamp", "close"]).reset_index(drop=True)
    except Exception as exc:
        logger.warning("Parse error for {} {}: {}", relative_strike, option_type, exc)
        return pd.DataFrame()


def load_all_streams() -> pd.DataFrame:
    """
    Load and parse every cached raw stream into one big DataFrame.
    18 streams (9 relative strikes × 2 option types).
    """
    frames = []
    for rel_strike in RELATIVE_STRIKES:
        for opt_type in OPTION_TYPES:
            monthly_resps = load_raw_stream(rel_strike, opt_type)
            for resp in monthly_resps:
                df = _parse_response(resp, rel_strike, opt_type)
                if not df.empty:
                    frames.append(df)

    if not frames:
        logger.error("No raw data found — run 'fetch' step first.")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp", "relative_strike", "option_type"])
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    logger.info(
        "Loaded {:,} bars | {} relative strikes | CE+PE",
        len(combined), combined["relative_strike"].nunique(),
    )
    return combined


# ── Identify weekly cycles ────────────────────────────────────────────────────

def _round_to_nearest_50(price: float) -> int:
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def _last_spot(spot_ref: pd.DataFrame, target_date: date) -> float | None:
    """Return Nifty spot from the last 5-min bar of target_date, or None if missing."""
    rows = spot_ref[spot_ref["date"] == target_date].sort_values("timestamp")
    if rows.empty:
        return None
    return float(rows.iloc[-1]["spot"])


def identify_weekly_cycles(
    all_data: pd.DataFrame,
    start: str,
    end: str,
) -> list[dict]:
    """
    Return weekly cycle dicts across both expiry regimes.

    OLD REGIME (before REGIME_CHANGE_DATE = Sep 1 2025) — Thursday expiry:
      entry_date  = Tuesday   (ATM fixed at Tuesday close)
      expiry_date = Thursday  (+2 calendar days)
      window      = Tue, Wed, Thu  (2 trading sessions of hold)
      regime      = "thu_expiry"

    NEW REGIME (Sep 1 2025 onwards) — Tuesday expiry:
      entry_date  = Thursday  (ATM fixed at Thursday close, previous week)
      expiry_date = Tuesday   (+5 calendar days)
      window      = Thu, Fri, Mon, Tue  (4 trading sessions of hold)
      regime      = "tue_expiry"
    """
    start_d = date.fromisoformat(start)
    end_d   = date.fromisoformat(end)

    spot_ref = all_data[
        (all_data["relative_strike"] == "ATM") &
        (all_data["option_type"] == "CE")
    ][["date", "timestamp", "spot"]].copy()

    cycles = []

    # ── OLD REGIME 2-day: find Tuesdays, exit Thursday ───────────────────────
    d = start_d
    while d < REGIME_CHANGE_DATE and d <= end_d:
        if d.weekday() != 1:   # Tuesday
            d += timedelta(days=1)
            continue

        expiry = d + timedelta(days=2)
        if expiry > end_d or expiry >= REGIME_CHANGE_DATE:
            d += timedelta(days=1)
            continue

        spot = _last_spot(spot_ref, d)
        if spot is None:
            logger.debug("OLD 2d: no spot for Tuesday {} — skip", d)
            d += timedelta(days=1)
            continue

        cycles.append({
            "entry_date":  d,
            "expiry_date": expiry,
            "entry_spot":  spot,
            "atm_strike":  _round_to_nearest_50(spot),
            "regime":      "thu_expiry",
        })
        d += timedelta(days=1)

    # ── OLD REGIME 4-day: find Mondays, exit Thursday (apple-to-apple vs new) ─
    d = start_d
    while d < REGIME_CHANGE_DATE and d <= end_d:
        if d.weekday() != 0:   # Monday
            d += timedelta(days=1)
            continue

        expiry = d + timedelta(days=3)   # Thursday
        if expiry > end_d or expiry >= REGIME_CHANGE_DATE:
            d += timedelta(days=1)
            continue

        spot = _last_spot(spot_ref, d)
        if spot is None:
            logger.debug("OLD 4d: no spot for Monday {} — skip", d)
            d += timedelta(days=1)
            continue

        cycles.append({
            "entry_date":  d,
            "expiry_date": expiry,
            "entry_spot":  spot,
            "atm_strike":  _round_to_nearest_50(spot),
            "regime":      "thu_expiry_4day",
        })
        d += timedelta(days=1)

    # ── NEW REGIME: find Tuesdays as expiry, entry = prev Thursday ────────────
    d = max(start_d, REGIME_CHANGE_DATE)
    while d <= end_d:
        if d.weekday() != 1:   # Tuesday (expiry day in new regime)
            d += timedelta(days=1)
            continue

        entry = d - timedelta(days=5)   # Thursday of previous week
        if entry < start_d:
            d += timedelta(days=1)
            continue

        spot = _last_spot(spot_ref, entry)
        if spot is None:
            logger.debug("NEW: no spot for Thursday {} — skip", entry)
            d += timedelta(days=1)
            continue

        cycles.append({
            "entry_date":  entry,
            "expiry_date": d,
            "entry_spot":  spot,
            "atm_strike":  _round_to_nearest_50(spot),
            "regime":      "tue_expiry",
        })
        d += timedelta(days=1)

    old_n = sum(1 for c in cycles if c["regime"] == "thu_expiry")
    new_n = sum(1 for c in cycles if c["regime"] == "tue_expiry")
    logger.info(
        "Identified {} cycles: {} thu_expiry (pre-Sep25) + {} tue_expiry (new regime)",
        len(cycles), old_n, new_n,
    )
    return cycles


# ── Build weekly parquet ──────────────────────────────────────────────────────

def _target_strikes(atm: int) -> list[int]:
    """Return the 5 absolute strike values: ATM-100, ATM-50, ATM, ATM+50, ATM+100."""
    return [atm - 2 * STRIKE_STEP, atm - STRIKE_STEP, atm, atm + STRIKE_STEP, atm + 2 * STRIKE_STEP]


def build_cycle_parquet(cycle: dict, all_data: pd.DataFrame) -> bool:
    """
    Build and save one parquet file for a weekly cycle.
    File: data/options/weekly/YYYY-MM-DD_{regime}.parquet  (keyed by expiry date)
    Returns True if written, False if insufficient data.
    """
    entry_date  = cycle["entry_date"]
    expiry_date = cycle["expiry_date"]
    atm         = cycle["atm_strike"]
    regime      = cycle["regime"]
    targets     = _target_strikes(atm)

    # Date window depends on regime:
    #   thu_expiry: Tue, Wed, Thu   (2 trading sessions)
    #   tue_expiry: Thu, Fri, Mon, Tue  (4 trading sessions)
    if regime == "thu_expiry":
        wed = entry_date + timedelta(days=1)
        window_dates = {entry_date, wed, expiry_date}
    elif regime == "thu_expiry_4day":
        # Mon → Tue → Wed → Thu
        tue = entry_date + timedelta(days=1)
        wed = entry_date + timedelta(days=2)
        window_dates = {entry_date, tue, wed, expiry_date}
    else:  # tue_expiry
        fri = entry_date + timedelta(days=1)
        mon = expiry_date - timedelta(days=1)
        window_dates = {entry_date, fri, mon, expiry_date}

    # Filter all_data to this 3-day window and target absolute strikes
    mask = (
        all_data["date"].isin(window_dates) &
        all_data["strike"].isin(targets)
    )
    week_df = all_data[mask].copy()

    if week_df.empty:
        logger.warning("No data for cycle {} (ATM={})", expiry_date, atm)
        return False

    # Add strategy metadata columns
    week_df["expiry_date"]  = expiry_date
    week_df["entry_date"]   = entry_date
    week_df["entry_spot"]   = cycle["entry_spot"]
    week_df["atm_strike"]   = atm
    week_df["regime"]        = regime
    week_df["strike_offset"] = week_df["strike"] - atm   # -100, -50, 0, +50, +100

    # Ladder membership flags
    week_df["in_1L"] = week_df["strike_offset"] == 0
    week_df["in_3L"] = week_df["strike_offset"].abs() <= STRIKE_STEP
    week_df["in_5L"] = week_df["strike_offset"].abs() <= 2 * STRIKE_STEP   # all 5 strikes

    # Keep useful columns only
    keep = [
        "expiry_date", "entry_date", "entry_spot", "atm_strike", "regime",
        "strike", "strike_offset", "option_type",
        "timestamp", "date",
        "open", "high", "low", "close", "volume", "oi", "iv", "spot",
        "in_1L", "in_3L", "in_5L",
    ]
    week_df = week_df[[c for c in keep if c in week_df.columns]]

    out_path = settings.weekly_dir / f"{expiry_date}_{regime}.parquet"
    week_df.to_parquet(out_path, index=False)
    logger.debug("Written {} | {} bars | ATM={}", out_path.name, len(week_df), atm)
    return True


def build_all(start: str, end: str) -> int:
    """
    Load raw data, identify cycles, write one parquet per expiry.
    Returns number of parquet files written.
    """
    all_data = load_all_streams()
    if all_data.empty:
        return 0

    cycles  = identify_weekly_cycles(all_data, start, end)
    written = 0
    skipped = 0

    for cycle in cycles:
        out = settings.weekly_dir / f"{cycle['expiry_date']}_{cycle['regime']}.parquet"
        if out.exists():
            skipped += 1
            continue
        if build_cycle_parquet(cycle, all_data):
            written += 1

    logger.success(
        "BUILD COMPLETE | {} written, {} already existed | weekly/ dir: {}",
        written, skipped, settings.weekly_dir,
    )
    return written
