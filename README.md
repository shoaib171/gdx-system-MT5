# GDX-CORR — Gold/Dollar Correlation Trading System

XAUUSD ↔ DXY inverse-correlation engine with live dashboard, scored signals
and optional auto-trading on MetaTrader 5 (Exness).

## How the signal works
1. **Regime filter** — rolling 50-bar correlation between gold & DXY returns.
   Tradeable only when corr ≤ −0.60 (strong inverse regime).
2. **Direction** — trade gold AGAINST dollar momentum:
   DXY falling (EMA9 < EMA21 + negative ROC) → **BUY** gold, and vice versa.
3. **Confirmation** — gold's own EMA alignment must agree.
4. **Decoupling bonus** — correlation z-score break ≥ 1.5σ.
5. **Session filter** — London/NY overlap, 1:00–9:30 PM PKT.

Score out of 100. Signal fires at ≥70, auto-trade executes at ≥75.

## Risk management (built in)
- Lot size from equity risk % (default 0.5%)
- SL = 1.5 × ATR(14), TP = 2R
- 1 position max, 4 trades/day max
- 45-min cooldown after a loss, hard stop after 2 consecutive losses
- Once-per-bar execution guard (no whipsaw re-entry on same candle)
- Magic number 77201 — never touches your manual/STIS positions

## Setup (Contabo Windows VPS)
```
pip install -r requirements.txt
```
1. Open `config.py`:
   - Set `GOLD_SYMBOL` exactly as it appears in your Market Watch
     (`XAUUSDm` for Exness Standard, `XAUUSD` for Pro/Raw).
   - Login fields optional — with MT5 terminal already logged in, leave as-is.
   - Paste `DISCORD_WEBHOOK_URL` if you want alerts.
2. Make sure **Algo Trading is enabled** in the MT5 terminal (top toolbar button
   + Tools → Options → Expert Advisors → allow algorithmic trading).
3. Run:
```
python app.py
```
4. Open `http://localhost:5077` on the VPS, or `http://<VPS-IP>:5077`
   from your phone (open port 5077 in Windows Firewall).

## DXY source
The engine first looks for a broker dollar-index symbol (USDX/DXY etc.).
Exness doesn't offer one, so it automatically builds a **synthetic DXY**
from the 6 official ICE component pairs with correct weights — mathematically
identical behaviour for correlation purposes.

## Safety notes
- Auto-trade starts **OFF** every run. Turn it on from the dashboard (asks confirmation).
- Test on your Exness **demo** account first — just log the terminal into demo.
- The dashboard has a **Close all** kill switch for engine positions.

## Files
```
config.py              all settings
correlation_engine.py  data + synthetic DXY + indicators
signal_engine.py       scoring model
trader.py              execution + risk gates
app.py                 Flask server + engine loop
templates/dashboard.html
```
