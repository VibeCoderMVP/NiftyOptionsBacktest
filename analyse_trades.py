"""
Per-leg premium analysis for 8 selected 3L_SELL trades (tue_expiry regime).
Entry bar : 15:20 candle close on Thursday (2nd-last candle of entry day)
Exit bar  : 15:25 candle close on Tuesday  (last candle of expiry day)
Drawdown  : max(sum of 3L leg HIGHs during hold) - entry_total_premium, x lot_size
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

WEEKLY_DIR  = Path("data/options/weekly")
LOT_SIZE    = 75
BROKERAGE   = 20.0 * 6   # Rs per lot for 3L (6 legs)

# Target cycles: (entry_date, label, note)
TARGETS = [
    ("2026-01-08", "Jan Wk2",  ""),
    ("2026-02-05", "Feb Wk1",  ""),
    ("2026-03-12", "Mar Wk2",  ""),
    ("2026-04-16", "Apr Wk3",  ""),
    ("2026-05-07", "May Wk1",  ""),
    ("2026-05-21", "May Wk3*", "* May 28 (Wk4) data absent — Jun 2 likely holiday; using May 21"),
    ("2026-06-04", "Jun Wk1",  ""),
    ("2026-06-18", "Jun Wk3",  ""),
]

LEG_ORDER = [
    ("ATM-50", "CE"),
    ("ATM-50", "PE"),
    ("ATM",    "CE"),
    ("ATM",    "PE"),
    ("ATM+50", "CE"),
    ("ATM+50", "PE"),
]


def next_tuesday(d: date) -> date:
    days = (1 - d.weekday()) % 7 or 7
    return d + timedelta(days=days)


def get_bar(df: pd.DataFrame, target_date: date, hour: int, minute: int) -> pd.DataFrame:
    mask = (
        (df["timestamp"].dt.date == target_date) &
        (df["timestamp"].dt.hour == hour) &
        (df["timestamp"].dt.minute == minute)
    )
    s = df[mask].sort_values("timestamp")
    return s.groupby(["strike", "option_type"]).last().reset_index()


def last_bar(df: pd.DataFrame, target_date: date) -> pd.DataFrame:
    day = df[df["timestamp"].dt.date == target_date]
    if day.empty:
        return pd.DataFrame()
    return day.sort_values("timestamp").groupby(["strike", "option_type"]).last().reset_index()


def analyse_cycle(entry_date_str: str) -> dict | None:
    entry  = date.fromisoformat(entry_date_str)
    expiry = next_tuesday(entry)
    pfile  = WEEKLY_DIR / f"{expiry}_tue_expiry.parquet"

    if not pfile.exists():
        return None

    df = pd.read_parquet(pfile)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    atm  = int(df["atm_strike"].iloc[0])
    spot = float(df["entry_spot"].iloc[0])

    legs_df = df[df["in_3L"] == True].copy()

    # Entry: 15:20 bar close
    entry_bars = get_bar(legs_df, entry, 15, 20)
    if entry_bars.empty:
        entry_bars = last_bar(legs_df, entry)   # fallback to last bar

    # Exit: 15:25 bar close (last bar of expiry day)
    # Use ALL strikes on exit day to get the spot price, not just in_3L
    all_exit = last_bar(df, expiry)
    exit_bars = last_bar(legs_df, expiry)
    if exit_bars.empty:
        wed = expiry - timedelta(days=6)
        exit_bars = last_bar(legs_df, wed)
        all_exit  = last_bar(df, wed)

    if entry_bars.empty:
        return None

    # Spot at exit: use the spot column from the broadest available exit data
    exit_spot: float | None = None
    if not all_exit.empty and "spot" in all_exit.columns:
        exit_spot = float(all_exit["spot"].dropna().iloc[-1]) if not all_exit["spot"].dropna().empty else None

    # Build per-leg map
    def price_map(bars: pd.DataFrame) -> dict[tuple, float]:
        m = {}
        for _, row in bars.iterrows():
            m[(int(row["strike"]), row["option_type"])] = float(row["close"])
        return m

    entry_map = price_map(entry_bars)
    exit_map  = price_map(exit_bars) if not exit_bars.empty else {}

    legs = []
    for label, otype in LEG_ORDER:
        if label == "ATM-50":
            strike = atm - 50
        elif label == "ATM":
            strike = atm
        else:
            strike = atm + 50
        e_ltp = entry_map.get((strike, otype), None)
        x_ltp = exit_map.get((strike, otype), None)

        # If exit not in rolling data but we have exit spot, infer intrinsic value
        inferred = False
        if x_ltp is None and exit_spot is not None:
            if otype == "CE":
                x_ltp = round(max(exit_spot - strike, 0.05), 2)
            else:
                x_ltp = round(max(strike - exit_spot, 0.05), 2)
            inferred = True

        pnl_pts = (e_ltp - x_ltp) if (e_ltp is not None and x_ltp is not None) else None
        legs.append({
            "label":      f"{strike} {otype}",
            "entry":      e_ltp,
            "exit":       x_ltp,
            "inferred":   inferred,
            "pnl_pts":    pnl_pts,
            "pnl_rs":     round(pnl_pts * LOT_SIZE, 0) if pnl_pts is not None else None,
        })

    total_entry = sum(l["entry"] for l in legs if l["entry"] is not None)
    total_exit  = sum(l["exit"]  for l in legs if l["exit"]  is not None)
    gross_pts   = total_entry - total_exit
    gross_rs    = round(gross_pts * LOT_SIZE, 0)
    net_rs      = gross_rs - BROKERAGE

    # Max drawdown: worst-case position value during hold
    # For SELL: position loses money when option prices rise
    # Drawdown at any point = (sum of 3L HIGH at that timestamp) - total_entry_premium
    # We look at each 5-min bar's HIGH sum
    hold = legs_df[
        (legs_df["timestamp"].dt.date >= entry) &
        (legs_df["timestamp"].dt.date <= expiry)
    ].copy()

    # Only include bars from after entry point
    entry_ts_start = pd.Timestamp(entry) + pd.Timedelta(hours=15, minutes=20)
    hold = hold[hold["timestamp"] >= entry_ts_start]

    # Sum HIGH across all 6 3L legs per timestamp
    high_sum = (
        hold.groupby("timestamp")["high"]
        .sum()
        .reset_index()
    )
    # Only use timestamps where all 6 legs are present
    leg_count = hold.groupby("timestamp")["strike"].count()
    full_ts = leg_count[leg_count == 6].index
    high_sum = high_sum[high_sum["timestamp"].isin(full_ts)]

    max_high_sum = float(high_sum["high"].max()) if not high_sum.empty else total_entry
    drawdown_pts = max_high_sum - total_entry
    drawdown_rs  = round(drawdown_pts * LOT_SIZE, 0)

    return {
        "entry":        entry,
        "expiry":       expiry,
        "spot":         spot,
        "exit_spot":    exit_spot,
        "atm":          atm,
        "legs":         legs,
        "total_entry":  round(total_entry, 2),
        "total_exit":   round(total_exit, 2),
        "gross_pts":    round(gross_pts, 2),
        "gross_rs":     gross_rs,
        "net_rs":       net_rs,
        "drawdown_pts": round(drawdown_pts, 2),
        "drawdown_rs":  drawdown_rs,
        "outcome":      "WIN" if net_rs > 0 else "LOSS",
    }


def main() -> None:
    console.print()
    console.print("[bold]3L SELL — Per-Leg Premium Analysis (tue_expiry regime, 2026)[/bold]")
    console.print("[dim]Entry = 15:20 candle close on Thursday | Exit = last candle on Tuesday[/dim]")
    console.print("[dim]Lot size: 75 | Brokerage est: Rs 120 (6 legs x Rs 20)[/dim]")
    console.print("[dim]Exit LTP marked * = inferred from exit-day Nifty spot (rolling data absent: strike moved outside ATM+/-4 range)[/dim]")
    console.print()

    all_results = []

    for entry_date_str, label, note in TARGETS:
        result = analyse_cycle(entry_date_str)
        if result is None:
            console.print(f"[red]{label}: data not available ({entry_date_str})[/red]")
            continue
        result["label"] = label
        result["note"]  = note
        all_results.append(result)

    for r in all_results:
        outcome_color = "green" if r["outcome"] == "WIN" else "red"
        exit_spot_str = f"  Exit spot: {r['exit_spot']:.0f}" if r.get("exit_spot") else ""
        console.rule(
            f"[bold]{r['label']}[/bold]  Entry: {r['entry']} (Thu)  ->  Expiry: {r['expiry']} (Tue)  |  "
            f"Entry spot: {r['spot']:.0f}  ATM: {r['atm']}{exit_spot_str}"
        )
        if r["note"]:
            console.print(f"[yellow]{r['note']}[/yellow]")

        # Per-leg table
        leg_tbl = Table(box=box.SIMPLE, show_header=True)
        leg_tbl.add_column("Leg",          style="cyan",  width=12)
        leg_tbl.add_column("Entry LTP",    style="white", justify="right", width=12)
        leg_tbl.add_column("Exit LTP",     style="white", justify="right", width=12)
        leg_tbl.add_column("P&L (pts)",    justify="right", width=12)
        leg_tbl.add_column("P&L (Rs)",     justify="right", width=13)

        for leg in r["legs"]:
            pnl_pts = leg["pnl_pts"]
            pnl_rs  = leg["pnl_rs"]
            pts_str = f"{pnl_pts:+.2f}" if pnl_pts is not None else "-"
            rs_str  = f"Rs {pnl_rs:+,.0f}" if pnl_rs is not None else "-"
            pts_color = "green" if (pnl_pts or 0) >= 0 else "red"
            rs_color  = pts_color
            exit_str = "-"
            if leg["exit"] is not None:
                exit_str = f"{leg['exit']:.2f}"
                if leg.get("inferred"):
                    exit_str += "*"   # * = inferred from spot (not from rolling data)
            leg_tbl.add_row(
                leg["label"],
                f"{leg['entry']:.2f}" if leg["entry"] is not None else "-",
                exit_str,
                f"[{pts_color}]{pts_str}[/{pts_color}]",
                f"[{rs_color}]{rs_str}[/{rs_color}]",
            )

        console.print(leg_tbl)

        # Summary row
        net_color = "green" if r["net_rs"] > 0 else "red"
        dd_color  = "red"   if r["drawdown_rs"] > 0 else "green"
        console.print(
            f"  Total entry premium : [bold]{r['total_entry']:.2f} pts[/bold]  "
            f"(Rs {r['total_entry'] * 75:,.0f})"
        )
        console.print(
            f"  Total exit premium  : [bold]{r['total_exit']:.2f} pts[/bold]  "
            f"(Rs {r['total_exit'] * 75:,.0f})"
        )
        console.print(
            f"  Gross P&L           : [bold]{r['gross_pts']:+.2f} pts = Rs {r['gross_rs']:+,.0f}[/bold]"
        )
        console.print(
            f"  Net P&L (after brok): [{net_color}][bold]Rs {r['net_rs']:+,.0f}[/bold][/{net_color}]  "
            f"| Outcome: [{net_color}]{r['outcome']}[/{net_color}]"
        )
        console.print(
            f"  Max drawdown (mark) : [{dd_color}]{r['drawdown_pts']:+.2f} pts = Rs {r['drawdown_rs']:+,.0f}[/{dd_color}]"
            f"  [dim](worst intrabar high-sum vs entry premium)[/dim]"
        )
        console.print()

    # Summary table across all trades
    console.rule("[bold]SUMMARY[/bold]")
    summary = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    summary.add_column("Week",          style="cyan",  width=9)
    summary.add_column("Entry",         width=11)
    summary.add_column("ATM",           justify="right", width=7)
    summary.add_column("Entry Prem",    justify="right", width=12)
    summary.add_column("Exit Prem",     justify="right", width=11)
    summary.add_column("Net P&L Rs",    justify="right", width=13)
    summary.add_column("Max DD Rs",     justify="right", width=13)
    summary.add_column("Result",        width=7)

    wins = losses = 0
    total_net = 0.0

    for r in all_results:
        net_color = "green" if r["net_rs"] > 0 else "red"
        dd_color  = "red"   if r["drawdown_rs"] > 0 else "green"
        summary.add_row(
            r["label"],
            str(r["entry"]),
            str(r["atm"]),
            f"{r['total_entry']:.1f} pts",
            f"{r['total_exit']:.1f} pts",
            f"[{net_color}]Rs {r['net_rs']:+,.0f}[/{net_color}]",
            f"[{dd_color}]Rs {r['drawdown_rs']:+,.0f}[/{dd_color}]",
            f"[{net_color}]{r['outcome']}[/{net_color}]",
        )
        if r["outcome"] == "WIN":
            wins += 1
        else:
            losses += 1
        total_net += r["net_rs"]

    console.print(summary)
    closed = wins + losses
    win_rate = 100 * wins / closed if closed else 0
    console.print(
        f"[bold]Trades: {closed}  |  Wins: {wins}  |  Losses: {losses}  |  "
        f"Win Rate: {win_rate:.0f}%  |  Total Net P&L: Rs {total_net:+,.0f}[/bold]"
    )
    console.print()


if __name__ == "__main__":
    main()
