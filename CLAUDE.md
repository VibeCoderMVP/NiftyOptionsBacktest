# NiftyOptionsBacktest — Claude Instructions

## What This Project Does

Two things in one repo:

1. **Historical backtest** — Nifty weekly options theta-decay harvest strategy using Dhan's paid `POST /charts/rollingoption` API. DuckDB + Parquet storage.
2. **Live forward testing** — entry signal at 15:20 Thursday, paper trade journal, live LTP tracking via tick_service ZMQ, EasyTerminal Options tab display.

**Read STRATEGY.md first** for strategy context. This file is project mechanics only.

---

## Running the Historical Pipeline

Always run from `D:\Trading\NiftyOptionsBacktest\` with `uv run`:

```
uv run python pipeline.py test-api          # verify Dhan credentials + API response
uv run python pipeline.py fetch             # download all raw data (cached, safe to re-run)
uv run python pipeline.py build             # parse raw JSON → weekly parquets
uv run python pipeline.py validate          # check coverage
uv run python pipeline.py backtest          # compute P&L for all regimes
uv run python pipeline.py all               # fetch + build + validate + backtest
uv run python pipeline.py query "<SQL>"     # ad-hoc DuckDB queries
```

---

## Live Forward Testing — Weekly Workflow

### Thursday 15:10-15:20 IST

```
SIGNAL.bat          — waits until 15:10, fetches Nifty spot, computes ATM,
                      prints 6-leg order slip, sends Telegram alert,
                      resolves option security IDs from Dhan instruments CSV,
                      writes D:\Trading\active_options_position.json
```

Or manually: `uv run python pipeline.py signal [--spot 24178] [--force]`

**Timing guard:** If run before 15:10 IST on Thursday, blocks in a countdown loop and fires automatically at 15:10. After 15:35 warns about stale spot but proceeds.

### Thursday 15:25-15:28 IST (after placing SELL orders)

```
ENTRY.bat           — prompts for 6 fill LTPs, logs to options_journal.jsonl,
                      backfills entry_ltp into active_options_position.json
```

Or manually: `uv run python pipeline.py paper-entry <spot> <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>`

LTP order: ATM-50 CE, ATM-50 PE, ATM CE, ATM PE, ATM+50 CE, ATM+50 PE

### Fri/Mon/Tue (while position is open)

```
START_OPTIONS_LTP.bat    — REST polling fallback (15s cadence, ZMQ port 5557)
                           Usually not needed — tick_service (port 5555) handles this.
```

EasyTerminal Options tab (F4) shows live LTPs + unrealized P&L automatically.

### Tuesday 15:20-15:28 IST (buying back all 6 legs)

```
EXIT.bat            — prompts for 6 buyback prices, logs exit, computes P&L,
                      marks active_options_position.json as closed
```

Or manually: `uv run python pipeline.py paper-exit <ltp1> <ltp2> <ltp3> <ltp4> <ltp5> <ltp6>`

**Timing guard:**
- Before expiry day → asks `Exit early? (Y/N)`. N = aborts. Y = exits now.
- Expiry day before 15:20 → asks `Exit early? (Y/N)`. N = blocks in wait loop until 15:20.
- Expiry day at/after 15:20 → proceeds immediately.

### Tuesday after 15:30 (update historical DB)

```
WEEKLY_BACKFILL.bat — fetch + build + backtest; cross-checks paper trade P&L vs historical
```

---

## Auto-Pilot: scheduler.py (standalone, fixed 2026-07-05)

`scheduler.py` runs the weekly cycle automatically, **independent of EasyTerminal being
open**. Run it continuously (`START_SCHEDULER.bat`), same as any other always-on service
in this codebase (TW's P1/P2/P3, each strategy's monitor.py):

- **Auto-entry:** configured weekday (default Thursday) at/after `entry_time_ist` (default
  15:20), no open position → calls `run_signal(force=True)` in-process (no subprocess —
  same venv) → subscribes to ZMQ port 5555/5557 itself and waits (up to 6 min) for all 6
  leg LTPs → `paper_trade.log_entry()` → sends a **new** Telegram message with the confirmed
  total premium collected (previously missing — the order-slip alert from `run_signal()`
  only has the *proposed* legs, not the actual fill premiums).
- **Auto-exit:** position open, `expiry_date == today`, at/after `exit_time_ist` (default
  15:25) → same LTP wait (shorter timeout — legs have been ticking all week) →
  `paper_trade.log_exit()` → Telegram exit summary (entry/exit premium, net P&L). A leg with
  no LTP by the deadline falls back to Rs 0.05 (matches ET's own force-close convention).
- **Force-close:** still available via ET's Options tab (`C` key) for a manual early exit —
  unaffected by this change, still lives in `options_panel.py`/`options_journal_writer.py`.
- **Terminal-only premium heartbeat (added 2026-07-05):** every 5 min
  (`_PREVIEW_HEARTBEAT_INTERVAL_S`), prints a "still alive and can actually price this" signal
  — distinct from `scheduler_heartbeat.json`, which only proves the process is up, not that
  spot-fetch/ATM/security-ID-resolution/premium-fetch are still working end-to-end:
  - **While flat** (no open position — gated on position state, not a hardcoded weekday
    window, so it self-corrects for any flat stretch: normally Wed through Thu pre-15:20
    since exit is Tuesday, but equally covers Mon/Tue too if an entry was ever missed, e.g.
    the 2026-07-02 incident this whole file exists because of): once/day
    (`_refresh_daily_preview`), fetches that day's Nifty **session open** via the new
    `get_nifty_open()` (Dhan `/marketfeed/ohlc`, not `/marketfeed/ltp` — see its docstring,
    the exact response field is unverified against a live call yet), computes the would-be
    ATM-50/ATM/ATM+50 3L straddle, resolves its 6 legs, then re-prices and reprints the
    cumulative premium every cycle. Purely a dry run — **never** writes
    active_options_position.json/options_journal.jsonl, never sends Telegram.
    Skips Sat/Sun (`today.weekday() >= 5`) so a market-closed fetch failure doesn't
    retry-storm every 30s all weekend — added 2026-07-05 after noticing the gap.
  - **While a position is open** (Thu 15:25 through Tue 15:25): same cadence, but prints the
    real position's current cumulative premium instead (one-shot REST fetch via
    `_fetch_option_ltps_once`, not the ZMQ collector — simpler and independent of whether
    ticks are flowing at that exact moment). Still terminal-only; the actual exit still goes
    through `_try_auto_exit` above.

Config: `D:\Trading\options_config.json`. Writes `data/scheduler_heartbeat.json` (ET Services
tab row "Options Scheduler") and `data/scheduler.log`.

### Why this moved out of EasyTerminal (2026-07-05 incident)

The trigger used to be a 30-second Textual timer inside ET's `app.py`
(`_check_options_schedule`) — which only ever runs while ET's TUI is actually open. On
Thursday 2026-07-02, ET wasn't open at 15:20, so nothing fired: `active_options_position.json`
and `options_ltp_cache.json` both sat frozen at the prior cycle's 2026-06-30 exit with zero
activity for the rest of that week, and the cycle was silently skipped (no Telegram warning,
no log — just nothing happened). Moving the trigger to its own always-on process closes that
gap the same way every other strategy in this codebase already avoids it.

---

## Project Layout

```
NiftyOptionsBacktest/
├── pipeline.py               # CLI entry point — all commands here
├── STRATEGY.md               # Full strategy document (read this first)
├── CLAUDE.md                 # This file
├── pyproject.toml            # uv-managed dependencies
├── .env                      # DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (never commit)
├── analyse_trades.py         # Per-leg analysis of historical trades (exit data inference)
├── options_ltp_service.py    # Standalone REST polling LTP service (ZMQ port 5557 fallback)
├── scheduler.py              # Standalone auto-pilot (added 2026-07-05) — see "Auto-Pilot" above
├── SIGNAL.bat                # Thursday: compute ATM + order slip (manual/backup path)
├── ENTRY.bat                 # Thursday: log entry LTPs after fills (manual/backup path)
├── EXIT.bat                  # Tuesday: log exit LTPs after buyback (manual/backup path)
├── WEEKLY_BACKFILL.bat       # Tuesday: update historical DB
├── START_OPTIONS_LTP.bat     # Start REST fallback LTP polling service
├── START_SCHEDULER.bat       # Start the standalone auto-pilot scheduler — keep running always
├── src/
│   ├── config.py             # All constants + Settings (pydantic-settings from .env)
│   ├── fetcher.py            # Dhan API calls + disk caching
│   ├── builder.py            # Raw JSON → weekly parquet + cycle identification
│   ├── backtest.py           # P&L engine + regime-split summaries
│   ├── validator.py          # Coverage checks
│   ├── signal.py             # Entry signal engine (spot fetch, ATM, order slip, Telegram)
│   ├── paper_trade.py        # Journal log_entry / log_exit functions
│   └── dhan_instruments.py   # Downloads Dhan instruments CSV; resolves security IDs
└── data/
    ├── options/raw/           # Cached monthly JSON files (gitignored — regenerate with fetch)
    ├── options/weekly/        # Per-cycle parquet files (gitignored — regenerate with build)
    ├── options_journal.jsonl  # Paper trade log (entry/exit records, OPEN = outcome:null)
    ├── .last_signal.json      # Written by signal; read by paper-entry for ATM
    ├── scheduler_heartbeat.json  # scheduler.py health beacon — ET Services tab row
    ├── scheduler.log          # scheduler.py rotating log (5MB/14 days)
    ├── dhan_instruments.csv   # Instruments master (refreshed every 20h)
    ├── backtest_results.parquet
    ├── backtest_summary_pre_sep2025_2day.csv
    ├── backtest_summary_pre_sep2025_4day.csv
    └── backtest_summary_sep2025_onwards_4day.csv
```

---

## Coordination File: active_options_position.json

Written at `D:\Trading\active_options_position.json`. Shared between this repo, tick_service, and EasyTerminal.

```json
{
  "status": "open",
  "updated_at": "2026-06-26T15:21:00",
  "entry_date": "2026-06-26",
  "expiry_date": "2026-07-01",
  "atm": 24050,
  "entry_spot": 24046.25,
  "contracts": [
    {"strike": 24000, "option_type": "CE", "security_id": "49081", "exchange_segment": "NSE_FNO", "entry_ltp": 158.35},
    ...6 total...
  ]
}
```

- Written by `signal.py` on Thursday (status=open, entry_ltp=null)
- Updated by `paper_trade.log_entry()` (fills in entry_ltp for each leg)
- Watched by `tick_service.py` (subscribes to NSE_FNO contracts, publishes `OPT_` ticks)
- Closed by `paper_trade.log_exit()` (status=closed)
- Read by EasyTerminal auto-entry to know which legs to wait for LTPs

---

## options_journal.jsonl Schema

One JSON object per line. `outcome: null` = still open.

```json
{
  "batch_id": "20260626-152100",
  "ladder_size": "3L",
  "regime": "tue_expiry",
  "entry_date": "2026-06-26",
  "expiry_date": "2026-07-01",
  "entry_time": "2026-06-26 15:21",
  "entry_spot": 24050.0,
  "atm_strike": 24050,
  "lot_size": 75,
  "lots": 1,
  "legs": [
    {"strike": 24000, "type": "CE", "entry_ltp": 158.35, "exit_ltp": null, "exit_time": null},
    ...6 total...
  ],
  "total_entry_premium": 639.0,
  "total_exit_premium": null,
  "gross_pnl_pts": null,
  "gross_pnl_rs": null,
  "net_pnl_rs": null,
  "outcome": null,
  "paper_trade": true
}
```

P&L formula: `gross_pnl_pts = total_entry_premium - total_exit_premium` (SELL strategy — profit when total premium falls).  
Brokerage: Rs 20/leg/side × 6 legs × 2 sides = Rs 240/trade.  
`net_pnl_rs = gross_pnl_pts × lot_size × lots - brokerage`

---

## Key Architecture Decisions

**Dhan `POST /charts/rollingoption`** returns rolling front-week contract data:
- `expiryCode=1` = nearest front-week (NOT 0 — API rejects 0 as falsy)
- `drvOptionType="CALL"` fills `data["ce"]`; `data["pe"]` is null. Must call separately for PUT.
- Both calls needed per relative strike → 18 API streams (9 strikes × 2 types)
- Timestamps are Unix seconds UTC → convert to IST in builder
- `strike` column = actual absolute strike (float, varies per bar as ATM rolls)

**Three regimes in the data:**
- `thu_expiry` — Jan 2023–Aug 2025, entry=Tuesday close, expiry=Thursday close (2-day)
- `thu_expiry_4day` — Jan 2023–Aug 2025, entry=Monday close, expiry=Thursday close (4-day)
- `tue_expiry` — Sep 2025 onwards, entry=Thursday close, expiry=Tuesday close (4-day)

`REGIME_CHANGE_DATE = date(2025, 9, 1)` in config.py controls the split.

**Lot size history:**
- Pre Nov 20, 2024: 25 shares/lot
- Nov 20, 2024+: 75 shares/lot (SEBI F&O reform)

`NIFTY_LOT_SIZE = 75` in config.py. Historical backtest adjusts for pre-reform lots.

---

## Known Quirks

**Windows CP1252 terminal:** Never use non-ASCII characters (₹, →, etc.) in Rich output or loguru messages. Use `Rs` and `->` instead.

**Dhan API gotchas (all learned the hard way):**
- Field name is `requiredData` (with 'd') — NOT `requireData`
- `interval` must be `int`, not `str`
- `expiryCode=0` is falsy — API rejects it with "expiryCode is required". Use `1`.
- No top-level `"status"` field in response — check `resp["data"]["ce"] is not None`
- `data["pe"]` is `None` (not dict) when `drvOptionType="CALL"` — both sides are NOT returned together

**Windows SSL:** `truststore.inject_into_ssl()` called at fetcher + signal module load. Required for Dhan's cert chain on Windows. Do not remove.

**Holiday handling:** When expiry day is a market holiday, NSE moves expiry to previous Wednesday. `compute_cycle_pnl()` in backtest.py falls back to Wednesday if Thursday/Tuesday data is absent.

**Rolling data coverage gap:** When Nifty moves >200 pts during the week, entry strikes fall outside the ATM±4 relative range on exit day → exit LTP is absent. `analyse_trades.py` infers intrinsic value (`max(exit_spot - strike, 0.05)` for CE) from the exit-day spot price. These are marked with `*`.

**After expiry Tuesday:** Run WEEKLY_BACKFILL.bat after Tuesday (expiry day) 15:30 close to fetch + build + backtest the just-completed cycle. The parquet for the current expiry week does not exist until that run completes.

---

## Adding a New Regime or Strategy Variant

1. Add a constant to `config.py` if needed
2. Add a new branch in `identify_weekly_cycles()` in `builder.py`
3. Add the new `regime` string to `REGIME_META` dict in `backtest.py`
4. Delete affected weekly parquets and re-run `build` + `backtest`

---

## Dependencies

```toml
httpx, pandas, pyarrow, duckdb, pydantic-settings, loguru, rich, truststore,
python-dotenv, pyzmq>=25.0
```

Install: `uv sync --system-certs` (--system-certs required on Windows for PyPI SSL)

## .env Format

```
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_token
TELEGRAM_BOT_TOKEN=your_bot_token   # optional — for signal alerts
TELEGRAM_CHAT_ID=your_chat_id       # optional
```

## GitHub

Repo: https://github.com/VibeCoderMVP/NiftyOptionsBacktest
Branch: main
Never push: .env, data/, .venv/
