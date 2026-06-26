# Nifty Weekly Options Theta Harvest — Strategy Reference

> **Status:** Backtested Jan 2023 – Jun 2026 | 3-ladder (6 legs) SELL is the primary config  
> **Regime:** Two distinct regimes split at Sep 1, 2025 (NSE changed Nifty expiry from Thursday to Tuesday)  
> **Core idea:** Sell the structural overpricing of implied volatility in the final 2–4 days of a Nifty weekly contract

---

## 1. Philosophy

Nifty weekly options systematically overprice implied volatility relative to realized volatility over the 2–4 day window before expiry. This is not a prediction about direction — it is a bet that the market's *fear premium* (the gap between what the option prices in as a possible move vs what Nifty actually moves) is consistently too large.

Every week, option buyers pay more for protection than the actual move warrants, on average. Sellers collect that excess. The trade loses only when Nifty makes an unusually large move — typically ±2–3% or more in a single week — which happens but is the minority case.

**This is not market prediction. It is systematic premium collection.**

---

## 2. The Two Regimes

NSE changed Nifty 50 weekly expiry from Thursday to Tuesday effective **September 1, 2025**, under SEBI's directive to limit each exchange to one weekly index options contract.

| Regime | Period | Expiry Day | Entry Day | Exit Day | Hold |
|--------|--------|-----------|-----------|----------|------|
| `thu_expiry` (2-day) | Jan 2023 – Aug 2025 | Thursday | Tuesday ~15:25 | Thursday ~15:25 | 2 sessions |
| `thu_expiry_4day` (4-day) | Jan 2023 – Aug 2025 | Thursday | **Monday** ~15:25 | Thursday ~15:25 | 4 sessions |
| `tue_expiry` (4-day) | Sep 2025 onwards | Tuesday | **Thursday** ~15:25 | Tuesday ~15:25 | 4 sessions |

**Recommended configuration going forward: `tue_expiry` — enter Thursday close, exit Tuesday close.**  
The 4-day hold dominates the 2-day hold in all metrics (win rate, avg P&L, max loss control).

---

## 3. The Ladder Structure

Three configurations were tested. All use **SELL** side only.

### 1L — Short Straddle (2 legs)
Sell ATM CE + Sell ATM PE

### 3L — Short Iron Butterfly extended (6 legs)  ← **Primary config**
Sell ATM-50 CE + Sell ATM-50 PE  
Sell ATM CE    + Sell ATM PE  
Sell ATM+50 CE + Sell ATM+50 PE

### 5L — Full ladder (10 legs)
Sell ATM-100 CE + Sell ATM-100 PE  
Sell ATM-50 CE  + Sell ATM-50 PE  
Sell ATM CE     + Sell ATM PE  
Sell ATM+50 CE  + Sell ATM+50 PE  
Sell ATM+100 CE + Sell ATM+100 PE

**Strike step = 50 points** (each rung of the ladder is ±50 from ATM).

---

## 4. Strike Selection — Exact Process

### Step 1: Identify entry day
- **New regime (Sep 2025+):** Entry day = Thursday of the current week
- **Old regime (pre-Sep 2025):** Entry day = Tuesday of the current week (4-day) or Monday (2-day)

### Step 2: Watch Nifty spot at ~15:20 IST on entry day
- Use Nifty 50 index (not Bank Nifty, not futures — though futures are ±20-50pt of spot)
- The futures price is technically more accurate for options pricing, but spot works for ATM selection since you're centering ±50 around it

### Step 3: Round to nearest 50
```
ATM = round(spot / 50) * 50

Examples:
  23788 → 23800
  23812 → 23800  (rounds down, 23800 is closer)
  23825 → 23850  (midpoint rounds up in Python)
  24151 → 24150
```

### Step 4: Build the 6 strikes (3L)
```
Strike 1: ATM - 50  (e.g. 23750)
Strike 2: ATM       (e.g. 23800)
Strike 3: ATM + 50  (e.g. 23850)
```
For each strike → sell 1 lot CE + sell 1 lot PE = 6 legs total.

### Step 5: Check option chain
Before placing orders, open Nifty option chain and verify:
- The three strikes have reasonable open interest (avoid illiquid far strikes)
- Bid-ask spreads are within 1–2 pts (normal near expiry for liquid strikes)
- You're selling into the front-week expiry contract (the one expiring the coming Tuesday/Thursday)

---

## 5. Entry Execution

**Time:** 15:20–15:28 IST on entry day (before the last candle closes at 15:30)

**Order type:** Limit orders at LTP (last traded price) or best bid. Avoid market orders — options spreads widen in the last few minutes.

**Sequence:**
1. Place all 6 SELL orders simultaneously (or within 2-3 minutes)
2. Use the Nifty futures price visible at 15:20 to confirm ATM hasn't shifted since 15:15
3. If Nifty moves >25 points between 15:15 and 15:28, recalculate ATM before placing

**Per-leg entry:** Each leg = 1 lot = 75 shares (current Nifty lot size — verify this before trading as SEBI periodically revises).

**Record at entry:**
- Entry timestamp (IST)
- Nifty spot at entry
- ATM strike selected
- All 6 strikes and option types
- Entry LTP for each leg
- Total premium collected (sum of all 6 leg LTPs)
- Margin blocked (from broker statement)

---

## 6. Exit Execution

**Passive exit (recommended):** Hold all 6 legs until expiry. On expiry day (Tuesday in new regime, Thursday in old):
- ITM options will be exercised automatically by exchange at intrinsic value
- OTM options expire worthless (you keep full premium)
- Near-ATM options may have a few points of residual value — optionally buy back at 15:20 to avoid pin risk

**Active exit option:** Close all legs at 15:25 on expiry day via buy orders. Cleaner P&L accounting, avoids exercise logistics.

**Do NOT:**
- Set stop losses mid-week (defeats the theta-harvest purpose; creates more losses from whipsaws)
- Adjust/roll positions mid-week (adds complexity; the backtest does not include this)
- Exit early because the position looks bad mid-week

**The only exception:** If Nifty moves >150 points from entry ATM mid-week (3× the ±50 ladder width), consider taking a loss. This is not in the backtest but is prudent risk management for tail events.

---

## 7. Backtest Results Summary

All results are **per lot** (1 lot = 75 shares). Brokerage estimated at Rs 20/leg/lot.

### New regime (Sep 2025+) — 4-day hold, Thu entry → Tue exit, 38 cycles

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss | Avg Entry Premium |
|--------|--------|-------|---------------|-----------|-----------------|-------------------|
| 1L_SELL | 32 | 81.2% | +Rs 8,682 | +Rs 2.78L | -Rs 4,416 | Rs 276 |
| **3L_SELL** | **36** | **83.3%** | **+Rs 23,805** | **+Rs 8.57L** | **-Rs 7,792** | **Rs 760** |
| 5L_SELL | 38 | 86.8% | +Rs 38,195 | +Rs 14.5L | -Rs 10,806 | Rs 1,215 |

### Old regime (Jan 2023–Aug 2025) — 4-day hold, Mon entry → Thu exit, 123 cycles

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss | Avg Entry Premium |
|--------|--------|-------|---------------|-----------|-----------------|-------------------|
| 1L_SELL | 118 | 78.8% | +Rs 6,264 | +Rs 7.39L | -Rs 7,578 | Rs 224 |
| **3L_SELL** | **123** | **81.3%** | **+Rs 17,808** | **+Rs 21.9L** | **-Rs 17,572** | **Rs 637** |
| 5L_SELL | 128 | 82.8% | +Rs 29,201 | +Rs 37.4L | -Rs 24,391 | Rs 1,035 |

### Old regime — 2-day hold, Tue entry → Thu exit, 133 cycles

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss |
|--------|--------|-------|---------------|-----------|-----------------|
| 1L_SELL | 128 | 69.5% | +Rs 4,120 | +Rs 5.27L | -Rs 8,819 |
| 3L_SELL | 132 | 69.7% | +Rs 11,977 | +Rs 15.8L | -Rs 22,249 |
| 5L_SELL | 133 | 71.4% | +Rs 20,906 | +Rs 27.8L | -Rs 27,238 |

**Key finding:** 4-day hold beats 2-day hold on every metric — higher win rate, higher avg P&L, and *lower* max loss. This is counter-intuitive but makes sense: more days of theta decay means more of the premium is collected before the option goes to near-zero, so most weeks you're cutting a well-decayed position rather than a freshly-alive one.

---

## 8. Capital Requirements

For **3L_SELL (6 legs), 1 lot each:**

| Item | Estimate |
|------|---------|
| SPAN margin (approximate) | Rs 80,000–1,20,000 |
| Exposure margin | Rs 20,000–40,000 |
| **Total margin blocked** | **~Rs 1.0–1.5L** |
| Premium collected (credited) | Rs 700–1,600 (varies with VIX) |
| Net capital required | ~Rs 1.0–1.5L per weekly position |

Note: SEBI netting rules mean the 6-leg position gets significant margin benefit vs 6 naked positions. Check your broker's margin calculator with the exact strikes before your first trade.

For **5L_SELL (10 legs):** Expect ~Rs 1.5–2.0L margin.

**Scalability:** Every additional lot multiplies P&L AND margin linearly. 5 lots of 3L_SELL = Rs 5–7.5L margin, avg ~Rs 1.19L profit per week at scale.

---

## 9. Risk Profile

### What kills this trade
1. **Large directional move (>2% in 2 days):** Budget announcements, election results, RBI policy surprises, geopolitical events. These are the max-loss events visible in the data.
2. **VIX spike:** A sudden VIX expansion means you sold cheap and the options are now worth more. This appears as mid-week mark-to-market loss even if you eventually hold to expiry.
3. **Back-to-back bad weeks:** The strategy has 17–19% losing weeks. Two consecutive losses halve the quarterly gain.

### What doesn't kill this trade
- Normal trending weeks (Nifty up or down 0.5–1.5%): The ±50 ladder absorbs this
- Slow grinding moves: Theta decay outpaces delta loss
- Low-VIX environments: Less premium collected but win rate is higher

### Position sizing guideline
- Never deploy more than 25–30% of total trading capital in this strategy in a given week
- Keep 3× max-loss-week as cash reserve
- Do not increase lots after a loss week (no martingale)

---

## 10. Holiday Handling

When the **entry day** (Thursday in new regime) is a market holiday:
- Advance entry to the previous trading day (Wednesday)
- Same process — last 5-min bar, round to nearest 50

When the **expiry day** (Tuesday in new regime) is a market holiday:
- NSE moves expiry to the preceding Wednesday
- Your exit is now Wednesday close, not Tuesday
- The backtest handles this automatically (holiday fallback built in)

**Public holidays to watch for Nifty options (check NSE calendar each year):**
Republic Day, Holi, Ram Navami, Mahavir Jayanti, Good Friday, Eid, Independence Day, Ganesh Chaturthi, Dussehra, Diwali (Muhurat trading day), Gurunanak Jayanti, Christmas

---

## 11. Two-Week Simulation Protocol

Before going live, run this paper-trading drill for 2 weeks:

### Week 1 — Entry Thursday Jun 26, 2026 / Exit Tuesday Jul 1, 2026

**On Thursday Jun 26 at 15:20 IST:**
1. Note Nifty spot on terminal
2. Calculate ATM (round to nearest 50)
3. Open option chain for Jul 1 expiry
4. Record the following for all 6 legs:

| Leg | Strike | Type | LTP at 15:20 | LTP at 15:28 | Would-be fill |
|-----|--------|------|--------------|--------------|---------------|
| 1 | ATM-50 | CE | | | |
| 2 | ATM-50 | PE | | | |
| 3 | ATM | CE | | | |
| 4 | ATM | PE | | | |
| 5 | ATM+50 | CE | | | |
| 6 | ATM+50 | PE | | | |

5. Record total premium (sum of column 4 or 5)
6. Check margin requirement on broker app

**On Monday Jun 30 and Tuesday Jul 1:**
1. Note option chain LTPs at 15:20
2. Fill in exit premiums
3. Calculate P&L: (entry_premium - exit_premium) × 75 - brokerage

**Repeat for Week 2:** Entry Thursday Jul 3 / Exit Tuesday Jul 8

### Simulation success criteria
- You successfully identified ATM on both entry days within 5 minutes
- You understand which expiry contract you're selling (front week, not next)
- You can see the margin requirement before placing orders
- You tracked P&L correctly on both exit days

---

## 12. EasyTerminal — Options Tab Design

### New tab: "Options [F4]" or sub-tab under SIM

Add to `app.py` as a new `TabPane` inside the existing `TabbedContent`:
```python
with TabPane("Options  [F4]", id="tab-options"):
    yield OptionsPanel(id="options-panel")
```

### `OptionsPanel` — layout

```
┌─ NIFTY WEEKLY OPTIONS ──────────────────────────────────────────────────────┐
│ Regime: tue_expiry  │  Entry: Thu 2026-06-26 15:27  │  Expiry: Tue 2026-07-01
│ Entry Spot: 24178   │  ATM: 24200                   │  Days left: 4 / Hold: 4
├─────────────────────────────────────────────────────────────────────────────┤
│  Strike  │ Type │ Lots │ Entry LTP │ LTP (live) │ P&L pts │ P&L Rs  │ Status
│──────────┼──────┼──────┼───────────┼────────────┼─────────┼─────────┼───────
│  24150   │  CE  │  1   │   85.40   │   42.10    │ +43.30  │ +3248   │ OPEN
│  24150   │  PE  │  1   │  102.20   │   58.90    │ +43.30  │ +3248   │ OPEN
│  24200   │  CE  │  1   │  112.80   │   65.30    │ +47.50  │ +3563   │ OPEN
│  24200   │  PE  │  1   │   98.60   │   52.10    │ +46.50  │ +3488   │ OPEN
│  24250   │  CE  │  1   │   88.10   │   48.20    │ +39.90  │ +2993   │ OPEN
│  24250   │  PE  │  1   │  112.50   │   66.30    │ +46.20  │ +3465   │ OPEN
├─────────────────────────────────────────────────────────────────────────────┤
│ TOTAL ENTRY PREMIUM: Rs 599.60  │  CURRENT: Rs 332.90  │  NET P&L: +Rs 20,003
│ Margin blocked: ~Rs 1.20L       │  Premium decay: 44.5% │  Win if < Rs 599.60
└─────────────────────────────────────────────────────────────────────────────┘
```

### Data model — add to `models.py`

```python
@dataclass
class ETOptionsLeg:
    strike: int
    option_type: str       # "CE" or "PE"
    lots: int
    entry_ltp: float
    current_ltp: float     # updated live via ZMQ ticks
    entry_time: str        # "2026-06-26 15:27"
    status: str            # "OPEN" | "EXPIRED" | "CLOSED"

    @property
    def pnl_pts(self) -> float:
        return self.entry_ltp - self.current_ltp   # SELL: profit when price falls

    @property
    def pnl_rs(self) -> float:
        return self.pnl_pts * 75 * self.lots

@dataclass
class ETOptionsPosition:
    regime: str            # "tue_expiry" or "thu_expiry_4day"
    entry_date: str        # "2026-06-26"
    expiry_date: str       # "2026-07-01"
    entry_spot: float
    atm_strike: int
    legs: list[ETOptionsLeg]
    margin_blocked: float  # from broker

    @property
    def total_entry_premium(self) -> float:
        return sum(leg.entry_ltp for leg in self.legs)

    @property
    def total_current_premium(self) -> float:
        return sum(leg.current_ltp for leg in self.legs)

    @property
    def total_pnl_rs(self) -> float:
        return sum(leg.pnl_rs for leg in self.legs)

    @property
    def days_to_expiry(self) -> int:
        from datetime import date
        return (date.fromisoformat(self.expiry_date) - date.today()).days
```

### Persistence — options trade log

Store each completed options cycle in `data/options_journal.jsonl` (one JSON per line):

```json
{
  "regime": "tue_expiry",
  "entry_date": "2026-06-26",
  "expiry_date": "2026-07-01",
  "entry_spot": 24178.0,
  "atm_strike": 24200,
  "legs": [
    {"strike": 24150, "option_type": "CE", "lots": 1, "entry_ltp": 85.40, "exit_ltp": 2.10, "entry_time": "2026-06-26 15:27", "exit_time": "2026-07-01 15:28"},
    {"strike": 24150, "option_type": "PE", "lots": 1, "entry_ltp": 102.20, "exit_ltp": 8.30, "entry_time": "2026-06-26 15:27", "exit_time": "2026-07-01 15:28"},
    ...
  ],
  "total_entry_premium": 599.60,
  "total_exit_premium": 44.10,
  "gross_pnl_pts": 555.50,
  "gross_pnl_rs": 41662.50,
  "brokerage_rs": 120.0,
  "net_pnl_rs": 41542.50,
  "margin_blocked": 120000.0,
  "outcome": "WIN"
}
```

### Stats tab — add options section
Alongside the existing MCIC/DCVWAP stats, add:
- Total options cycles completed
- Win rate (%)
- Total premium collected (all-time)
- Total net P&L (all-time)
- Current open position P&L

---

## 13. Live Architecture Roadmap

### Phase 1: Manual + monitoring (now)
```
You (manual) → place orders in Dhan app at 15:20
→ record entry in options_journal.jsonl manually
→ EasyTerminal Options tab reads journal file + shows live LTPs via tick
```

### Phase 2: Semi-automated entry signal
```
Script runs at 15:15 on entry day
→ fetches Nifty spot from Dhan REST API
→ calculates ATM + 6 strikes
→ prints order slip to terminal / sends Telegram alert
You → confirm + place orders manually
```

### Phase 3: Fully automated
```
Cron at 15:20 on entry day
→ calculate strikes
→ place 6 SELL orders via Dhan /orders API
→ log to options_journal.jsonl
→ Cron at 15:25 on exit day
→ place 6 BUY orders (close position)
→ calculate final P&L and log
```

**Dhan API endpoints needed for live execution:**
- `GET /positions` — verify legs are open
- `POST /orders` — place SELL/BUY orders
- `GET /orders/{order_id}` — confirm fill price
- `POST /charts/rollingoption` — already built (fetcher.py)

### ZMQ feed for live LTP in EasyTerminal
The existing `tick_service.py` (TradingWebSockets P2) already publishes ticks on ZMQ port 5555.  
For options, Dhan WebSocket security IDs for each strike need to be subscribed dynamically.  
The tick format is identical — just different security IDs.

---

## 14. Entry-Day Checklist (Laminate This)

```
[ ] Check NSE market holiday calendar — is today actually a trading day?
[ ] Is it Thursday? (new regime) / Monday? (old regime comparison)
[ ] At 15:15 — open Nifty option chain for front-week expiry
[ ] At 15:20 — note Nifty spot: ___________
[ ] Round to nearest 50: ATM = ___________
[ ] Confirm ATM-50 = ___, ATM = ___, ATM+50 = ___
[ ] Check bid-ask spread on all 6 legs (should be < 2pts each)
[ ] Check margin requirement on broker app: Rs ___________
[ ] Place 6 SELL limit orders (not market):
    [ ] ___CE SELL 1 lot at Rs ___
    [ ] ___PE SELL 1 lot at Rs ___
    [ ] ___CE SELL 1 lot at Rs ___
    [ ] ___PE SELL 1 lot at Rs ___
    [ ] ___CE SELL 1 lot at Rs ___
    [ ] ___PE SELL 1 lot at Rs ___
[ ] Confirm all 6 fills by 15:29
[ ] Record total entry premium collected: Rs ___________
[ ] Log to options_journal.jsonl
[ ] Set reminder for expiry day (Tue/Thu) at 15:20
```

---

## 15. Exit-Day Checklist

```
[ ] It is expiry day (Tuesday new regime / Thursday old regime)
[ ] At 15:20 — check option chain LTPs for all 6 legs
[ ] Calculate current mark-to-market P&L
[ ] Decision: let expire OR buy back before 15:30?
    → If all legs are OTM and LTP < Rs 5 each: let expire (save brokerage)
    → If any leg is ITM or LTP > Rs 20: buy back at 15:25 to avoid pin risk
[ ] Record exit premiums for each leg
[ ] Calculate net P&L: (entry_total - exit_total) × 75 - brokerage
[ ] Update options_journal.jsonl
[ ] Telegram notification to self with P&L
```

---

## 16. Key Numbers to Know

| Parameter | Value |
|-----------|-------|
| Nifty lot size | 75 shares |
| Strike step | 50 points |
| Entry time | ~15:20 IST (entry day) |
| Exit time | ~15:25 IST (expiry day) |
| 3L legs | 6 (sell 3 strikes × CE+PE) |
| Typical 3L entry premium | Rs 450–1,600 (depends on VIX) |
| Win rate (new regime, 3L) | 83% |
| Max observed loss (3L, new regime) | Rs 7,792 per lot |
| Avg profit (new regime, 3L) | Rs 23,805 per lot per week |
| Brokerage estimate | Rs 20/leg/lot = Rs 120 for 3L |

---

## 17. What This Strategy Is NOT

- Not a directional bet (we don't predict Nifty direction)
- Not intraday (no monitoring required during market hours)
- Not delta-hedged (pure short-vol, naked)
- Not suitable if you cannot monitor on entry/exit days at 15:20
- Not guaranteed profit — 17–19% of weeks are losers

The edge is statistical. It requires running this consistently for months, not cherry-picking. One bad week (election result, sudden rate decision) will wipe ~2 weeks of gains. The alpha comes from staying disciplined across 50+ cycles.

---

*Document generated from NiftyOptionsBacktest pipeline | Data: Dhan API | Storage: DuckDB + Parquet*  
*Last updated: 2026-06-27 | Backtest range: 2023-01-01 to 2026-06-26*
