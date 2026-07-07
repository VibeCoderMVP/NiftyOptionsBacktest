"""
Validates weekly parquet coverage and prints a report.

Checks:
  1. Each parquet file has data for all 10 strikes (5 absolute strikes × CE+PE)
  2. Each file has at least a Tuesday entry bar and a Thursday exit bar
  3. Reports gaps, thin weeks, and missing strikes as a Rich table
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import OPTION_TYPES, settings

console = Console()

EXPECTED_STRIKES_PER_SIDE = 5        # ATM-100, -50, 0, +50, +100
EXPECTED_LEGS             = EXPECTED_STRIKES_PER_SIDE * len(OPTION_TYPES)   # 10
MIN_BARS_PER_CYCLE        = 20       # ~1 hour of 5-min bars minimum to call the data usable


def validate_all() -> dict:
    """
    Validate every weekly parquet.
    Returns summary dict: {total, complete, incomplete, missing_legs, thin_data}
    """
    files = sorted(settings.weekly_dir.glob("*.parquet"))
    if not files:
        logger.error("No weekly parquet files found. Run 'build' step first.")
        return {}

    results = []
    for f in files:
        result = _validate_one(f)
        results.append(result)

    df = pd.DataFrame(results)
    _print_report(df, files)
    return {
        "total":       len(df),
        "complete":    int((df["legs_found"] == EXPECTED_LEGS).sum()),
        "incomplete":  int((df["legs_found"] < EXPECTED_LEGS).sum()),
        "missing_legs": int(df["missing_legs"].sum()),
        "thin_data":   int((df["total_bars"] < MIN_BARS_PER_CYCLE).sum()),
    }


def _validate_one(path: Path) -> dict:
    expiry = path.stem
    try:
        df = pd.read_parquet(path)
        atm = int(df["atm_strike"].iloc[0]) if "atm_strike" in df.columns else 0
        entry = str(df["entry_date"].iloc[0])  if "entry_date"  in df.columns else "?"
        spot  = float(df["entry_spot"].iloc[0]) if "entry_spot" in df.columns else 0.0

        legs_found = df.groupby(["strike", "option_type"]).ngroups
        missing    = max(0, EXPECTED_LEGS - legs_found)

        # Check Tuesday entry bars exist
        has_entry = bool((df["date"].astype(str) == entry).any()) if "date" in df.columns else False

        # Check Thursday exit bars exist
        has_exit = bool((df["date"].astype(str) == expiry).any()) if "date" in df.columns else False

        return {
            "expiry":       expiry,
            "entry":        entry,
            "entry_spot":   round(spot, 1),
            "atm":          atm,
            "legs_found":   legs_found,
            "missing_legs": missing,
            "total_bars":   len(df),
            "has_entry":    has_entry,
            "has_exit":     has_exit,
            "ok":           (legs_found == EXPECTED_LEGS and has_entry and has_exit),
        }
    except Exception as exc:
        return {
            "expiry": expiry, "entry": "?", "entry_spot": 0, "atm": 0,
            "legs_found": 0, "missing_legs": EXPECTED_LEGS,
            "total_bars": 0, "has_entry": False, "has_exit": False,
            "ok": False, "error": str(exc),
        }


def _print_report(df: pd.DataFrame, files: list[Path]) -> None:
    complete   = int((df["legs_found"] == EXPECTED_LEGS).sum())
    incomplete = len(df) - complete
    thin       = int((df["total_bars"] < MIN_BARS_PER_CYCLE).sum())

    console.print("\n[bold]Options Data Coverage Report[/bold]")
    console.print(f"  Total weekly cycles : {len(df)}")
    console.print(f"  Complete (10 legs)  : [green]{complete}[/green]")
    console.print(f"  Incomplete          : [red]{incomplete}[/red]")
    console.print(f"  Thin (<{MIN_BARS_PER_CYCLE} bars)     : [yellow]{thin}[/yellow]")
    console.print()

    # Show problematic weeks
    bad = df[~df["ok"]]
    if bad.empty:
        console.print("[green]All cycles have complete data.[/green]\n")
        return

    table = Table(title="Incomplete Cycles", show_lines=True)
    table.add_column("Expiry",   style="cyan")
    table.add_column("Entry",    style="white")
    table.add_column("Spot",     style="white")
    table.add_column("ATM",      style="white")
    table.add_column("Legs",     style="yellow")
    table.add_column("Bars",     style="white")
    table.add_column("Has Tue",  style="white")
    table.add_column("Has Thu",  style="white")

    for _, row in bad.iterrows():
        table.add_row(
            str(row["expiry"]),
            str(row["entry"]),
            str(row["entry_spot"]),
            str(row["atm"]),
            f"{row['legs_found']}/{EXPECTED_LEGS}",
            str(row["total_bars"]),
            "[green]Y[/green]" if row["has_entry"] else "[red]N[/red]",
            "[green]Y[/green]" if row["has_exit"]  else "[red]N[/red]",
        )
    console.print(table)
    console.print()
