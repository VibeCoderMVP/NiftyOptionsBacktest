# NiftyOptionsBacktest — Claude Instructions

## What This Project Does

Backtests a Nifty weekly options theta-decay harvest strategy using Dhan's paid historical API.
Fetches rolling options data → builds weekly parquet files → runs 6 configurations → outputs regime-split P&L summaries.

**Read STRATEGY.md first** for strategy context. This file is project mechanics only.

## Running the Pipeline

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

## Project Layout

```
NiftyOptionsBacktest/
├── pipeline.py           # CLI entry point — all commands here
├── STRATEGY.md           # Full strategy document (read this first)
├── CLAUDE.md             # This file
├── pyproject.toml        # uv-managed dependencies
├── .env                  # DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (never commit)
├── src/
│   ├── config.py         # All constants + Settings (pydantic-settings from .env)
│   ├── fetcher.py        # Dhan API calls + disk caching
│   ├── builder.py        # Raw JSON → weekly parquet + cycle identification
│   ├── backtest.py       # P&L engine + regime-split summaries
│   └── validator.py      # Coverage checks
└── data/
    ├── options/raw/      # Cached monthly JSON files (gitignored — regenerate with fetch)
    ├── options/weekly/   # Per-cycle parquet files (gitignored — regenerate with build)
    ├── backtest_results.parquet
    ├── backtest_summary_pre_sep2025_2day.csv
    ├── backtest_summary_pre_sep2025_4day.csv
    ├── backtest_summary_sep2025_onwards_4day.csv
    └── pipeline.log
```

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

**Weekly parquet filenames:** `{expiry_date}_{regime}.parquet` (e.g. `2026-06-17_tue_expiry.parquet`)

## Known Quirks

**Windows CP1252 terminal:** Never use non-ASCII characters (₹, →, etc.) in Rich output or loguru messages. Use `Rs` and `->` instead. The `.encode('cp1252')` error will surface in loguru/Rich if you use them.

**Dhan API gotchas (all learned the hard way):**
- Field name is `requiredData` (with 'd') — NOT `requireData`
- `interval` must be `int`, not `str`
- `expiryCode=0` is falsy — API rejects it with "expiryCode is required". Use `1`.
- No top-level `"status"` field in response — check `resp["data"]["ce"] is not None`
- `data["pe"]` is `None` (not dict) when `drvOptionType="CALL"` — both sides are NOT returned together

**Windows SSL:** `truststore.inject_into_ssl()` called at fetcher module load. Required for Dhan's cert chain on Windows. Do not remove.

**Holiday handling:** When expiry day is a market holiday, NSE moves expiry to previous Wednesday. `compute_cycle_pnl()` in backtest.py falls back to Wednesday if Thursday/Tuesday data is absent.

## Adding a New Regime or Strategy Variant

1. Add a constant to `config.py` if needed
2. Add a new branch in `identify_weekly_cycles()` in `builder.py`
3. Add the new `regime` string to `REGIME_META` dict in `backtest.py`
4. Delete affected weekly parquets and re-run `build` + `backtest`

## Dependencies

```toml
httpx, pandas, pyarrow, duckdb, pydantic-settings, loguru, rich, truststore, python-dotenv
```

Install: `uv sync --system-certs` (--system-certs required on Windows for PyPI SSL)

## .env Format

```
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_token
```

## GitHub

Repo: https://github.com/VibeCoderMVP/NiftyOptionsBacktest  
Branch: main  
Never push: .env, data/, .venv/
