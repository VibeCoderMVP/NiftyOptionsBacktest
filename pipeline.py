"""
Nifty Weekly Options Ladder Backtest — Pipeline

Usage (always run from D:\\Trading\\NiftyOptionsBacktest\\):

  uv run python pipeline.py test-api
      Quick sanity-check: fetches 1 week of ATM CE data and prints a sample.
      Run this FIRST to confirm credentials and API response format.

  uv run python pipeline.py fetch [--start 2023-01-01] [--end 2026-06-26] [--force]
      Download all 18 option streams from Dhan API.
      Cached: safe to re-run; only fetches missing chunks (unless --force).
      One-time run ~6 minutes at default rate limit.

  uv run python pipeline.py build [--start 2023-01-01] [--end 2026-06-26]
      Parse raw JSON -> one parquet per weekly expiry in data/options/weekly/.
      Safe to re-run: skips existing files.

  uv run python pipeline.py validate
      Check every parquet for complete 10-strike coverage.
      Prints a Rich table of any gaps.

  uv run python pipeline.py backtest
      Run all 6 configurations (1L/3L/5L × BUY/SELL) across all weekly cycles.
      Saves data/backtest_results.parquet and data/backtest_summary.csv.
      Prints a summary table.

  uv run python pipeline.py all [--start 2023-01-01] [--end 2026-06-26]
      Runs: fetch -> build -> validate -> backtest in sequence.

  uv run python pipeline.py query "<SQL>"
      Run a DuckDB SQL query against the backtest results.
      Views available: 'results' (per-cycle P&L), 'options' (all bar data).

      Examples:
        uv run python pipeline.py query "SELECT Config, [Total P&L ₹] FROM summary"
        uv run python pipeline.py query "SELECT expiry_date, 5L_SELL_net_rs FROM results ORDER BY 1"
        uv run python pipeline.py query "SELECT strftime(expiry_date,'%Y-%m') as month, AVG(\\"1L_SELL_net_rs\\") as avg_pnl FROM results GROUP BY 1 ORDER BY 1"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console

load_dotenv()

console = Console()


# ── Argument parsing ──────────────────────────────────────────────────────────

def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Nifty Options Ladder Backtest Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "command",
        choices=["test-api", "fetch", "build", "validate", "backtest", "all", "query"],
        help="Pipeline step to run",
    )
    p.add_argument("--start",  default="2023-01-01", help="Backtest start date YYYY-MM-DD")
    p.add_argument("--end",    default="",           help="Backtest end date YYYY-MM-DD (default: today)")
    p.add_argument("--force",  action="store_true",  help="Re-fetch even if cached")
    p.add_argument("sql",      nargs="?",            help="SQL query (for 'query' command)")
    return p


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_test_api() -> None:
    """Fetch 1 week of ATM CE data and print a few rows. Use to verify credentials."""
    from src.fetcher import fetch_rolling_option

    console.print("[bold]Testing Dhan /charts/rollingoption API...[/bold]")
    resp = fetch_rolling_option(
        relative_strike="ATM",
        from_date="2024-01-02",
        to_date="2024-01-05",   # 4 days — one expiry week
        interval=5,
    )
    if resp is None:
        console.print("[red]API call failed. Check credentials in .env and review logs.[/red]")
        sys.exit(1)

    data = resp.get("data", {})
    console.print(f"[green]API call succeeded.[/green]")
    console.print(f"Response keys in 'data': {list(data.keys()) if isinstance(data, dict) else type(data)}")

    # Print first 3 bars of the CE side to show structure
    ce = data.get("ce", {}) if isinstance(data, dict) else {}
    keys = list(ce.keys())
    n    = len(ce[keys[0]]) if keys else 0
    sample = [{k: ce[k][i] for k in keys} for i in range(min(3, n))]

    console.print(f"\n[bold]CE side keys:[/bold] {keys}")
    console.print(f"[bold]Total CE bars:[/bold] {n}")
    console.print("\n[bold]First 3 CE bars:[/bold]")
    for row in sample:
        console.print(f"  {row}")

    console.print(
        "\n[dim]If you see timestamps, OHLC values, spot, and strike columns above — "
        "the API is working correctly. Proceed with 'fetch'.[/dim]"
    )


def cmd_fetch(start: str, end: str, force: bool) -> None:
    from src.fetcher import fetch_all_streams
    effective_end = end or date.today().strftime("%Y-%m-%d")
    console.print(f"[bold]Fetching options data: {start} -> {effective_end}[/bold]")
    console.print(f"  Streams: 9 relative strikes x 2 option types = 18 total")
    console.print(f"  Chunks:  ~30-day windows per stream")
    console.print(f"  Cache:   data/options/raw/  (--force to re-fetch all)")
    console.print()
    fetch_all_streams(start, effective_end, force=force)


def cmd_build(start: str, end: str) -> None:
    from src.builder import build_all
    effective_end = end or date.today().strftime("%Y-%m-%d")
    console.print(f"[bold]Building weekly parquet files: {start} -> {effective_end}[/bold]")
    n = build_all(start, effective_end)
    console.print(f"[green]{n} new parquet files written to data/options/weekly/[/green]")


def cmd_validate() -> None:
    from src.validator import validate_all
    console.print("[bold]Validating weekly parquet coverage...[/bold]")
    summary = validate_all()
    if summary:
        console.print(f"Complete: {summary['complete']}/{summary['total']} cycles")


def cmd_backtest() -> None:
    from src.backtest import print_summary, run_backtest, summary_stats
    console.print("[bold]Running backtest across all weekly cycles...[/bold]")
    results = run_backtest()
    if results.empty:
        console.print("[red]No results — check that build step completed.[/red]")
        return
    summary = summary_stats(results)
    print_summary(summary)
    console.print(f"Per-cycle detail  -> data/backtest_results.parquet")
    console.print(f"Summary           -> data/backtest_summary.csv")
    console.print(f"\nFor ad-hoc queries: uv run python pipeline.py query \"<SQL>\"")


def cmd_query(sql: str | None) -> None:
    if not sql:
        console.print("[red]Provide a SQL string: pipeline.py query \"SELECT ...\"[/red]")
        sys.exit(1)
    from src.backtest import open_duckdb
    con = open_duckdb()
    try:
        df = con.execute(sql).df()
        console.print(df.to_string(index=False))
    except Exception as exc:
        console.print(f"[red]Query error: {exc}[/red]")
        console.print("[dim]Available views: 'options' (bar data), 'results' (per-cycle P&L)[/dim]")
    finally:
        con.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
        level="INFO",
    )
    logger.add(
        "data/pipeline.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )

    parser = _make_parser()
    args   = parser.parse_args()
    start  = args.start
    end    = args.end or date.today().strftime("%Y-%m-%d")

    if args.command == "test-api":
        cmd_test_api()

    elif args.command == "fetch":
        cmd_fetch(start, end, args.force)

    elif args.command == "build":
        cmd_build(start, end)

    elif args.command == "validate":
        cmd_validate()

    elif args.command == "backtest":
        cmd_backtest()

    elif args.command == "all":
        console.print(f"[bold]Running full pipeline: {start} -> {end}[/bold]\n")
        cmd_fetch(start, end, args.force)
        console.print()
        cmd_build(start, end)
        console.print()
        cmd_validate()
        console.print()
        cmd_backtest()

    elif args.command == "query":
        cmd_query(args.sql)


if __name__ == "__main__":
    main()
