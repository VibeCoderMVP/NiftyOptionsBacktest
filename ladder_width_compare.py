"""
Additive comparison: 3-strike SELL ladder half-width +/-50 (baseline, live) vs +/-100
(challenger). Isolates ladder width only -- same entry/exit schedule, same regimes,
same lot sizing, same SELL side as the primary backtest in src/backtest.py.

Reads the existing weekly parquets (data/options/weekly/*.parquet). These already carry
strikes out to ATM+/-100 (built for the 5L config), so no re-fetch/re-build is needed --
only the offset set used to pick legs changes between the two configs compared here.

Does NOT touch options_journal.jsonl, active_options_position.json, backtest_results.parquet,
or any existing backtest_summary_*.csv -- purely additive outputs:

  data/backtest_ladder_width_weekly.csv    -- per-week pnl, both configs, aligned keys
  data/backtest_ladder_width_compare.csv   -- summary rows (config x regime, + "all")
  data/LADDER_WIDTH_50_VS_100_REPORT.md    -- markdown report with tables + recommendation

Run: uv run python ladder_width_compare.py
"""
from __future__ import annotations

from datetime import date as _date
from pathlib import Path

import pandas as pd
from loguru import logger

from src.backtest import BROKERAGE_PER_LEG, REGIME_META, _last_bar
from src.config import lot_size_for_date, settings

OFFSET_SETS = {
    "3L_50":  [-50, 0, 50],
    "3L_100": [-100, 0, 100],
}
N_LEGS_EXPECTED = 6  # 3 strikes x CE+PE
REGIME_ORDER = ["thu_expiry", "thu_expiry_4day", "tue_expiry"]


# ── Per-cycle P&L for both ladder widths ─────────────────────────────────────

def compute_cycle(path: Path) -> dict | None:
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        logger.warning("Could not read {}: {}", path.name, exc)
        return None

    expiry = str(df["expiry_date"].iloc[0])
    entry  = str(df["entry_date"].iloc[0])
    spot   = float(df["entry_spot"].iloc[0])
    atm    = int(df["atm_strike"].iloc[0])
    regime = str(df["regime"].iloc[0]) if "regime" in df.columns else "thu_expiry"

    lot_size = lot_size_for_date(_date.fromisoformat(expiry))

    entry_bars = _last_bar(df, entry)
    exit_bars  = _last_bar(df, expiry)

    # Holiday fallback, mirrors compute_cycle_pnl in src/backtest.py
    if exit_bars.empty:
        wed = str(pd.Timestamp(entry).date() + pd.Timedelta(days=1))
        exit_bars = _last_bar(df, wed)

    if entry_bars.empty or exit_bars.empty:
        return None

    merged = entry_bars[["strike", "strike_offset", "option_type", "close", "spot"]].rename(
        columns={"close": "entry_price", "spot": "entry_spot_bar"}
    ).merge(
        exit_bars[["strike", "option_type", "close", "spot"]].rename(
            columns={"close": "exit_price", "spot": "exit_spot_bar"}
        ),
        on=["strike", "option_type"], how="inner",
    )
    if merged.empty:
        return None

    exit_spot = None
    if merged["exit_spot_bar"].notna().any():
        exit_spot = float(merged["exit_spot_bar"].dropna().iloc[-1])

    result: dict = {
        "expiry_date": expiry, "entry_date": entry, "regime": regime,
        "atm_strike": atm, "entry_spot": spot, "exit_spot": exit_spot,
        "lot_size": lot_size,
    }

    for cfg, offsets in OFFSET_SETS.items():
        legs = merged[merged["strike_offset"].isin(offsets)]
        n_legs = len(legs)
        result[f"{cfg}_n_legs"] = n_legs
        if n_legs != N_LEGS_EXPECTED:
            result[f"{cfg}_complete"] = False
            continue
        result[f"{cfg}_complete"] = True

        entry_premium = float(legs["entry_price"].sum())
        exit_premium  = float(legs["exit_price"].sum())
        pnl_pts = entry_premium - exit_premium   # SELL: collect entry, pay exit
        pnl_rs  = pnl_pts * lot_size
        brokerage = BROKERAGE_PER_LEG * n_legs
        net_rs = pnl_rs - brokerage

        result[f"{cfg}_entry_premium"] = round(entry_premium, 2)
        result[f"{cfg}_exit_premium"]  = round(exit_premium, 2)
        result[f"{cfg}_pnl_pts"]       = round(pnl_pts, 2)
        result[f"{cfg}_pnl_rs"]        = round(pnl_rs, 2)
        result[f"{cfg}_net_rs"]        = round(net_rs, 2)

    return result


def run_all() -> pd.DataFrame:
    files = sorted(settings.weekly_dir.glob("*.parquet"))
    if not files:
        logger.error("No weekly parquet files found. Run 'uv run python pipeline.py build' first.")
        return pd.DataFrame()

    skipped_missing_legs = {cfg: 0 for cfg in OFFSET_SETS}
    rows = []
    for f in files:
        r = compute_cycle(f)
        if not r:
            continue
        rows.append(r)
        for cfg in OFFSET_SETS:
            if not r.get(f"{cfg}_complete", False):
                skipped_missing_legs[cfg] += 1

    if not rows:
        logger.error("No cycles produced results.")
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values(["regime", "expiry_date"]).reset_index(drop=True)

    out = settings.data_dir / "backtest_ladder_width_weekly.csv"
    df.to_csv(out, index=False)
    logger.info("Per-week comparison ({} cycles) -> {}", len(df), out)
    for cfg, n in skipped_missing_legs.items():
        logger.info("{}: {} cycles skipped (fewer than {} legs priced)", cfg, n, N_LEGS_EXPECTED)

    return df


# ── Summary stats ─────────────────────────────────────────────────────────────

def _config_regime_stats(df: pd.DataFrame, cfg: str, regime_filter: str | None) -> dict | None:
    sub = df if regime_filter is None else df[df["regime"] == regime_filter]
    sub = sub[sub[f"{cfg}_complete"] == True]
    if sub.empty:
        return None

    net = sub[f"{cfg}_net_rs"]
    wins = net[net > 0]
    losses = net[net <= 0]

    return {
        "config":               cfg,
        "regime":               regime_filter or "all",
        "n_trades":             len(sub),
        "win_rate_pct":         round(100 * len(wins) / len(sub), 1),
        "avg_net_rs":           round(net.mean(), 0),
        "median_net_rs":        round(net.median(), 0),
        "total_net_rs":         round(net.sum(), 0),
        "max_win_rs":           round(net.max(), 0),
        "max_loss_rs":          round(net.min(), 0),
        "avg_win_rs":           round(wins.mean(), 0) if not wins.empty else None,
        "avg_loss_rs":          round(losses.mean(), 0) if not losses.empty else None,
        "profit_factor":        round(wins.sum() / abs(losses.sum()), 2) if losses.sum() != 0 else None,
        "std_net_rs":           round(net.std(), 0),
        "avg_entry_premium_pts": round(sub[f"{cfg}_entry_premium"].mean(), 1),
    }


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cfg in OFFSET_SETS:
        for regime in [None] + REGIME_ORDER:
            r = _config_regime_stats(df, cfg, regime)
            if r:
                rows.append(r)
    out_df = pd.DataFrame(rows)
    out_path = settings.data_dir / "backtest_ladder_width_compare.csv"
    out_df.to_csv(out_path, index=False)
    logger.info("Summary table -> {}", out_path)
    return out_df


# ── Head-to-head (same weeks only) ────────────────────────────────────────────

def head_to_head(df: pd.DataFrame, regime_filter: str | None = None) -> pd.DataFrame:
    sub = df if regime_filter is None else df[df["regime"] == regime_filter]
    both = sub[(sub["3L_50_complete"] == True) & (sub["3L_100_complete"] == True)].copy()
    if both.empty:
        return both
    both["delta_net_rs"] = both["3L_100_net_rs"] - both["3L_50_net_rs"]
    both["winner"] = both["delta_net_rs"].apply(lambda x: "100" if x > 0 else ("50" if x < 0 else "tie"))
    if "exit_spot" in both.columns and both["exit_spot"].notna().any():
        both["abs_spot_move"] = (both["exit_spot"] - both["entry_spot"]).abs()
    return both


def _h2h_summary(h2h: pd.DataFrame) -> dict:
    if h2h.empty:
        return {}
    n = len(h2h)
    wins_100 = (h2h["winner"] == "100").sum()
    wins_50  = (h2h["winner"] == "50").sum()
    ties     = (h2h["winner"] == "tie").sum()
    corr = None
    if n > 2:
        corr = round(h2h["3L_50_net_rs"].corr(h2h["3L_100_net_rs"]), 3)
    return {
        "n_weeks":            n,
        "avg_net_50":         round(h2h["3L_50_net_rs"].mean(), 0),
        "avg_net_100":        round(h2h["3L_100_net_rs"].mean(), 0),
        "total_net_50":       round(h2h["3L_50_net_rs"].sum(), 0),
        "total_net_100":      round(h2h["3L_100_net_rs"].sum(), 0),
        "win_rate_50":        round(100 * (h2h["3L_50_net_rs"] > 0).sum() / n, 1),
        "win_rate_100":       round(100 * (h2h["3L_100_net_rs"] > 0).sum() / n, 1),
        "pct_100_beats_50":   round(100 * wins_100 / n, 1),
        "pct_50_beats_100":   round(100 * wins_50 / n, 1),
        "pct_tie":            round(100 * ties / n, 1),
        "corr_weekly_pnl":    corr,
    }


def _tertile_breakdown(h2h: pd.DataFrame) -> pd.DataFrame | None:
    if h2h.empty or "abs_spot_move" not in h2h.columns or h2h["abs_spot_move"].isna().all():
        return None
    h = h2h.dropna(subset=["abs_spot_move"]).copy()
    if len(h) < 6:
        return None
    try:
        h["move_tertile"] = pd.qcut(h["abs_spot_move"], 3, labels=["small", "medium", "large"], duplicates="drop")
    except ValueError:
        return None
    rows = []
    for t in h["move_tertile"].cat.categories:
        sub = h[h["move_tertile"] == t]
        if sub.empty:
            continue
        rows.append({
            "move_tertile":    t,
            "n_weeks":         len(sub),
            "avg_abs_move":    round(sub["abs_spot_move"].mean(), 1),
            "avg_net_50":      round(sub["3L_50_net_rs"].mean(), 0),
            "avg_net_100":     round(sub["3L_100_net_rs"].mean(), 0),
            "avg_delta":       round(sub["delta_net_rs"].mean(), 0),
        })
    return pd.DataFrame(rows)


# ── Markdown report ────────────────────────────────────────────────────────────

def _md_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_no data_\n"
    cols = list(df.columns)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(v) for v in row) + " |"
        for row in df.itertuples(index=False)
    )
    return f"{header}\n{sep}\n{body}\n"


def write_report(df: pd.DataFrame, summary: pd.DataFrame) -> None:
    h2h_all = head_to_head(df)
    h2h_tue = head_to_head(df, "tue_expiry")

    stats_all = _h2h_summary(h2h_all)
    stats_tue = _h2h_summary(h2h_tue)
    tertiles_tue = _tertile_breakdown(h2h_tue)

    tue_50 = _config_regime_stats(df, "3L_50", "tue_expiry")
    tue_100 = _config_regime_stats(df, "3L_100", "tue_expiry")

    lines = []
    lines.append("# 3L Ladder Half-Width: +/-50 (baseline) vs +/-100 (challenger)\n")
    lines.append(
        "SELL-side 3-strike ladders (6 legs), same entry/exit schedule and regimes as the "
        "primary backtest -- only strike offsets differ: +/-50 vs +/-100 from ATM.\n"
    )

    lines.append("## Summary by config x regime\n")
    lines.append(_md_table(summary))

    lines.append("## Head-to-head, same weeks only (all regimes combined)\n")
    if stats_all:
        lines.append(
            f"- N weeks with both configs complete: **{stats_all['n_weeks']}**\n"
            f"- Avg net Rs: 50={stats_all['avg_net_50']}  100={stats_all['avg_net_100']}\n"
            f"- Total net Rs: 50={stats_all['total_net_50']}  100={stats_all['total_net_100']}\n"
            f"- Win rate: 50={stats_all['win_rate_50']}%  100={stats_all['win_rate_100']}%\n"
            f"- % weeks 100 beats 50: {stats_all['pct_100_beats_50']}%  "
            f"| 50 beats 100: {stats_all['pct_50_beats_100']}%  | tie: {stats_all['pct_tie']}%\n"
            f"- Correlation of weekly P&L (50 vs 100): {stats_all['corr_weekly_pnl']}\n"
        )
    else:
        lines.append("_no overlapping weeks with both configs complete_\n")

    lines.append("## Head-to-head, tue_expiry only (live regime, lot=75)\n")
    if stats_tue:
        lines.append(
            f"- N weeks: **{stats_tue['n_weeks']}**\n"
            f"- Avg net Rs: 50={stats_tue['avg_net_50']}  100={stats_tue['avg_net_100']}\n"
            f"- Total net Rs: 50={stats_tue['total_net_50']}  100={stats_tue['total_net_100']}\n"
            f"- Win rate: 50={stats_tue['win_rate_50']}%  100={stats_tue['win_rate_100']}%\n"
            f"- % weeks 100 beats 50: {stats_tue['pct_100_beats_50']}%  "
            f"| 50 beats 100: {stats_tue['pct_50_beats_100']}%  | tie: {stats_tue['pct_tie']}%\n"
            f"- Correlation of weekly P&L (50 vs 100): {stats_tue['corr_weekly_pnl']}\n"
        )
    else:
        lines.append("_no overlapping tue_expiry weeks with both configs complete -- likely too few cycles yet_\n")

    lines.append("## Conditional on move size (tue_expiry, tertiles of |entry->exit spot move|)\n")
    lines.append(_md_table(tertiles_tue) if tertiles_tue is not None else "_insufficient data for tertile split_\n")

    lines.append("## Interpretation\n")

    if tue_50 and tue_100:
        wr_delta = tue_100["win_rate_pct"] - tue_50["win_rate_pct"]
        avg_delta = tue_100["avg_net_rs"] - tue_50["avg_net_rs"]
        maxloss_delta = tue_100["max_loss_rs"] - tue_50["max_loss_rs"]  # less negative = smaller loss
        lines.append(
            f"1. **Win rate vs credit (tue_expiry):** +/-100 win rate {tue_100['win_rate_pct']}% "
            f"vs +/-50 {tue_50['win_rate_pct']}% (delta {wr_delta:+.1f}pp). "
            f"Avg net Rs {tue_100['avg_net_rs']} vs {tue_50['avg_net_rs']} (delta {avg_delta:+.0f}). "
            + ("Confirms the expected trade: higher WR, lower avg P&L.\n" if wr_delta > 0 and avg_delta < 0
               else "Does not show the expected higher-WR/lower-P&L tradeoff in this sample -- check N before trusting.\n")
        )
        lines.append(
            f"2. **Max loss:** +/-100 max single loss Rs {tue_100['max_loss_rs']} vs +/-50 Rs {tue_50['max_loss_rs']} "
            f"(delta {maxloss_delta:+.0f}, positive = smaller loss magnitude for 100). "
            "Std of weekly net: "
            f"{tue_100['std_net_rs']} (100) vs {tue_50['std_net_rs']} (50).\n"
        )
        lines.append(
            "3. **tue_expiry recommendation input:** "
            f"Profit factor 50={tue_50['profit_factor']} vs 100={tue_100['profit_factor']}; "
            f"total net 50=Rs{tue_50['total_net_rs']} vs 100=Rs{tue_100['total_net_rs']} "
            f"over {tue_50['n_trades']}/{tue_100['n_trades']} trades respectively.\n"
        )
        closer = "same strategy, calmer path" if abs(wr_delta) < 10 else "materially different product"
        lines.append(f"4. **Character of the change:** {closer} (win-rate shift {wr_delta:+.1f}pp).\n")

        # Recommendation rule: optimize total net + profit factor on tue_expiry, win rate secondary, max loss tertiary
        pf50 = tue_50["profit_factor"] or 0
        pf100 = tue_100["profit_factor"] or 0
        tn50 = tue_50["total_net_rs"]
        tn100 = tue_100["total_net_rs"]
        if tn100 > tn50 and pf100 >= pf50:
            rec = "**switch to +/-100**"
        elif tn50 >= tn100 and pf50 >= pf100:
            rec = "**stick with +/-50**"
        else:
            rec = "**mixed signal -- total net and profit factor disagree; keep +/-50 live, track +/-100 in parallel paper**"
        lines.append(
            f"5. **Recommendation** (optimizing total net + profit factor on tue_expiry first, "
            f"win rate secondary, max loss tertiary): {rec}\n"
        )
    else:
        lines.append(
            "_Not enough complete tue_expiry cycles with all 6 legs priced for both configs to "
            "draw a tue_expiry-specific conclusion yet -- see the all-regime head-to-head above "
            "for the larger-sample (pre-Sep-2025) read, and re-run this report as more tue_expiry "
            "weeks accumulate._\n"
        )

    lines.append(
        "\n---\n*Generated by ladder_width_compare.py -- additive to the primary NiftyOptionsBacktest "
        "pipeline; does not modify options_journal.jsonl, active_options_position.json, or "
        "backtest_results.parquet.*\n"
    )

    out_path = settings.data_dir / "LADDER_WIDTH_50_VS_100_REPORT.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.success("Report -> {}", out_path)


def main() -> None:
    df = run_all()
    if df.empty:
        return
    summary = summary_table(df)
    write_report(df, summary)


if __name__ == "__main__":
    main()
