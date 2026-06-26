"""Central config — all paths and strategy constants live here."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Dhan constants ────────────────────────────────────────────────────────────
NIFTY_SECURITY_ID = "13"         # Nifty 50 underlying in NSE_FNO
DHAN_API_BASE     = "https://api.dhan.co/v2"

# Relative strikes to fetch: ATM-4 … ATM+4 (covers ±200pt Nifty moves)
# This ensures our fixed Tuesday-close strikes always appear somewhere in the data
RELATIVE_STRIKES = [
    "ATM-4", "ATM-3", "ATM-2", "ATM-1", "ATM",
    "ATM+1", "ATM+2", "ATM+3", "ATM+4",
]
OPTION_TYPES = ["CALL", "PUT"]

# Nifty strike interval
STRIKE_STEP = 50   # points per strike

# How many steps on each side form the ladder
# 0 = ATM only (1-ladder), 1 = ATM±50 (3-ladder), 2 = ATM±100 (5-ladder)
LADDER_OFFSETS = [0, STRIKE_STEP, 2 * STRIKE_STEP]   # [0, 50, 100]

# Current Nifty lot size (post SEBI F&O reform, effective Nov 20, 2024)
NIFTY_LOT_SIZE = 75

# Lot size history — SEBI revised Nifty lot sizes as index levels changed.
#   Jan 2023 – Nov 19, 2024 : 25 shares/lot
#   Nov 20, 2024 onwards    : 75 shares/lot (SEBI F&O reform, minimum contract value increase)
# Source: NSE contract specification archives + SEBI circular Oct 1, 2024.
_LOT_SIZE_HISTORY = [
    (date(2023, 1, 1),  25),
    (date(2024, 11, 20), 75),
]


def lot_size_for_date(d: date) -> int:
    """Return the Nifty lot size in effect on a given expiry date."""
    size = 25
    for from_date, ls in _LOT_SIZE_HISTORY:
        if d >= from_date:
            size = ls
    return size


# NSE changed Nifty weekly expiry from Thursday to Tuesday effective Sep 1, 2025.
# Data before this date: entry=Tuesday close, exit=Thursday close (2-day hold).
# Data from this date:   entry=Thursday close, exit=Tuesday close  (5-day hold).
REGIME_CHANGE_DATE = date(2025, 9, 1)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    dhan_client_id:    str
    dhan_access_token: SecretStr

    telegram_bot_token: str = ""   # optional: for entry signal alerts
    telegram_chat_id:   str = ""   # optional: your Telegram chat/channel ID

    backtest_start: str = "2023-01-01"
    backtest_end:   str = ""           # defaults to today in pipeline.py

    data_dir:     Path = Path("data")
    api_delay_s:  float = 0.5          # seconds between Dhan API calls

    @property
    def raw_dir(self) -> Path:
        d = self.data_dir / "options" / "raw"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def weekly_dir(self) -> Path:
        d = self.data_dir / "options" / "weekly"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def effective_end(self) -> str:
        return self.backtest_end or date.today().strftime("%Y-%m-%d")


settings = Settings()
