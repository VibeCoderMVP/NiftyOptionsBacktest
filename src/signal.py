"""
Entry signal engine for Nifty weekly options (tue_expiry regime).

Run at ~15:15 IST on Thursdays:
  uv run python pipeline.py signal [--force]

Fetches Nifty spot -> computes ATM -> prints order slip -> sends Telegram alert.
Saves last signal to data/.last_signal.json for use by paper-entry command.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import truststore
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import DHAN_API_BASE, NIFTY_LOT_SIZE, settings

truststore.inject_into_ssl()
console = Console()

_IST = timedelta(hours=5, minutes=30)


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + _IST


def is_entry_day(force: bool = False) -> bool:
    """True if today is Thursday (new regime entry day), or force=True."""
    if force:
        return True
    return _ist_now().weekday() == 3  # 3 = Thursday


def is_market_open() -> bool:
    now = _ist_now()
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def get_nifty_spot() -> float | None:
    """
    Fetch Nifty 50 LTP via Dhan POST /marketfeed/ltp.
    Returns None on any failure — caller prompts for manual input.
    """
    headers = {
        "access-token": settings.dhan_access_token.get_secret_value(),
        "client-id":    settings.dhan_client_id,
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(
            f"{DHAN_API_BASE}/marketfeed/ltp",
            json={"IDX_I": ["13"]},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape: {"data": {"IDX_I": {"13": {"last_price": 24178.5, ...}}}}
        ltp = (
            data.get("data", {})
                .get("IDX_I", {})
                .get("13", {})
                .get("last_price")
        )
        if ltp is not None:
            return float(ltp)
        logger.warning("LTP field not found in response: {}", str(data)[:300])
        return None
    except Exception as exc:
        logger.warning("get_nifty_spot failed: {}", exc)
        return None


def compute_atm(spot: float) -> int:
    return int(round(spot / 50) * 50)


def next_expiry_tuesday(from_date: date) -> date:
    """Given a Thursday entry date, return the following Tuesday (+5 days)."""
    days = (1 - from_date.weekday()) % 7 or 7
    return from_date + timedelta(days=days)


def build_order_slip(atm: int) -> list[dict]:
    return [
        {"strike": atm - 50, "type": "CE"},
        {"strike": atm - 50, "type": "PE"},
        {"strike": atm,      "type": "CE"},
        {"strike": atm,      "type": "PE"},
        {"strike": atm + 50, "type": "CE"},
        {"strike": atm + 50, "type": "PE"},
    ]


def format_signal_message(
    spot: float,
    atm: int,
    legs: list[dict],
    entry_date: date,
    expiry_date: date,
) -> str:
    lines = [
        "NIFTY WEEKLY OPTIONS - ENTRY SIGNAL",
        f"Date      : {entry_date} (Thursday)",
        f"Regime    : tue_expiry | 4-day hold",
        f"Nifty Spot: {spot:.0f}",
        f"ATM Strike: {atm}",
        f"Expiry    : {expiry_date} (Tuesday)",
        "",
        "ORDER SLIP (all SELL, 1 lot each):",
        f"  {'Strike':<8} {'Type':<5} Action",
        f"  {'-'*28}",
    ]
    for leg in legs:
        lines.append(f"  {leg['strike']:<8} {leg['type']:<5} SELL 1 lot")
    lines += [
        "",
        f"ENTER at 15:20-15:28 IST today.",
        f"CLOSE ALL by 15:25 on {expiry_date} (Tuesday).",
        f"Lot size: {NIFTY_LOT_SIZE} shares/lot",
    ]
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    token   = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env)")
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram alert sent to chat {}", chat_id)
    except Exception as exc:
        logger.warning("Telegram send failed: {}", exc)


def save_last_signal(entry_date: date, expiry_date: date, spot: float, atm: int) -> None:
    path = Path(settings.data_dir) / ".last_signal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "entry_date":  str(entry_date),
            "expiry_date": str(expiry_date),
            "spot":        spot,
            "atm":         atm,
        }),
        encoding="utf-8",
    )


def load_last_signal() -> dict | None:
    path = Path(settings.data_dir) / ".last_signal.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_signal(force: bool = False) -> None:
    today = _ist_now().date()
    now   = _ist_now()

    if not is_entry_day(force):
        day_name = now.strftime("%A")
        console.print(
            f"[yellow]Today is {day_name} — entry day is Thursday. "
            f"Use --force to run anyway.[/yellow]"
        )
        return

    if not is_market_open():
        console.print(
            f"[yellow]Market is closed (IST {now.strftime('%H:%M')}). "
            f"Spot price may be stale.[/yellow]"
        )

    console.print("[bold]Fetching Nifty 50 spot price...[/bold]")
    spot = get_nifty_spot()

    if spot is None:
        console.print("[red]Auto-fetch failed (market may be closed or API unavailable).[/red]")
        try:
            raw = input("Enter Nifty spot price manually (or press Enter to abort): ").strip()
            if not raw:
                console.print("[red]Aborted.[/red]")
                return
            spot = float(raw)
        except (ValueError, EOFError):
            console.print("[red]Invalid input. Aborted.[/red]")
            return

    atm         = compute_atm(spot)
    expiry_date = next_expiry_tuesday(today)
    legs        = build_order_slip(atm)

    table = Table(
        title=f"NIFTY ENTRY SIGNAL | Entry: {today} (Thu) | Expiry: {expiry_date} (Tue)",
        show_lines=True,
    )
    table.add_column("Strike", style="cyan",   justify="right")
    table.add_column("Type",   style="yellow")
    table.add_column("Action", style="green")
    table.add_column("Lots",   style="white")
    for leg in legs:
        table.add_row(str(leg["strike"]), leg["type"], "SELL", "1")

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]Nifty Spot:[/bold] {spot:.0f}  |  "
        f"[bold]ATM:[/bold] {atm}  |  "
        f"[bold]Expiry:[/bold] {expiry_date}"
    )
    console.print(
        f"[bold green]ENTER at 15:20-15:28 IST.  "
        f"CLOSE ALL by 15:25 on {expiry_date}.[/bold green]"
    )
    console.print(f"[dim]Lot size: {NIFTY_LOT_SIZE} shares | Brokerage est: Rs 120 for 6 legs[/dim]")
    console.print()

    msg = format_signal_message(spot, atm, legs, today, expiry_date)
    send_telegram(msg)
    save_last_signal(today, expiry_date, spot, atm)
    logger.info("Signal complete | ATM={} expiry={} spot={:.0f}", atm, expiry_date, spot)
    console.print(f"[dim]Signal saved -> data/.last_signal.json[/dim]")
    console.print(
        f"\n[dim]Next step: after placing orders, run:[/dim]\n"
        f"[bold]uv run python pipeline.py paper-entry "
        f"<ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>[/bold]\n"
        f"[dim]LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE[/dim]"
    )
