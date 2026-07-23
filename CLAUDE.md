# NiftyOptionsBacktest — Claude Instructions

## What This Project Does

Two things in one repo:

1. **Historical backtest** — Nifty weekly options theta-decay harvest strategy using Dhan's paid `POST /charts/rollingoption` API. DuckDB + Parquet storage.
2. **Live forward testing** — entry signal at 15:20 Thursday, paper trade journal, live LTP tracking via tick_service ZMQ, EasyTerminal Options tab display.

**Read STRATEGY.md first** for strategy context. This file is project mechanics only.

---

## Two parallel ladder variants (added 2026-07-21)

`src/config.py::LADDER_VARIANTS` drives the whole forward-test loop as a list of variant
dicts, not a single hardcoded config, since 2026-07-21:

| Variant | id | offset | paper_only | active file | journal file |
|---|---|---|---|---|---|
| Live | `3L-50` | 50 | `False` | `active_options_position.json` | `options_journal.jsonl` |
| Paper (calm-tail track) | `3L-100` | 100 | `True` | `active_options_position_3l100.json` | `options_journal_3l100.jsonl` |

Both are always exactly 3 strikes x CE+PE = 6 legs — only the strike offset differs, so the
`len(legs) != 6` guards throughout the codebase stay correct for either variant unmodified.
3L-100 was added because the offline ladder-width backtest
(`data/LADDER_WIDTH_50_VS_100_REPORT.md`) showed near-identical total P&L (ρ≈0.99 weekly
correlation) but a meaningfully calmer left tail (max loss Rs -4,346 vs -7,792 in tue_expiry) —
too promising to ignore, too thin a sample (26 weeks) to switch the live ladder on. 3L-100
therefore runs as a second always-on **paper-only** track inside the same `scheduler.py`
process, same 15:20/15:25 clock, writing to its own separate active-position/journal files so
neither variant's state machine can collide with the other's.

`src/signal.py::build_order_slip`/`write_active_position`/`run_signal` and
`src/paper_trade.py::log_entry`/`log_exit` all take an `offset`/`active_path`/`journal_path`
(and `ladder_id`/`variant_label`) parameter now, defaulting to the exact pre-2026-07-21 3L-50
behavior — a call site that doesn't pass these keeps working unchanged (verified byte-identical
regression on the active-position payload and journal record for `offset=50` defaults).

**3L-100's own live LTP subscription, not just the strike-band monitor.** TW's `tick_service.py`
only auto-subscribes contracts by watching `active_options_position.json`'s mtime — a single
hardcoded path. 3L-100 writes to a *different* file, so that auto-subscribe mechanism never
sees it. `scheduler.py::_subscribe_variant_legs()` fixes this by calling
`request_subscription()` directly for each variant's own 6 legs right after signal (same
mechanism `nifty_strike_band_monitor` already uses) — so 3L-100's feed doesn't silently depend
on the daily strike-band job's health.

**Config gating is independent per variant.** `options_config.json`'s top-level `auto_entry`/
`auto_exit` gate 3L-50 only (unchanged). A new `"ladder_100": {"enabled": true, "paper_mode":
true, "lots": 1}` block gates 3L-100 independently — so pausing live 3L-50 doesn't stop 3L-100
paper-testing, and vice versa.

**Telegram messages for 3L-100 are always tagged `[3L-100 PAPER ONLY -- DO NOT PLACE REAL
ORDERS]`** — the existing 3L-50 message wording (which reads as "go place 6 SELL orders") would
otherwise be dangerously ambiguous for a paper-only track.

**EasyTerminal's Options tab (F4) shows both, stacked** — see `EasyTerminal/CLAUDE.md`'s Options
Tab section for the display side.

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
- **Terminal-only premium heartbeat:** every 5 min (`_PREVIEW_HEARTBEAT_INTERVAL_S`), prints
  a "still alive and can actually price this" signal — distinct from `scheduler_heartbeat.json`,
  which only proves the process is up, not that spot-fetch/ATM/security-ID-resolution/
  premium-fetch are still working end-to-end:
  - **While flat** (no open position — gated on position state, not a hardcoded weekday
    window, so it self-corrects for any flat stretch: normally Wed through Thu pre-15:20
    since exit is Tuesday, but equally covers Mon/Tue too if an entry was ever missed, e.g.
    the 2026-07-02 incident this whole file exists because of): once/day
    (`_refresh_daily_preview`), gets that day's Nifty **session open**, computes the would-be
    ATM-50/ATM/ATM+50 3L straddle, resolves its 6 legs, then re-prices and reprints the
    cumulative premium every cycle. Purely a dry run — **never** writes
    active_options_position.json/options_journal.jsonl, never sends Telegram.
    Skips Sat/Sun (`today.weekday() >= 5`).
  - **While a position is open** (Thu 15:25 through Tue 15:25): same cadence, prints the
    real position's current cumulative premium. Still terminal-only; the actual exit still
    goes through `_try_auto_exit` above.

  **Rewritten 2026-07-07 — both the Nifty spot/open fetch AND all premium reads are now
  ZMQ-first, REST is gone from the normal path entirely:**
  - **Nifty spot/open**: `NiftySpotCollector` subscribes to TW's P2 on ZMQ topic `b"NIFTY"`
    (added to P2's feed the same day — see `TradingWebSockets/CLAUDE.md`'s "Dynamic
    Subscriptions" section). Tracks the first tick of each IST day as a session-open proxy.
    REST (`get_nifty_open`/`get_nifty_spot` in `src/signal.py`) is kept only as a fallback for
    when ZMQ has no data yet (P2 not up, or no tick received this session yet) — under normal
    operation this REST path is never exercised. A capped 15s wait-for-first-tick runs at
    scheduler startup before the main loop begins, so the very first cycle doesn't hit REST
    just because its own ZMQ subscriber hadn't warmed up yet (NIFTY ticks arrive several
    times/second once subscribed, so 15s is a generous margin).
  - **Retry cooldown**: `_refresh_daily_preview()` previously retried a failed fetch every 30s
    forever (no backoff on the failure path, only a success gate) — this hammered Dhan's REST
    endpoints for 297 logged attempts over two days and plausibly contributed to a 429
    rate-limit block. Fixed with `_PREVIEW_RETRY_COOLDOWN_S = 300` (5 min).
  - **Both premium branches** (open-position AND dry-run-preview) now read from `LtpCollector`
    (ZMQ, `OPT_`-prefix topics), not REST. `_fetch_option_ltps_once` (the old REST helper) has
    been deleted — it had become genuinely dead code once both call sites stopped using it.
    Preview legs get their live ticks via `trading_core.subscription_registry.
    request_subscription()`, requesting topics named `OPT_{strike}_{type}` with `strike`/
    `option_type` passed through as extra payload fields — this is exactly what lets the
    existing `LtpCollector` (already running for real positions) pick them up with zero new
    consumer code. New legs typically start ticking within ~5 seconds of being requested.
  - See `trading_core/CLAUDE.md`'s "Subscription Registry" section and the
    `bug_scheduler_preview_retry_storm`/`project_p2_dynamic_subscriptions` memory entries for
    the full incident/fix record.

- **Strike band monitor** (`_refresh_strike_band` / `StrikeBandState`, added 2026-07-08):
  runs every day, unconditionally — unlike the entry-decision preview above, this does NOT
  gate on being flat. Once/day, from Nifty's session open (same ZMQ-first/REST-fallback
  source as the preview), computes ATM and subscribes a **13-strike band (ATM ± 300, step
  50 = 26 legs, CE+PE)** via the same `request_subscription()` path, requester name
  `nifty_strike_band_monitor`, topics `OPT_{strike}_{type}` (real prefix, not a test one —
  safe because both `LtpCollector` here and ET's `zmq_options.py` already filter ticks
  client-side to only the strikes they actually care about, so extra band legs are silently
  ignored by anything not asking for them). Purpose: continuous premium visibility across a
  wide strike range regardless of position state, and specifically wide enough (±6 strikes)
  that a mid-day scheduler restart — which recomputes "session open" from whatever Nifty
  spot is at restart time, not the true 09:15 open, see `NiftySpotCollector`'s docstring —
  still lands well inside the covered band rather than going stale. Terminal-only, same as
  the entry-decision preview: no journal/Telegram/position side effects.

- **Stress test** (`stress_test_dynamic_subscriptions.py`, added 2026-07-07) — **run only for
  ad hoc validation of the dynamic-subscription mechanism itself, never leave it running.**
  Originally documented as a "permanent utility" to keep running always; downgraded 2026-07-08
  after it was found actively injecting 4 fake `STRESS_*`-prefixed strikes into the live P2
  feed during real market hours, which the user had not intended and does not want by default.
  Every N seconds (default 60), requests a random Nifty straddle strike (±500 pts from spot,
  step 50) via the same `request_subscription()` path, and prints the resulting premium once
  both legs tick — still useful for confirming the registry mechanism works end-to-end after a
  change to it, just not as a standing background process. Run manually with
  `START_STRESS_TEST.bat` or `uv run python stress_test_dynamic_subscriptions.py [--interval N]
  [--iterations N]` (0 = forever), then **stop it and delete
  `D:\Trading\dynamic_subscriptions\stress_test_dynamic_subscriptions.json` when done** — see
  `TradingWebSockets/CLAUDE.md`'s "Dynamic Subscriptions" gotcha: killing the process alone
  does not un-subscribe its legs or stop P2 from re-reading its stale request file on the next
  restart.

Config: `D:\Trading\options_config.json`. Writes `data/scheduler_heartbeat.json` (ET Services
tab row "Options Scheduler") and `data/scheduler.log`.

**`_ist_now()` must return a naive datetime — fixed 2026-07-06.** It computes the correct IST
wall-clock value via `datetime.now(timezone.utc) + _IST`, but that expression keeps
`tzinfo=utc` on a value that's actually ~5.5h ahead of true UTC. ET's `_age_seconds()` branches
on whether the parsed timestamp has `tzinfo`: if so, it computes `datetime.now(timezone.utc) -
dt`, which went **negative** here (a "future" timestamp) — and a negative age is trivially
"fresh," so the Services tab showed the Options Scheduler as `OK`/GREEN for hours after it had
actually crashed (the `LtpCollector.stop()` `AttributeError`, same day). Fixed by stripping
`tzinfo` before returning, matching every other service's naive-local-IST heartbeat convention
in this codebase (see `TradingWebSockets/CLAUDE.md`'s `_age_seconds()` gotcha for the general
rule). If a future service's heartbeat ever shows GREEN when it shouldn't, check whether its
timestamp is tz-aware and whether that tag is actually correct before trusting the display.

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
├── stress_test_dynamic_subscriptions.py  # Ad hoc validation utility, run manually only (added 2026-07-07) — see "Auto-Pilot" above
├── START_STRESS_TEST.bat     # Launcher for the stress test above
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
- **`POST /marketfeed/ltp` and `POST /marketfeed/ohlc` require security IDs as `int`, not `str`, in the request list** — found 2026-07-06. `{"IDX_I": ["13"]}` → `400 {"data":{"814":"Invalid Request"},"status":"failed"}`; `{"IDX_I": [13]}` → `200 success`. This is the opposite convention from `POST /charts/rollingoption` (used by `fetcher.py`), which wants `NIFTY_SECURITY_ID` as the string `"13"` — don't "fix" one to match the other, they're genuinely different endpoints with different expectations. `get_nifty_spot()`/`get_nifty_open()` (`src/signal.py`) cast to `int(...)` only in the request payload (dict keys/lookups elsewhere stay as strings, matching what the response actually returns) — these are now only exercised as a fallback, see "Auto-Pilot: scheduler.py" above. `_fetch_option_ltps_once()` (the REST helper this same fix originally applied to) was deleted 2026-07-07 once both its call sites moved to ZMQ.

**Windows SSL:** `truststore.inject_into_ssl()` called at fetcher + signal module load. Required for Dhan's cert chain on Windows. Do not remove.

**Holiday handling:** When expiry day is a market holiday, NSE moves expiry to previous Wednesday. `compute_cycle_pnl()` in backtest.py falls back to Wednesday if Thursday/Tuesday data is absent.

**Rolling data coverage gap:** When Nifty moves >200 pts during the week, entry strikes fall outside the ATM±4 relative range on exit day → exit LTP is absent. `analyse_trades.py` infers intrinsic value (`max(exit_spot - strike, 0.05)` for CE) from the exit-day spot price. These are marked with `*`.

**After expiry Tuesday:** Run WEEKLY_BACKFILL.bat after Tuesday (expiry day) 15:30 close to fetch + build + backtest the just-completed cycle. The parquet for the current expiry week does not exist until that run completes.

**A suspended Python function can resume mid-execution across a real hardware sleep/hibernate — found live 2026-07-23.** Both auto-entry ladders (3L-50, 3L-100) logged `"AUTO-ENTRY triggered"` at 15:45:43/16:12:58, but the machine had been asleep since 12:44 (unrelated incident — see `D:\Trading\LEARNING_WINDOWS_SLEEP_INCIDENT_2026-07-23.md`) and only truly woke at 16:12:53. `_try_auto_entry()`'s internal "wait up to 6 min for all 6 leg LTPs" loop was mid-flight when the deep hibernate cut in, and picked up exactly where it left off ~27 minutes later once the machine actually resumed — Windows hibernate freezes and restores full process state (stack, locals, open sockets), it doesn't kill the process. **This did not corrupt the trade**: both entries fired well after 15:20 (the configured `entry_time_ist`), but since NSE had already closed at 15:30 before either even triggered, the option chain fetch — whenever it actually executed — returned the same frozen end-of-day settlement snapshot either way. Verified directly, not assumed: re-fetching the live chain hours later returned `spot=23869.6`, `23850 CE ltp=147.5`, `23850 PE ltp=110.65` — an exact match to what both ladders recorded. If a similar late-trigger is ever observed again, check the Windows Event Log for a sleep/wake event before assuming a scheduler logic bug — and if the market was already closed before the trigger fired, the recorded prices are very likely still correct (Dhan's chain API returns the frozen last print after hours regardless of when you ask), just late.

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
DHAN_PIN=your_pin                   # required for TOTP auto-renewal (see below)
DHAN_TOTP_SECRET=your_totp_secret   # required for TOTP auto-renewal (see below)
TELEGRAM_BOT_TOKEN=your_bot_token   # optional — for signal alerts
TELEGRAM_CHAT_ID=your_chat_id       # optional
```

**Token auto-renewal (fixed 2026-07-06):** `D:\Trading\renew_token.py`'s daily 08:50 auto-renewal
only ever wrote `DHAN_ACCESS_TOKEN` to `TradingWebSockets\.env` and `TradingCommodities\.env` —
this project's `.env` was never in that list, so its token silently went stale (found live
2026-07-06: `scheduler.py` was failing every Dhan call with `401 Unauthorized` off a token that
had expired ~10 days earlier, while TW's own token was fine). Fixed by adding
`D:\Trading\NiftyOptionsBacktest\.env` to `renew_token.py`'s `ENV_FILES` list — this project's
token is now kept current by the same daily auto-renewal as TW's. If a 401 shows up again on
any Dhan call from this project, check `renew_token.py`'s `ENV_FILES` list hasn't regressed
before assuming it's a credentials problem.

## GitHub

Repo: https://github.com/VibeCoderMVP/NiftyOptionsBacktest
Branch: main
Never push: .env, data/, .venv/
