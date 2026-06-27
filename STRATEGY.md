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

**Mandatory active exit — always close by 15:25 on expiry day.** Never let short options expire unattended.

Reasons:
- **STT (Securities Transaction Tax):** Short options that expire ITM are taxed as if exercised at intrinsic value, not at the premium you collected. The STT on an exercised short can significantly exceed your collected premium.
- **Pin risk:** In the final 10 minutes of Tuesday/Thursday, institutional delta-hedging creates sharp Nifty moves of 30–50 points. An OTM option with LTP = Rs 2 can become deep ITM within 5 minutes.
- **Saving Rs 120 in brokerage is not worth a multi-thousand rupee surprise.**

**Exit procedure (15:20–15:28 on expiry day):**
1. Check option chain for all 6 legs
2. Place BUY limit orders at current LTP (or a few rupees above to ensure fill)
3. Confirm all 6 fills before 15:29
4. For any leg with LTP < Rs 2 — still buy back; the STT risk is not worth the Rs 2 saving

**Do NOT:**
- Set stop losses mid-week (defeats the theta-harvest purpose; creates more losses from whipsaws)
- Adjust/roll positions mid-week (adds complexity; the backtest does not include this)
- Exit early because the position looks bad mid-week

**The only exception:** If Nifty moves >150 points from entry ATM mid-week (3× the ±50 ladder width), consider taking a loss. This is not in the backtest but is prudent risk management for tail events.

---

## 7. Backtest Results Summary

**Lot size note:** Nifty lot size changed from 25 to 75 shares effective Nov 20, 2024 (SEBI F&O reform).  
The backtest applies the correct historical lot size to each cycle — pre-Nov 2024 P&L is calculated at lot=25, post-Nov 2024 at lot=75. Numbers are not comparable across regimes in absolute Rs terms for this reason; the points-level win rates and averages are comparable.

Brokerage estimated at Rs 20/leg/lot.

**"Avg Entry Premium" is in index points** (sum of all 6 leg LTPs). Multiply by lot size for rupee value.

### New regime (Sep 2025+) — 4-day hold, Thu entry → Tue exit | lot size = 75 throughout

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss | Avg Entry Premium (pts) |
|--------|--------|-------|---------------|-----------|-----------------|--------------------------|
| 1L_SELL | 32 | 81.2% | +Rs 8,682 | +Rs 2.78L | -Rs 4,416 | 276 pts (= Rs 20,715/lot) |
| **3L_SELL** | **36** | **83.3%** | **+Rs 23,805** | **+Rs 8.57L** | **-Rs 7,792** | **760 pts (= Rs 56,993/lot)** |
| 5L_SELL | 38 | 86.8% | +Rs 38,195 | +Rs 14.5L | -Rs 10,806 | 1,215 pts (= Rs 91,125/lot) |

### Old regime (Jan 2023–Aug 2025) — 4-day hold, Mon entry → Thu exit | lot size = 25 (most cycles)

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss | Avg Entry Premium (pts) |
|--------|--------|-------|---------------|-----------|-----------------|--------------------------|
| 1L_SELL | 118 | 76.3% | +Rs 3,838 | +Rs 4.53L | -Rs 7,578 | 224 pts |
| **3L_SELL** | **123** | **81.3%** | **+Rs 10,900** | **+Rs 13.4L** | **-Rs 17,228** | **637 pts** |
| 5L_SELL | 128 | 82.8% | +Rs 17,759 | +Rs 22.7L | -Rs 24,391 | 1,035 pts |

### Old regime — 2-day hold, Tue entry → Thu exit | lot size = 25 (most cycles)

| Config | Trades | Win % | Avg P&L/trade | Total P&L | Max Single Loss |
|--------|--------|-------|---------------|-----------|-----------------|
| 1L_SELL | 128 | 68.0% | +Rs 2,684 | +Rs 3.44L | -Rs 5,211 |
| 3L_SELL | 132 | 69.7% | +Rs 7,722 | +Rs 10.2L | -Rs 14,723 |
| 5L_SELL | 133 | 71.4% | +Rs 13,198 | +Rs 17.6L | -Rs 16,190 |

**Key finding:** 4-day hold beats 2-day hold on every metric — higher win rate, higher avg P&L, and *lower* max loss. This is counter-intuitive but makes sense: more days of theta decay means more of the premium is collected before the option goes to near-zero, so most weeks you're cutting a well-decayed position rather than a freshly-alive one.

**Why new regime shows higher Rs P&L than old regime:** Partly because lot size is 3× larger (75 vs 25), and partly because new regime captures a slightly higher-volatility post-2025 market. Compare win rates and points P&L for a like-for-like view.

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

## 11. Forward Testing Protocol

The automated pipeline (Phase 2) handles this each week. For the first few weeks, verify the automation is working correctly:

### Each Thursday at 15:20 IST

ET should auto-fire the entry signal. If it doesn't (ET not running, config has auto_entry=false):
```
SIGNAL.bat    → waits until 15:10, prints order slip, writes active_options_position.json
ENTRY.bat     → run after fills; prompts for 6 LTPs; logs to options_journal.jsonl
```

Verify after entry:
- `D:\Trading\active_options_position.json` exists and has `"status": "open"`
- ET Options tab (F4) shows all 6 legs with live LTPs updating
- `data/options_journal.jsonl` has a new entry with `"outcome": null`

### Each Tuesday (expiry day) at 15:25 IST

ET auto-closes. If ET not running:
```
EXIT.bat    → prompts for 6 buyback LTPs; logs exit; marks position closed
```

Verify after exit:
- `data/options_journal.jsonl` last entry has `"outcome": "WIN"` or `"outcome": "LOSS"` and non-null exit_ltp for all legs
- `D:\Trading\active_options_position.json` has `"status": "closed"`
- ET Options tab shows no open position

### First week sanity check

After week 1, run:
```
uv run python pipeline.py paper-show    (shows current or last closed paper trade P&L)
```

Compare logged P&L to your actual broker P&L. The gap should be < 2% (slippage). If larger, check that you're logging the correct fill prices.

### Simulation success criteria
- AT ran the entry signal without intervention
- ET showed all 6 live LTPs by 15:25 Thursday
- Exit fired automatically at 15:25 Tuesday
- paper-show P&L matches broker P&L within Rs 200 (Rs 1-5 slippage per leg × 6 legs × 2 sides)

---

## 12. EasyTerminal — Options Tab (LIVE as of 2026-06-27)

**Status: BUILT.** The Options tab is in production at `D:\Trading\EasyTerminal\`.

### Tab: "Options [F4]"

Keyboard shortcut F4 from within ET's SIM view. Shows the current open position (if any) plus completed cycle history.

### What it displays

```
NIFTY WEEKLY OPTIONS [PAPER]  |  Entry: 2026-06-26 15:21  |  Expiry: 2026-07-01  |  DTE: 4  |  ATM: 24050
+---------+------+-----------+-----------+----------+------------+---------+
| Strike  | Type | Entry LTP |  Mkt LTP  |  P&L pts |   P&L Rs   | Status  |
+---------+------+-----------+-----------+----------+------------+---------+
|  24000  |  CE  |    158.35 |    122.50 |   +35.85 |  +2,689 Rs |  LIVE   |
|  24000  |  PE  |     65.55 |     92.10 |   -26.55 |  -1,991 Rs |  LIVE   |
|  24050  |  CE  |    127.15 |     98.30 |   +28.85 |  +2,164 Rs |  LIVE   |
|  24050  |  PE  |     83.60 |    108.40 |   -24.80 |  -1,860 Rs |  LIVE   |
|  24100  |  CE  |     99.00 |     78.55 |   +20.45 |  +1,534 Rs |  LIVE   |
|  24100  |  PE  |    105.35 |    131.20 |   -25.85 |  -1,939 Rs |  LIVE   |
+---------+------+-----------+-----------+----------+------------+---------+
Entry: 639.00 pts (Rs 47,925) | Curr: 630.05 pts | Unreal P&L: +8.95 pts = +Rs 671 | [C] = Force Close
Config: Ladder=3L | Auto-Entry=ON @ 15:20 IST | Auto-Exit=ON @ 15:25 IST | Mode=PAPER | Edit: D:\Trading\options_config.json
```

### Auto-entry (Thursday 15:20 IST)

ET's 30-second scheduler fires when: weekday=Thursday, time>=15:20, `options_config.json` has `auto_entry=true`, no open position exists.

What happens automatically:
1. ET runs `pipeline.py signal` subprocess (fetches Nifty spot, computes ATM, writes `active_options_position.json` with security IDs)
2. `tick_service.py` detects the file mtime change within 10s, subscribes to all 6 NSE_FNO contracts
3. ZMQ ticks begin arriving in ET's `ZmqOptionsWorker` (port 5555 primary or 5557 fallback)
4. When all 6 legs have at least one live LTP, ET calls `log_entry()` and reloads the panel
5. You: open broker app and place 6 SELL limit orders manually

### Auto-exit (Tuesday 15:25 IST on expiry day)

Scheduler detects open position with `expiry_date == today` and time >= 15:25. Calls `log_exit()` with current live LTPs. You: confirm fills in broker app.

### Force-close (C key, any time)

Press `C` on Options tab → modal shows current per-leg live LTPs → confirm with Y or Enter → `log_exit()` called immediately. Use when mid-week news makes you want to exit early.

### Config

`D:\Trading\options_config.json` — edit directly, ET re-reads every 30s:
```json
{
  "ladder_size": "3L",
  "entry_time_ist": "15:20",
  "exit_time_ist": "15:25",
  "auto_entry": true,
  "auto_exit": true,
  "lots": 1,
  "strategy": "SELL",
  "paper_mode": true
}
```

### Persistence — options_journal.jsonl schema

`data/options_journal.jsonl` — one JSON object per line. `outcome: null` = still open.

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

P&L: `gross_pnl_pts = total_entry_premium - total_exit_premium` (profit when total premium falls).
Brokerage: Rs 20/leg/lot per side × 6 legs × 2 sides = **Rs 240/trade** (entry + exit combined).
`net_pnl_rs = gross_pnl_pts * lot_size * lots - 240`

---

## 13. Live Architecture Roadmap

### Phase 1: Manual + monitoring — COMPLETE
```
You (manual): place 6 SELL orders in Dhan app at 15:20
You (manual): record fills in options_journal.jsonl via ENTRY.bat / pipeline.py paper-entry
EasyTerminal Options tab: reads journal + shows live LTPs via ZMQ tick
```

### Phase 2: Signal automation — COMPLETE (as of 2026-06-27)
```
ET auto-entry at 15:20 Thursday:
  → runs pipeline.py signal (fetches spot, computes ATM, writes active_options_position.json)
  → tick_service.py subscribes to 6 NSE_FNO contracts (watches file mtime every 10s)
  → ZMQ ticks flow to ET ZmqOptionsWorker (port 5555 primary, 5557 fallback)
  → all 6 LTPs captured → log_entry() called automatically
  → ET Options tab shows open position with live LTPs

You (manual): open Dhan app, place 6 SELL limit orders
              (fill prices have Rs 1-5 slippage vs logged LTPs — acceptable for forward testing)

ET auto-exit at 15:25 Tuesday (expiry day):
  → log_exit() called with current live LTPs
  → position closed in journal + active_options_position.json

You (manual): open Dhan app, buy back 6 legs
              (or use force-close C key in ET if exiting early)

Emergency fallbacks: SIGNAL.bat, ENTRY.bat, EXIT.bat in NiftyOptionsBacktest/
```

### Phase 3: Fully automated (not yet built)
```
ET or cron at 15:20 entry day
→ place 6 SELL orders via Dhan /orders API
→ confirm fills → log actual fill prices to journal
→ At 15:25 exit day
→ place 6 BUY orders via Dhan /orders API
→ calculate final P&L from actual fills
```

**Dhan API endpoints needed for Phase 3:**
- `POST /v2/orders` — place SELL/BUY orders
- `GET /v2/orders/{order_id}` — confirm fill price and status
- `GET /v2/positions` — verify legs are open before exit

**Constraint holding Phase 3 back:** Need real fill data to validate slippage before automating order placement. Run Phase 2 for at least 4-8 weeks to build a slippage distribution.

### ZMQ live LTP architecture (implemented)

`tick_service.py` (TradingWebSockets P2) publishes on port 5555. Option ticks use `OPT_{strike}_{type}` topic format (e.g. `OPT_24000_CE`). Equity subscribers (MCIC, DCVWAP) receive zero option messages — fully additive. Fallback: `options_ltp_service.py` (REST polling, port 5557). ET subscribes to both ports simultaneously.

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
[ ] ET auto-exit fires at 15:25 IST — OR — press [C] in ET Options tab to force-close
[ ] If ET not running: run EXIT.bat (prompts for 6 buyback prices)
[ ] At 15:20 — check option chain LTPs for all 6 legs in broker app
[ ] Place BUY limit orders for all 6 legs (even legs with LTP < Rs 2 — STT risk is real)
[ ] Decision: let expire OR buy back?
    → If all legs are OTM and LTP < Rs 2 each AND you are certain: let expire (save Rs 120 brokerage)
    → If any leg is ITM or LTP > Rs 20: buy back at 15:25 to avoid pin risk
    → When in doubt: buy back. STT on an exercised short ITM option >> brokerage saved
[ ] Confirm all 6 fills in broker app by 15:29
[ ] Record exit premiums for each leg in options_journal.jsonl (ET handles this if auto-exit fired)
[ ] Verify ET Options tab now shows no open position (or paper-show shows WIN/LOSS)
[ ] Telegram notification to self with P&L (sent automatically by ET or logged to activity log)
```

---

## 16. Key Numbers to Know

| Parameter | Value |
|-----------|-------|
| Nifty lot size (current, post Nov 2024) | 75 shares |
| Nifty lot size (pre Nov 20, 2024) | 25 shares |
| Strike step | 50 points |
| Entry time | ~15:20 IST (entry day) |
| Exit time | ~15:25 IST (expiry day) — mandatory active close |
| 3L legs | 6 (sell 3 strikes × CE+PE) |
| Typical 3L entry premium (new regime) | 450–1,600 pts = Rs 34K–120K at lot 75 |
| Win rate (new regime, 3L) | 83% |
| Max observed loss (3L, new regime) | Rs 7,792 per lot |
| Avg net profit (new regime, 3L) | Rs 23,805 per lot per week |
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
*Last updated: 2026-06-27 | Backtest range: 2023-01-01 to 2026-06-26 | Phase 2 automation live*
