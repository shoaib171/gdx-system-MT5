# Entry Quality Filters — plan, reasoning and tuning guide

Written after the 2026-07-14 session. Keep this file updated when any filter's
config value changes — it is the memory of WHY these exist.

## The case study that created this file (2026-07-14)

A news spike at 17:30 PKT moved gold ~$54 in one M15 candle (4038 → 4092).
The bot's response exposed three structural weaknesses:

| Trade | Entry | What happened | Root cause |
|---|---|---|---|
| 1 | 4079.01 @ 17:30:07 | SL in 21 seconds, −705.50 | Regime flipped −0.46 → −0.83 in **7 seconds** because the forming spike candle entered the rolling correlation; entry fired the same second, unconfirmed. ATR (9.4) was from the calm morning → SL $14 wide in a $54-candle market. |
| 2 | 4092.77 @ 17:45 | SL in 8 min, −735.50 | Confirmed, but bought the **top** of the spike (price ~5×ATR above EMA21); retrace hit SL. |
| 3 | 4077.82 @ 18:00 | +257.50 (closed manually) | Entered **after** the spike settled, near EMA — the only good entry of the day. |

Net: −1183.50. Filters below would have blocked trades 1 and 2 and allowed 3.

## The four filters

All are **execution gates** — scoring, SL/TP and risk sizing are untouched.
Each has a config switch; blocked entries are logged locally (never Discord)
with the reason, so the daily JSONL/report shows what was skipped and why.

### 1. Entry confirmation (`ENTRY_REQUIRES_CONFIRMATION = True`)
Entries now require the same next-candle confirmation that notifications use:
a firing signal must still fire on a NEW M15 candle before the bot may enter.
Kills the "score crossed 75 for one second during a spike" entry (trade 1).
Max cost: 15 minutes. A signal that cannot survive one candle is exactly the
signal we don't want to trade.

### 2. Spike filter (`SPIKE_BAR_ATR_RATIO = 2.5`, 0 = off)
No entry while the current or last candle's range exceeds 2.5 × ATR(14).
On 2026-07-14 the spike candle was **5.7 × ATR** — both losing trades blocked.
Dormant on normal days (a normal candle never approaches 2.5×ATR), which is
the point: news doesn't happen every day, so this filter costs nothing on
quiet days and saves the account on violent ones. Analysis and signals
continue; only execution waits for the market to settle.

### 3. Overextension filter (`MAX_EXTENSION_ATR = 1.5`, 0 = off)
No BUY when price is more than 1.5 × ATR above EMA21 (mirror for SELL).
Momentum scoring is loudest at the top of a move — exactly the worst entry.
Trade 2 (price ~5×ATR above EMA21) blocked; trade 3 (back near EMA) allowed.
This filter waits for the pullback instead of chasing.

### 4. Regime stability (`REGIME_CLOSED_BARS_ONLY = True`, `REGIME_MIN_BARS = 1`)
The regime (corr ≤ −0.60) is meant to be an *established market state*, not a
tick-level signal — but it was computed on the forming candle every 5 seconds,
so one monster bar could activate it in 7 seconds (trade 1).
Now the correlation used for the regime comes from **closed candles only**,
and must hold for `REGIME_MIN_BARS` consecutive closed bars.

- `REGIME_MIN_BARS = 1` (current): max 15-min delay — kills intra-second flips.
- `REGIME_MIN_BARS = 2`: max 30-min delay — also survives a single closed
  spike bar. Escalate to 2 if the daily reports show regime flip-flopping.

Why the delay is cheap: the regime lasts hours (on 2026-07-14 it stayed at
corr −0.89…−0.91 from 17:30 to past 21:00). The wait happens once per regime
activation, not per trade — after activation the bot trades at full speed.

## The objection we already debated (so we don't re-litigate it)

*"Won't waiting make the bot miss the big move?"* — The data said no:
the two entries **during** the spike lost −1441; the one entry **after** it
made +257 at a better price than the spike-top entry (4077 vs 4092), because
spikes whipsaw and the retrace hands the patient entry a discount. This
strategy earns the 2R slice of a multi-hour regime trend, which a 15–30 min
later entry still captures; it was never designed to catch the spike itself.

## Tuning protocol

1. Run at least a week. The daily reports + JSONL (`logs/`) record every
   blocked entry with its reason and the market state at that moment.
2. For each blocked entry ask: would it have hit SL or TP? (Bar events in the
   JSONL give the price path.)
3. Blocked entries that would have WON consistently → loosen that one filter
   (e.g. `SPIKE_BAR_ATR_RATIO` 2.5 → 3.0, `MAX_EXTENSION_ATR` 1.5 → 2.0).
4. Fake regime flips still visible → `REGIME_MIN_BARS` 1 → 2.
5. Any filter can be disabled instantly (set ratio/ATR to 0, booleans False)
   — no code changes needed.
