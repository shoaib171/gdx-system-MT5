# Trade Management System — entry to exit

Adopted 2026-07-15. The signal engine (GDX-CORR scoring + confirmation) decides
WHEN to trade; this document defines everything that happens AFTER the entry
decision. Config keys in `config.py` under TRADE MANAGEMENT.

## 1. Entry
- Entry price: market (ask for BUY, bid for SELL) at the confirmed signal.
- **SL**: beyond the swing low/high of the last `SL_SWING_LOOKBACK` (10) closed
  candles, plus an ATR buffer (`SL_ATR_BUFFER` × ATR), or **minimum $5** —
  whichever is FARTHER from entry.
- **TP1** = entry + 2 × risk (`TP1_RR`) — a virtual management level.
- **TP2** = entry + 3 × risk (`TP2_RR`) — the real TP on the broker order.
- Lot: AUTO = (balance × risk%) ÷ (SL distance × $100); MANUAL = fixed lot.
- The full lot trades as a single unit — no partial closes anywhere.

## 2. When price touches TP1
- Nothing is closed.
- SL immediately moves to **entry + $3** (BUY) / entry − $3 (SELL)
  (`BE_CUSHION`) — breakeven plus cushion. The position is now risk-free.
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
| 🏆 TP2 hit | Full lot at final target (3R) |
| 🎯 Trailing SL hit before TP2 | "Dynamic TP2" — locked profit, counted a WIN |
| 🔻 Full SL before TP1 | Original 1R risk lost |
| ⛔ Watchdog force-close | Price crossed SL but broker didn't honor it → bot closes at market |
| ✋ Manual close | Dashboard "Close all" |

Win/loss is decided by the trade's TOTAL realized P/L (never assumed), and the
exit price always comes from the broker's deal history.

## Example
Entry $4100, swing SL $4090 (risk $10), TP1 $4120, TP2 $4130, lot 0.02.
1. Price reaches $4120 → SL jumps to $4103 — the 0.02 lot can no longer lose.
2. Price runs to $4128, trailing SL follows to ~$4122, price reverses → SL hit.
3. Result: 0.02 × ($4122 − $4100) ≈ +$44, logged as
   `🎯 Dynamic TP2 @ 4122 (trailing lock)` — a WIN even though $4130 never printed.

## Restart safety
The management state (ticket, entry, risk, TP1, tp1_done) persists in
`bot_state.json`. If the bot restarts with a position open, it restores — or,
if the state file is missing, reconstructs TP1 from the order's entry and TP2
(risk = (TP2 − entry) / 3) and resumes managing where it left off.
