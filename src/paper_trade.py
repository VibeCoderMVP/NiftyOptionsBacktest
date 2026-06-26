"""
Paper trade journal for Nifty weekly options.

Records simulated trades to data/options_journal.jsonl for validation
before going live. Each record is one JSON object per line.

Usage (via pipeline.py):
  uv run python pipeline.py paper-entry 85.40 102.20 112.80 98.60 88.10 112.50
  uv run python pipeline.py paper-exit  2.10  8.30   5.20  12.10  1.80   6.40
  uv run python pipeline.py paper-show
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import NIFTY_LOT_SIZE, settings

console = Console()

_IST             = timedelta(hours=5, minutes=30)
BROKERAGE_PER_LEG = 20.0   # Rs per leg per lot (entry OR exit)


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + _IST


def _journal_path() -> Path:
    p = Path(settings.data_dir) / "options_journal.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_journal() -> list[dict]:
    path = _journal_path()
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _save_journal(records: list[dict]) -> None:
    _journal_path().write_text(
        "\n".join(json.dumps(r, default=str) for r in records) + "\n",
        encoding="utf-8",
    )


def _next_tuesday(from_date: date) -> date:
    """Given a Thursday entry date, return the following Tuesday."""
    days = (1 - from_date.weekday()) % 7 or 7
    return from_date + timedelta(days=days)


def log_entry(
    atm: int,
    legs_ltps: list[float],
    entry_spot: float,
    entry_date: date | None = None,
    lots: int = 1,
    regime: str = "tue_expiry",
) -> None:
    """
    Log a new paper trade entry.

    legs_ltps: 6 floats in order —
      [ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE]
    """
    if len(legs_ltps) != 6:
        console.print(f"[red]Expected 6 LTP values, got {len(legs_ltps)}.[/red]")
        return

    entry_date  = entry_date or _ist_now().date()
    expiry_date = _next_tuesday(entry_date)
    entry_time  = _ist_now().strftime("%Y-%m-%d %H:%M")
    lot_size    = NIFTY_LOT_SIZE

    strikes = [atm - 50, atm - 50, atm, atm, atm + 50, atm + 50]
    types   = ["CE", "PE", "CE", "PE", "CE", "PE"]

    legs: list[dict[str, Any]] = [
        {
            "strike":    strikes[i],
            "type":      types[i],
            "entry_ltp": legs_ltps[i],
            "exit_ltp":  None,
            "exit_time": None,
        }
        for i in range(6)
    ]

    total_entry = sum(legs_ltps)

    record: dict[str, Any] = {
        "regime":              regime,
        "entry_date":          str(entry_date),
        "expiry_date":         str(expiry_date),
        "entry_time":          entry_time,
        "entry_spot":          entry_spot,
        "atm_strike":          atm,
        "lot_size":            lot_size,
        "lots":                lots,
        "legs":                legs,
        "total_entry_premium": round(total_entry, 2),
        "total_exit_premium":  None,
        "gross_pnl_pts":       None,
        "gross_pnl_rs":        None,
        "net_pnl_rs":          None,
        "outcome":             None,
        "paper_trade":         True,
    }

    records = _load_journal()
    records.append(record)
    _save_journal(records)

    rs_collected = total_entry * lot_size * lots
    console.print(f"[green]Paper entry logged.[/green]")
    console.print(
        f"  ATM: {atm}  |  Expiry: {expiry_date}  |  "
        f"Entry premium: {total_entry:.2f} pts = Rs {rs_collected:.0f}"
    )
    console.print(
        f"[dim]On expiry day ({expiry_date}), run: "
        f"uv run python pipeline.py paper-exit <6 LTPs>[/dim]"
    )
    logger.info(
        "Paper entry | ATM={} expiry={} premium_pts={:.2f} Rs_collected={:.0f}",
        atm, expiry_date, total_entry, rs_collected,
    )


def log_exit(exit_ltps: list[float], exit_time: str | None = None) -> None:
    """
    Close the last open paper trade with exit LTPs (same order as entry).
    """
    if len(exit_ltps) != 6:
        console.print(f"[red]Expected 6 exit LTP values, got {len(exit_ltps)}.[/red]")
        return

    records = _load_journal()
    open_idx = next(
        (i for i in reversed(range(len(records))) if records[i].get("outcome") is None),
        None,
    )
    if open_idx is None:
        console.print("[red]No open paper trade found. Log an entry first.[/red]")
        return

    rec      = records[open_idx]
    exit_t   = exit_time or _ist_now().strftime("%Y-%m-%d %H:%M")
    lot_size = rec.get("lot_size", NIFTY_LOT_SIZE)
    lots     = rec.get("lots", 1)

    for i, leg in enumerate(rec["legs"]):
        leg["exit_ltp"]  = exit_ltps[i]
        leg["exit_time"] = exit_t

    total_exit  = sum(exit_ltps)
    pnl_pts     = rec["total_entry_premium"] - total_exit   # SELL: profit when price falls
    pnl_rs      = pnl_pts * lot_size * lots
    brokerage   = BROKERAGE_PER_LEG * 6 * lots * 2          # entry + exit, 6 legs each
    net_pnl_rs  = pnl_rs - brokerage

    rec.update({
        "total_exit_premium": round(total_exit, 2),
        "gross_pnl_pts":      round(pnl_pts, 2),
        "gross_pnl_rs":       round(pnl_rs, 2),
        "net_pnl_rs":         round(net_pnl_rs, 2),
        "outcome":            "WIN" if net_pnl_rs > 0 else "LOSS",
    })

    records[open_idx] = rec
    _save_journal(records)

    color = "green" if net_pnl_rs > 0 else "red"
    console.print(f"[{color}]Paper exit logged. Outcome: {rec['outcome']}[/{color}]")
    console.print(f"  Entry premium : {rec['total_entry_premium']:.2f} pts")
    console.print(f"  Exit premium  : {total_exit:.2f} pts")
    console.print(f"  Gross P&L     : {pnl_pts:+.2f} pts = Rs {pnl_rs:+.0f}")
    console.print(
        f"  Brokerage     : Rs {brokerage:.0f} "
        f"(Rs {BROKERAGE_PER_LEG:.0f}/leg x 6 legs x 2 sides)"
    )
    console.print(f"  [bold]Net P&L       : Rs {net_pnl_rs:+.0f}[/bold]")
    logger.info(
        "Paper exit | outcome={} net_pnl_rs={:.0f} pnl_pts={:.2f}",
        rec["outcome"], net_pnl_rs, pnl_pts,
    )


def show_journal() -> None:
    records = _load_journal()
    if not records:
        console.print("[yellow]No paper trades recorded yet. Run 'signal' then 'paper-entry'.[/yellow]")
        return

    table = Table(title="Options Paper Trade Journal", show_lines=True)
    table.add_column("Entry Date", style="cyan")
    table.add_column("Expiry",     style="cyan")
    table.add_column("ATM",        style="white",  justify="right")
    table.add_column("Entry Pts",  style="white",  justify="right")
    table.add_column("Exit Pts",   style="white",  justify="right")
    table.add_column("PnL Pts",    style="white",  justify="right")
    table.add_column("Net Rs",     style="white",  justify="right")
    table.add_column("Outcome",    style="white")

    wins = losses = 0
    total_pnl = 0.0

    for rec in records:
        outcome  = rec.get("outcome") or "OPEN"
        net_pnl  = rec.get("net_pnl_rs")
        pnl_pts  = rec.get("gross_pnl_pts")
        pnl_str  = f"Rs {net_pnl:+.0f}" if net_pnl is not None else "-"
        pts_str  = f"{pnl_pts:+.2f}" if pnl_pts is not None else "-"
        exit_str = f"{rec['total_exit_premium']:.1f}" if rec.get("total_exit_premium") is not None else "-"

        if outcome == "WIN":
            wins     += 1
            total_pnl += net_pnl or 0
            row_style = "green"
        elif outcome == "LOSS":
            losses    += 1
            total_pnl += net_pnl or 0
            row_style = "red"
        else:
            row_style = "yellow"

        table.add_row(
            rec.get("entry_date", ""),
            rec.get("expiry_date", ""),
            str(rec.get("atm_strike", "")),
            f"{rec.get('total_entry_premium', 0):.1f}",
            exit_str,
            pts_str,
            pnl_str,
            outcome,
            style=row_style,
        )

    console.print(table)
    closed = wins + losses
    if closed > 0:
        win_rate = 100 * wins / closed
        console.print(
            f"[bold]Closed: {closed}  |  Wins: {wins}  |  Losses: {losses}  |  "
            f"Win Rate: {win_rate:.1f}%  |  Total Net P&L: Rs {total_pnl:+.0f}[/bold]"
        )
