"""
Backtest engine: weekly Nifty options ladder.

Strategy logic:
  - Entry  : last 5-min bar of Tuesday  (the entry_date)
  - Exit   : last 5-min bar of Thursday (the expiry_date)
  - Side   : BUY  -> pay premium at entry, receive at exit
             SELL -> collect premium at entry, pay back at exit

Configurations tested:
  1L-BUY   : Buy  ATM CE + ATM PE  (long straddle)
  1L-SELL  : Sell ATM CE + ATM PE  (short straddle)
  3L-BUY   : Buy  (ATM±50 CE+PE + ATM CE+PE) — 6 legs
  3L-SELL  : Sell (ATM±50 CE+PE + ATM CE+PE) — 6 legs
  5L-BUY   : Buy  all 10 legs
  5L-SELL  : Sell all 10 legs

P&L is per-lot (one lot = NIFTY_LOT_SIZE shares = 75).
Brokerage and charges are shown separately at summary level as an estimate.

Results are saved to:
  data/backtest_results.parquet  — per-trade detail for DuckDB analysis
  data/backtest_summary.csv      — human-readable summary
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import NIFTY_LOT_SIZE, STRIKE_STEP, settings

console = Console()

CONFIGS = {
    "1L": "in_1L",
    "3L": "in_3L",
    "5L": "in_5L",
}
SIDES = ["BUY", "SELL"]

# Rough brokerage estimate per leg per lot (Rs — adjust as per your plan)
BROKERAGE_PER_LEG = 20.0


# ── Per-cycle P&L ─────────────────────────────────────────────────────────────

def _last_bar(df: pd.DataFrame, target_date) -> pd.DataFrame:
    """Return the last 5-min bar of each (strike, option_type) for a given date."""
    day = df[df["date"].astype(str) == str(target_date)]
    if day.empty:
        return pd.DataFrame()
    return (
        day.sort_values("timestamp")
        .groupby(["strike", "option_type"])
        .last()
        .reset_index()
    )


def compute_cycle_pnl(path: Path) -> dict | None:
    """
    Compute P&L for all 6 configurations for one weekly cycle.
    Returns a dict with one key per config-side, or None if data is insufficient.
    """
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        logger.warning("Could not read {}: {}", path.name, exc)
        return None

    expiry = str(df["expiry_date"].iloc[0])
    entry  = str(df["entry_date"].iloc[0])
    spot   = float(df["entry_spot"].iloc[0])
    atm    = int(df["atm_strike"].iloc[0])
    regime = str(df["regime"].iloc[0]) if "regime" in df.columns else "thu_expiry"

    entry_bars = _last_bar(df, entry)
    exit_bars  = _last_bar(df, expiry)

    # Holiday fallback: if Thursday expiry is a market holiday, NSE moves expiry to Wednesday
    if exit_bars.empty:
        wed = str(pd.Timestamp(entry).date() + pd.Timedelta(days=1))
        exit_bars = _last_bar(df, wed)
        if not exit_bars.empty:
            logger.info("Holiday expiry: using Wednesday {} exit for {}", wed, expiry)

    if entry_bars.empty or exit_bars.empty:
        logger.warning("Missing entry or exit bars for {}", expiry)
        return None

    # Merge entry and exit prices on (strike, option_type)
    merged = entry_bars[["strike", "option_type", "close", "in_1L", "in_3L", "in_5L"]].rename(
        columns={"close": "entry_price"}
    ).merge(
        exit_bars[["strike", "option_type", "close"]].rename(columns={"close": "exit_price"}),
        on=["strike", "option_type"],
        how="inner",
    )

    if merged.empty:
        return None

    result: dict = {
        "expiry_date": expiry,
        "entry_date":  entry,
        "entry_spot":  spot,
        "atm_strike":  atm,
        "regime":      regime,
        "n_legs_with_data": len(merged),
    }

    for config, col in CONFIGS.items():
        if col not in merged.columns:
            continue
        legs = merged[merged[col] == True].copy()
        if legs.empty:
            continue

        n_legs        = len(legs)
        total_premium = float(legs["entry_price"].sum())   # total premium collected/paid

        for side in SIDES:
            if side == "SELL":
                # Sell: collect entry premium, pay exit premium
                leg_pnl = legs["entry_price"] - legs["exit_price"]
            else:
                # Buy: pay entry premium, receive exit premium
                leg_pnl = legs["exit_price"] - legs["entry_price"]

            raw_pnl_pts  = float(leg_pnl.sum())                    # points (premium units)
            pnl_rupees   = raw_pnl_pts * NIFTY_LOT_SIZE            # × lot size
            brokerage    = BROKERAGE_PER_LEG * n_legs              # per lot estimate
            net_pnl      = pnl_rupees - brokerage

            key = f"{config}_{side}"
            result[f"{key}_pnl_pts"]   = round(raw_pnl_pts, 2)
            result[f"{key}_pnl_rs"]    = round(pnl_rupees,  2)
            result[f"{key}_net_rs"]    = round(net_pnl,     2)
            result[f"{key}_n_legs"]    = n_legs
            result[f"{key}_premium"]   = round(total_premium, 2)   # entry premium per lot

    return result


# ── Run across all cycles ─────────────────────────────────────────────────────

def run_backtest() -> pd.DataFrame:
    """
    Run all 6 configurations across every weekly parquet.
    Returns a DataFrame of per-cycle results (includes 'regime' column).
    """
    files = sorted(settings.weekly_dir.glob("*.parquet"))
    if not files:
        logger.error("No weekly parquet files found. Run 'build' step first.")
        return pd.DataFrame()

    rows = []
    for f in files:
        r = compute_cycle_pnl(f)
        if r:
            rows.append(r)

    if not rows:
        logger.error("No cycles produced results.")
        return pd.DataFrame()

    results = pd.DataFrame(rows).sort_values(["regime", "expiry_date"]).reset_index(drop=True)

    detail_path = settings.data_dir / "backtest_results.parquet"
    results.to_parquet(detail_path, index=False)
    logger.info("Per-cycle results saved -> {}", detail_path)

    return results


# ── Summary statistics ─────────────────────────────────────────────────────────

def _regime_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Compute summary stats for one regime's results DataFrame."""
    rows = []
    for config in CONFIGS:
        for side in SIDES:
            key = f"{config}_{side}"
            col = f"{key}_net_rs"
            if col not in df.columns:
                continue
            s = df[col].dropna()
            if s.empty:
                continue
            wins = (s > 0).sum()
            rows.append({
                "Config":        key,
                "N Trades":      len(s),
                "Win Rate %":    round(100 * wins / len(s), 1),
                "Avg P&L Rs":    round(s.mean(), 0),
                "Median P&L Rs": round(s.median(), 0),
                "Max Win Rs":    round(s.max(), 0),
                "Max Loss Rs":   round(s.min(), 0),
                "Total P&L Rs":  round(s.sum(), 0),
                "Avg Premium":   round(df[f"{key}_premium"].mean(), 1)
                                 if f"{key}_premium" in df.columns else None,
            })
    return pd.DataFrame(rows)


REGIME_META = {
    "thu_expiry":      ("pre_sep2025_2day",     "Pre-Sep 2025  |  Entry=Tue close  EXIT=Thu close  |  2-day hold"),
    "thu_expiry_4day": ("pre_sep2025_4day",     "Pre-Sep 2025  |  Entry=Mon close  EXIT=Thu close  |  4-day hold  [apple-to-apple vs new regime]"),
    "tue_expiry":      ("sep2025_onwards_4day",  "Sep 2025+     |  Entry=Thu close  EXIT=Tue close  |  4-day hold"),
}


def summary_stats(results: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """
    Compute summary statistics split by regime.
    Returns dict keyed by regime name. Saves per-regime CSVs.
    """
    summaries = {}
    col = "regime" if "regime" in results.columns else None
    for regime, (file_label, _) in REGIME_META.items():
        subset = results[results[col] == regime] if col else results
        if subset.empty:
            continue
        s = _regime_summary(subset)
        summaries[regime] = s
        csv_path = settings.data_dir / f"backtest_summary_{file_label}.csv"
        s.to_csv(csv_path, index=False)
        logger.info("Summary ({}) -> {}", regime, csv_path)
    return summaries


def _print_regime_table(summary: pd.DataFrame, title: str) -> None:
    table = Table(title=title, show_lines=True)
    for col in summary.columns:
        style = "cyan" if col == "Config" else "green" if "Win" in col else "white"
        table.add_column(col, style=style)
    for _, row in summary.iterrows():
        pnl = row.get("Total P&L Rs", 0) or 0
        style = "green" if pnl > 0 else "red"
        table.add_row(*[str(int(v) if isinstance(v, float) and v == int(v) else v) for v in row], style=style)
    console.print()
    console.print(table)


def print_summary(summaries: dict[str, pd.DataFrame]) -> None:
    """Print one Rich table per regime in logical order."""
    order = ["thu_expiry", "thu_expiry_4day", "tue_expiry"]
    for regime in order:
        if regime in summaries:
            _, title = REGIME_META[regime]
            _print_regime_table(summaries[regime], title)
    console.print()
    console.print(
        f"[dim]P&L per lot ({NIFTY_LOT_SIZE} shares). "
        f"Brokerage estimate: Rs{BROKERAGE_PER_LEG}/leg/lot.[/dim]"
    )
    console.print()


# ── DuckDB ad-hoc analysis helpers ───────────────────────────────────────────

def open_duckdb() -> duckdb.DuckDBPyConnection:
    """
    Return a DuckDB connection with the weekly parquet files registered
    as a view 'options' and backtest results as 'results'.

    Example queries:
        con.execute("SELECT * FROM results WHERE '5L_SELL_net_rs' > 5000").df()
        con.execute("SELECT strftime(expiry_date, '%Y-%m') as month, AVG(\"1L_SELL_net_rs\") FROM results GROUP BY 1").df()
        con.execute("SELECT * FROM options WHERE expiry_date='2024-06-27' AND option_type='CALL'").df()
    """
    con = duckdb.connect()

    weekly_pattern = str(settings.weekly_dir / "*.parquet")
    results_path   = str(settings.data_dir / "backtest_results.parquet")

    if list(settings.weekly_dir.glob("*.parquet")):
        con.execute(f"CREATE VIEW options AS SELECT * FROM read_parquet('{weekly_pattern}')")
    if (settings.data_dir / "backtest_results.parquet").exists():
        con.execute(f"CREATE VIEW results AS SELECT * FROM read_parquet('{results_path}')")

    return con
