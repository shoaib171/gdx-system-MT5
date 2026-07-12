# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

GDX-CORR — a Gold/Dollar inverse-correlation trading bot for MetaTrader 5, with a Flask
dashboard and optional auto-trading. All code lives in `mt5-correlation-system/`.
It only runs on Windows with an MT5 terminal installed (the `MetaTrader5` package
attaches to a local terminal).

## Commands

```powershell
cd mt5-correlation-system
pip install -r requirements.txt
python app.py          # starts engine loop + dashboard at http://localhost:5077
```

There are no tests or linters. To sanity-check changes, run `app.py` and poll
`http://localhost:5077/api/state` — `connected`, `error`, `snapshot`, and `signal`
tell you whether the MT5 connection, data pipeline, and scoring all work.

## Architecture

Single-process pipeline, one module per stage, all configured from `config.py`
(engines import it as `cfg` — never hardcode parameters elsewhere):

1. **`correlation_engine.py`** (`CorrelationEngine`) — connects to MT5, pulls XAUUSD +
   dollar-index bars, computes rolling correlation, correlation z-score, EMAs, ROC and
   ATR. Returns `{"df": DataFrame, "snapshot": dict}`; the flat `snapshot` dict is the
   contract consumed by every downstream stage.
   - **DXY source**: tries broker symbols in `DXY_CANDIDATES`; if none exist, builds a
     synthetic DXY from the 6 ICE component pairs in `DXY_COMPONENTS` using the official
     geometric-weight formula (`DXY_CONSTANT * Π pair^±weight`).
2. **`signal_engine.py`** (`evaluate`) — pure function: snapshot → scored signal
   (0–100). Five weighted components from `SCORE_WEIGHTS`: inverse-correlation regime
   (corr ≤ −0.60), DXY momentum direction, gold momentum agreement, decoupling z-score
   bonus, and London/NY session filter (PKT times). Direction is *contrarian to DXY*:
   DXY falling → BUY gold. `fire` at score ≥ 70, `auto_eligible` at ≥ 75.
3. **`trader.py`** (`Trader`) — MT5 order execution behind risk gates: max 1 open
   position, 4 trades/day, 45-min cooldown after a loss, hard stop after 2 consecutive
   losses. Lot size derived from equity risk % and ATR-based SL (TP = 2R). Orders retry
   once with FOK filling if IOC fails. Only touches positions with `MAGIC_NUMBER`
   (77201) so manual trades are never affected.
4. **`app.py`** — Flask server plus a daemon `engine_loop` thread that every
   `REFRESH_SECONDS` fetches → evaluates → (optionally) executes, and publishes into a
   module-level `STATE` dict guarded by `LOCK`. `templates/dashboard.html` polls
   `/api/state`. Auto-trade always starts OFF and is toggled via `/api/toggle_auto`;
   `/api/close_all` is the kill switch. `last_executed_bar` in STATE prevents duplicate
   entries on the same candle.

## Broker/symbol gotchas

- Symbol names are broker-specific: Exness Standard uses an `m` suffix
  (`XAUUSDm`, `EURUSDm`); MetaQuotes-Demo uses plain names (`XAUUSD`, `EURUSD`).
  `GOLD_SYMBOL` and the keys of `DXY_COMPONENTS` must both match the broker.
- On MetaQuotes-Demo, `USDX` is a Nasdaq ETF, **not** the dollar index — it must not be
  in `DXY_CANDIDATES` or the engine silently correlates gold against the wrong
  instrument. When in doubt, verify with `mt5.symbol_info(sym).description`.
- Auto-trading requires Algo Trading enabled in the MT5 terminal (toolbar button +
  Tools → Options → Expert Advisors).
