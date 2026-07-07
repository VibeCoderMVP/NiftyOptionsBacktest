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
      Run all 6 configurations (1L/3L/5L x BUY/SELL) across all weekly cycles.
      Saves data/backtest_results.parquet and data/backtest_summary.csv.
      Prints a summary table.

  uv run python pipeline.py all [--start 2023-01-01] [--end 2026-06-26]
      Runs: fetch -> build -> validate -> backtest in sequence.

  uv run python pipeline.py query "<SQL>"
      Run a DuckDB SQL query against the backtest results.
      Views available: 'results' (per-cycle P&L), 'options' (all bar data).

  uv run python pipeline.py signal [--force]
      Run entry signal for today (Thursday in new regime).
      Fetches Nifty spot, computes ATM, prints order slip, sends Telegram alert.
      Saves ATM/expiry to data/.last_signal.json for paper-entry.
      --force: run even if today is not Thursday (for testing).

  uv run python pipeline.py paper-entry <spot> <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>
      Log a paper trade entry after manually recording LTPs.
      spot  = Nifty spot at time of entry
      ltp1..6 in order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE
      ATM is read from data/.last_signal.json (run 'signal' first).

  uv run python pipeline.py paper-exit <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>
      Log exit LTPs for the last open paper trade and compute P&L.
      Same leg order as paper-entry.

  uv run python pipeline.py paper-show
      Print the full paper trade journal as a Rich table.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console

load_dotenv()

console = Console()

ALL_COMMANDS = [
    "test-api", "fetch", "build", "validate", "backtest", "all", "query",
    "signal", "paper-entry", "paper-exit", "paper-show",
]


# ── Argument parsing ──────────────────────────────────────────────────────────

def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Nifty Options Ladder Backtest Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", choices=ALL_COMMANDS, help="Pipeline step to run")
    p.add_argument("--start",  default="2023-01-01", help="Backtest start date YYYY-MM-DD")
    p.add_argument("--end",    default="",           help="Backtest end date YYYY-MM-DD (default: today)")
    p.add_argument("--force",  action="store_true",  help="Re-fetch cached data / override day check")
    p.add_argument("--spot",   type=float, default=None, help="Override Nifty spot price for signal (skip API fetch)")
    p.add_argument(
        "--entry-date", default=None, dest="entry_date",
        help="Override entry date for paper-entry (YYYY-MM-DD, default: today). "
             "Useful when backfilling Thursday's trade on a later day.",
    )
    p.add_argument(
        "extra_args", nargs="*",
        help="Extra positional args: SQL query string, or LTP values for paper commands",
    )
    return p


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_test_api() -> None:
    """Fetch 1 week of ATM CE data and print a few rows. Use to verify credentials."""
    from src.fetcher import fetch_rolling_option

    console.print("[bold]Testing Dhan /charts/rollingoption API...[/bold]")
    resp = fetch_rolling_option(
        relative_strike="ATM",
        option_type="CALL",
        from_date="2024-01-02",
        to_date="2024-01-05",
        interval=5,
    )
    if resp is None:
        console.print("[red]API call failed. Check credentials in .env and review logs.[/red]")
        sys.exit(1)

    data = resp.get("data", {})
    console.print("[green]API call succeeded.[/green]")
    console.print(f"Response keys in 'data': {list(data.keys()) if isinstance(data, dict) else type(data)}")

    ce     = data.get("ce", {}) if isinstance(data, dict) else {}
    keys   = list(ce.keys())
    n      = len(ce[keys[0]]) if keys else 0
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
    console.print("  Streams: 9 relative strikes x 2 option types = 18 total")
    console.print("  Chunks:  ~30-day windows per stream")
    console.print("  Cache:   data/options/raw/  (--force to re-fetch all)")
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
    console.print("Per-cycle detail  -> data/backtest_results.parquet")
    console.print("Summary           -> data/backtest_summary.csv")
    console.print('\nFor ad-hoc queries: uv run python pipeline.py query "<SQL>"')


def cmd_query(extra_args: list[str]) -> None:
    sql = " ".join(extra_args) if extra_args else ""
    if not sql:
        console.print('[red]Provide a SQL string: pipeline.py query "SELECT ..."[/red]')
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


def cmd_signal(force: bool, spot: float | None = None) -> None:
    from src.signal import run_signal
    run_signal(force=force, spot=spot)


def cmd_paper_entry(extra_args: list[str], entry_date_str: str | None = None) -> None:
    """
    paper-entry <spot> <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>

    ATM and expiry are read from data/.last_signal.json (run 'signal' first).
    Use --entry-date YYYY-MM-DD to backfill a Thursday trade logged on a later day.
    """
    from src.signal import load_last_signal
    from src.paper_trade import log_entry

    sig = load_last_signal()
    if sig is None:
        console.print("[red]No saved signal found. Run 'signal' first to set ATM.[/red]")
        sys.exit(1)

    if len(extra_args) < 7:
        console.print(
            "[red]Usage: paper-entry <spot> <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>[/red]\n"
            "[dim]LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE[/dim]"
        )
        sys.exit(1)

    try:
        spot     = float(extra_args[0])
        leg_ltps = [float(x) for x in extra_args[1:7]]
    except ValueError:
        console.print("[red]All values must be numbers.[/red]")
        sys.exit(1)

    entry_date = None
    if entry_date_str:
        try:
            entry_date = date.fromisoformat(entry_date_str)
        except ValueError:
            console.print(f"[red]Invalid --entry-date '{entry_date_str}'. Use YYYY-MM-DD.[/red]")
            sys.exit(1)

    try:
        log_entry(
            atm        = sig["atm"],
            legs_ltps  = leg_ltps,
            entry_spot = spot,
            entry_date = entry_date,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


def _get_open_record() -> dict | None:
    import json
    from pathlib import Path
    journal = Path("data/options_journal.jsonl")
    if not journal.exists():
        return None
    open_rec = None
    for line in journal.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                r = json.loads(line)
                if r.get("outcome") is None:
                    open_rec = r
            except json.JSONDecodeError:
                pass
    return open_rec


def _check_exit_timing() -> bool:
    """
    Check whether it is the right time to exit.
    Returns True if the caller should proceed with the exit immediately.
    Returns False if the caller should abort (user chose not to exit early
    and the auto-wait path timed out / was not applicable from CLI).

    Behaviour:
      - Before expiry day: asks Y/N. Y = exit now. N = abort (use ET or wait).
      - On expiry day before 15:20: asks Y/N. N = block until 15:20 then return True.
      - On expiry day at/after 15:20: proceed immediately (no prompt).
    """
    import time as _time
    from datetime import datetime, timedelta, timezone

    _IST = timedelta(hours=5, minutes=30)
    now_ist = datetime.now(timezone.utc) + _IST
    open_rec = _get_open_record()
    if open_rec is None:
        return True   # no open record — let log_exit handle the error

    expiry_str = open_rec.get("expiry_date", "")
    try:
        expiry_date_obj = date.fromisoformat(expiry_str)
    except (ValueError, TypeError):
        return True   # can't parse — proceed

    days_to_exp = (expiry_date_obj - now_ist.date()).days

    if days_to_exp > 0:
        # Before expiry day — definitely early
        console.print(
            f"\n[bold yellow]Early exit:[/bold yellow] Expiry is {expiry_str} "
            f"({days_to_exp} day(s) away). Closing now forfeits remaining time decay."
        )
        try:
            answer = input("Exit early? (Y/N): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            answer = "N"
        if answer != "Y":
            console.print("[dim]Aborted. Position remains open.[/dim]")
            return False
        return True

    elif days_to_exp == 0:
        # Expiry day — check time
        target = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
        if now_ist < target:
            remaining = (target - now_ist).total_seconds()
            mins, secs = divmod(int(remaining), 60)
            console.print(
                f"\n[bold yellow]Expiry day — exit window opens at 15:20 IST.[/bold yellow] "
                f"Current time: {now_ist.strftime('%H:%M')} IST "
                f"({mins}m {secs}s remaining)."
            )
            console.print(
                "[dim]Closing before 15:20 leaves time value on the table and carries "
                "pin risk through the close.[/dim]"
            )
            try:
                answer = input("Exit early anyway? (Y/N): ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                answer = "N"
            if answer == "Y":
                return True
            # N = wait until 15:20 and then auto-exit
            console.print(
                "[cyan]Waiting for 15:20 IST to auto-exit...[/cyan] "
                "Press Ctrl+C to abort."
            )
            try:
                while True:
                    now_ist = datetime.now(timezone.utc) + _IST
                    target  = now_ist.replace(hour=15, minute=20, second=0, microsecond=0)
                    remaining = (target - now_ist).total_seconds()
                    if remaining <= 0:
                        break
                    mins, secs = divmod(int(remaining), 60)
                    console.print(
                        f"\r[dim]  Auto-exit at 15:20 — {mins:02d}:{secs:02d} remaining...[/dim]",
                        end="",
                    )
                    _time.sleep(5)
                console.print()
            except KeyboardInterrupt:
                console.print("\n[red]Aborted by user. Position remains open.[/red]")
                return False
            return True
        else:
            # At or past 15:20 — proceed immediately
            return True

    else:
        # Past expiry (days_to_exp < 0) — something is wrong
        console.print(
            f"[yellow]Note: Expiry date {expiry_str} is in the past. Proceeding.[/yellow]"
        )
        return True


def cmd_paper_exit(extra_args: list[str]) -> None:
    """
    paper-exit <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>
    """
    from src.paper_trade import log_exit

    if not _check_exit_timing():
        return

    if len(extra_args) < 6:
        console.print(
            "[red]Usage: paper-exit <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>[/red]\n"
            "[dim]LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE[/dim]"
        )
        sys.exit(1)

    try:
        exit_ltps = [float(x) for x in extra_args[:6]]
    except ValueError:
        console.print("[red]All values must be numbers.[/red]")
        sys.exit(1)

    try:
        log_exit(exit_ltps)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)


def cmd_paper_show() -> None:
    from src.paper_trade import show_journal
    show_journal()


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
    extra  = args.extra_args or []

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
        cmd_query(extra)

    elif args.command == "signal":
        cmd_signal(force=args.force, spot=args.spot)

    elif args.command == "paper-entry":
        cmd_paper_entry(extra, entry_date_str=args.entry_date)

    elif args.command == "paper-exit":
        cmd_paper_exit(extra)

    elif args.command == "paper-show":
        cmd_paper_show()


if __name__ == "__main__":
    main()
