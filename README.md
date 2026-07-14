# GDX-CORR — Gold/Dollar Correlation Trading System

XAUUSD ↔ DXY inverse-correlation engine with live dashboard, scored signals,
next-candle confirmation, daily JSON diagnostics and optional auto-trading on
MetaTrader 5.

## How the signal works
1. **Regime filter** — rolling 50-bar correlation between gold & DXY returns.
   Tradeable only when corr ≤ −0.60 (strong inverse regime).
2. **Direction** — trade gold AGAINST dollar momentum:
   DXY falling (EMA9 < EMA21 + negative ROC) → **BUY** gold, and vice versa.
3. **Confirmation** — gold's own EMA alignment must agree.
4. **Decoupling bonus** — correlation z-score break ≥ 1.5σ.
5. **Session filter** — London/NY overlap, 1:00–9:30 PM PKT.

Score out of 100. Signal fires at ≥70, auto-trade executes at ≥75.

A firing signal must **still fire on the next 15-min candle** to be *confirmed* —
only confirmed signals are announced on Discord. A confirmed signal opposite to
the open position (score ≥ 75) closes it early (`EXIT_ON_OPPOSITE` in config).

## Operating hours (PKT — Asia/Karachi)
| Window | Behaviour |
|---|---|
| 03:00 – 21:30 | Full trading: analysis + signals + entries |
| 21:30 – 01:00 | Analysis + signals only — **no new entries** |
| 01:00 – 03:00 | Market closed (gold & DXY) — engine idles |
| 03:00 | Fresh trading day: stats reset, daily report generated |

The trading day rolls at **03:00**, not midnight — overnight activity stays in
one day's stats and logs.

## Risk management (built in)
- Lot sizing: **AUTO** (equity risk %, default 0.5%) or **MANUAL** (fixed lot) —
  switchable from the dashboard; trades are always taken by the bot
- SL = 1.5 × ATR(14), TP = 2R
- 1 position max
- **Daily loss limit / profit target ($)** — set from the dashboard; when either
  is hit the bot keeps analyzing and signalling but takes no entries until the
  next trading day
- 45-min cooldown after a loss
- **HALT after 3 consecutive losses** — survives restarts and day rollover;
  trading resumes only via the dashboard RESUME button
- Once-per-bar execution guard (no whipsaw re-entry on same candle)
- Magic number 77201 — never touches your manual positions

## Dashboard (port 5077)
- **Live scanner** — shows which analysis phase the engine is in right now
- Live signal + score breakdown, confirmation status (⏳ pending / ✅ confirmed)
- Correlation meter, gold/DXY overlay charts
- **Today's Trades** card — executed, wins, losses, win rate
- **Trade Settings** card — daily loss/profit limits, lot mode, manual lot
- Halt banner with RESUME button, AUTO-TRADE toggle, Close-all kill switch
- Auto-trade starts **OFF** every run

## Discord notifications (only these — no log spam)
| Event | Example |
|---|---|
| Bot start / stop | 🚀 Bot ACTIVE / 🛑 Bot CLOSED |
| Confirmed signal | ✅ SIGNAL CONFIRMED BUY — score 92, corr −0.72 |
| Entry | 🟩 ENTRY BUY 0.10 lots @ 4055.00 \| SL … \| TP … |
| SL / TP hit | 🔴 SL hit … / 🟢 TP hit … |
| Opposite-signal close | ⚠️ Closed SELL at P/L +891 — confirmed opposite BUY |
| Daily limit / halt / resume | 🛑 / 🎯 / ⛔ / ▶️ |
| Daily report (03:00) | 📊 Daily report: 4 trades, 3W/1L, P/L +320.50 … |

## Monitoring & diagnostics
- `logs/<day>.jsonl` — every event as one JSON line: per-candle `bar` snapshots
  (price, corr, score, direction), `entry`, `close`, `signal_confirmed`,
  `log`, `error`
- `logs/report_<day>.json` — daily aggregate, auto-built at 03:00
- `GET /api/report` — today's live report; `?day=YYYY-MM-DD` for past days
- `bot_state.json` — daily stats, settings and halt state persist across
  restarts; positions closed while the bot was off are detected and counted
  on the next start

## Setup (Windows + MT5 terminal)
```
pip install -r requirements.txt
```
1. Open `config.py`:
   - `MT5_LOGIN / MT5_PASSWORD / MT5_SERVER` — or leave empty to attach to an
     already-logged-in terminal.
   - Set `GOLD_SYMBOL` exactly as in Market Watch (`XAUUSD` on MetaQuotes-Demo,
     `XAUUSDm` on Exness Standard). `DXY_COMPONENTS` pair names must match the
     broker too.
   - Paste `DISCORD_WEBHOOK_URL` for alerts.
2. Enable **Algo Trading** in the MT5 terminal (toolbar button + Tools →
   Options → Expert Advisors).
3. Run:
```
python app.py
```
4. Open `http://localhost:5077` (or `http://<VPS-IP>:5077` from your phone —
   open port 5077 in Windows Firewall).

## DXY source
The engine first looks for a broker dollar-index symbol (`DXY_CANDIDATES`).
If none exists it builds a **synthetic DXY** from the 6 official ICE component
pairs with correct geometric weights — identical behaviour for correlation.
⚠️ On MetaQuotes-Demo, `USDX` is a Nasdaq ETF, not the dollar index — keep it
out of `DXY_CANDIDATES`.

## Safety notes
- Auto-trade starts **OFF** every run. Turn it on from the dashboard.
- Test on a **demo** account first.
- The dashboard has a **Close all** kill switch for engine positions.
- Force-killing the process (Task Manager / power loss) skips the shutdown
  notification; open positions always keep their SL/TP on the MT5 server.

## Files
```
config.py              all settings (connection, scoring, risk, hours)
correlation_engine.py  data + synthetic DXY + indicators
signal_engine.py       scoring model + session/entry/market-closed windows
trader.py              execution, risk gates, stats + state persistence
daily_logger.py        JSONL event log + daily report builder
app.py                 Flask server + engine loop (phases, confirmation)
templates/dashboard.html
bot_state.json         runtime state (auto-created, gitignored)
logs/                  daily .jsonl logs + report_<day>.json (gitignored)
```
