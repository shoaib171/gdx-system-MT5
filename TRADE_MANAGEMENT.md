# Trade Management System — entry to exit

Adopted 2026-07-15. The signal engine (GDX-CORR scoring + confirmation) decides
WHEN to trade; this document defines everything that happens AFTER the entry
decision. Config keys in `config.py` under TRADE MANAGEMENT.

Every distance is planned from live market structure — swing levels and current
ATR — nothing is a fixed dollar number.

## The idea (owner's design)
The bot plans each trade the way a trader would read the chart — no arbitrary
numbers: SL behind real structure, TP1 at a level price has actually visited
and reacted to before, and only once price proves the trade (reaches TP1) does
the stop start moving with ATR.

## 1. Entry
- Entry price: market (ask for BUY, bid for SELL) when the signal scores ≥ 75.
- **TP1** (`TP_MODE = "STRUCTURE"`): the nearest S/R zone in the trade's
  direction — fractal swing points over the last `SR_LOOKBACK` (120) closed
  candles, clustered within `SR_CLUSTER_ATR` × ATR. If no zone pays at least
  `MIN_TP1_RR` × risk, the entry is skipped ("no room to target").
  **TP2** = the next zone beyond TP1 (fallback: 2 × TP1 distance).
  `TP_MODE = "RR"` uses fixed multiples (`TP1_RR`/`TP2_RR`) instead.
- **SL** (`SL_MODE`):
  - `"ATR"` (default, backtest winner): `SL_ATR_MULT` (1.5) × ATR from entry.
  - `"SWING"`: beyond the 10-candle swing low/high + `SL_ATR_BUFFER` × ATR,
    minimum `SL_MIN_ATR` × ATR.
  - **Why ATR is the default** — 30-day backtest ($50k, identical management):
    ATR SL → 19 trades, 53% win, PF 1.26, **+$555**; swing SL → 11 trades,
    36% win, PF 0.42, **−$811**. Wide swing stops inflate 1R so TP1/TP2 sit
    too far away: winners get clipped at ~0.6R by the trail while losers pay
    the full 1R. Tight stops bring TP1 close → breakeven protection engages
    often (win rate 36% → 53%) and TP2 actually gets hit.
- **TP1** = entry + 1 × risk (`TP1_RR`, **1:1**) — a virtual management level.
- **TP2** = entry + 2 × risk (`TP2_RR`, **1:2**) — the real TP on the broker order.
- Lot: AUTO = (balance × risk%) ÷ (SL distance × $100); MANUAL = fixed lot.
- The full lot trades as a single unit — no partial closes anywhere.

## Backtest verdict (30 days, $50k, confirmed entries — same management everywhere)

| SL | Targets | Trades | Win% | PF | Net |
|---|---|---|---|---|---|
| swing | S/R strict (touches≥2, RR≥0.8) | 3 | 0% | 0.00 | −$692 (90 entries skipped "no target") |
| ATR 1.5× | S/R strict | 4 | 25% | 0.66 | −$251 (91 skipped) |
| swing | RR 1:1/1:2 | 11 | 36% | 0.42 | −$821 |
| **ATR 1.5×** | **S/R loose (touches≥1, RR≥0.3)** | **20** | **50%** | **1.21** | **+$501** ← deployed |
| ATR 1.5× | RR 1:1/1:2 | 19 | 53% | 1.26 | +$559 (one switch away) |

Two hard lessons: (1) swing stops lost in every combination — they inflate 1R
until no target is reachable; (2) strict S/R guards skip ~90% of entries
because this is a momentum system: price is usually breaking INTO fresh
territory when the signal confirms, so demanding a far, multi-tested zone
ahead of it rejects nearly everything. The loose settings keep the structure
idea and match RR-mode performance.

## 2. When price touches TP1 (1:1)
- Nothing is closed.
- SL immediately moves to **entry ± `BE_CUSHION_ATR` × ATR** (dynamic cushion,
  measured from the ATR at that moment) — breakeven plus cushion. The position
  is now risk-free.
- The SL move is retried up to `SL_MODIFY_RETRIES` (4) times, 0.5 s apart.
  If the broker rejects all attempts, the bot **safety-closes the full lot at
  market** rather than leave it at risk.

## 3. After TP1 — trailing stop
- SL trails price at a gap of `TRAIL_ATR_MULT` (1.5) × current ATR — dynamic:
  tighter in calm markets, wider in volatile ones.
- Moves only forward (locks profit), never backward, and never below the
  breakeven cushion. Broker modifications are throttled to `TRAIL_MIN_STEP`
  ($0.50) improvements so the server isn't spammed every 5 seconds.

## 4. The five exits
| Exit | Meaning |
|---|---|
| 🏆 TP2 hit | Full lot at final target (2R) |
| 🎯 Trailing SL hit before TP2 | "Dynamic TP2" — locked profit, counted a WIN |
| 🔻 Full SL before TP1 | Original 1R risk lost |
| ⛔ Watchdog force-close | Price crossed SL but broker didn't honor it → bot closes at market |
| ✋ Manual close | Dashboard "Close all" |

Win/loss is decided by the trade's TOTAL realized P/L (never assumed), and the
exit price always comes from the broker's deal history.

## Example (illustrative numbers — the bot derives its own from the market)
Entry $4100, swing SL $4090 (risk $10), TP1 $4110 (1:1), TP2 $4120 (1:2),
ATR ≈ $8, lot 0.02.
1. Price reaches $4110 → SL jumps to ~$4102.4 (entry + 0.3×ATR) — risk-free.
2. Price runs to $4118, trailing SL (1.5×ATR behind) follows, price reverses
   → trailing SL hit at ~$4106.
3. Result: 0.02 × ($4106 − $4100) ≈ +$12, logged as
   `🎯 Dynamic TP2 @ 4106 (trailing lock)` — a WIN even though $4120 never printed.

## Restart safety
The management state (ticket, entry, risk, TP1, cushion, tp1_done) persists in
`bot_state.json`. If the bot restarts with a position open, it restores — or,
if the state file is missing, reconstructs TP1 from the order's entry and TP2
(risk = (TP2 − entry) / TP2_RR) and resumes managing where it left off.
