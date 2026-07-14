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
   position, daily realized loss limit / profit target (after either is hit the engine
   keeps analyzing but takes no entries until the next day), 45-min cooldown after a
   loss, and a HALT after `MAX_CONSECUTIVE_LOSSES` (3) that survives day rollover and
   clears only via `/api/resume`. Lot sizing has two modes (dashboard-switchable):
   AUTO (equity risk % vs ATR-based SL, TP = 2R) or MANUAL (fixed lot). Orders retry
   once with FOK filling if IOC fails. Only touches positions with `MAGIC_NUMBER`
   (77201) so manual trades are never affected. `_log()` writes to the dashboard log;
   Discord receives only calls marked `discord=True` (entries with SL/TP, SL/TP hits,
   daily limits, halt/resume, bot start/stop) — never per-loop noise.
4. **`app.py`** — Flask server plus a daemon `engine_loop` thread that every
   `REFRESH_SECONDS` fetches → evaluates → (optionally) executes, and publishes into a
   module-level `STATE` dict guarded by `LOCK`. `templates/dashboard.html` polls
   `/api/state`. The loop also publishes a `phase` string (live scanner on the
   dashboard), logs signal changes locally with the reason (which component flipped),
   and runs a next-candle confirmation state machine: a firing signal must still fire
   on a NEW bar to be "confirmed" — only confirmations go to Discord, and a confirmed
   signal opposite to the open position (score ≥ `OPPOSITE_EXIT_SCORE`) closes it
   (`EXIT_ON_OPPOSITE`). Entries themselves are unchanged: immediate on
   `auto_eligible`, once per bar via `last_executed_bar`. Auto-trade always starts OFF
   (`/api/toggle_auto`); other APIs: `/api/close_all` (kill switch), `/api/resume`
   (clear halt), `/api/settings` (runtime daily limits + lot mode).

## Operating hours & diagnostics

- All times PKT (`SESSION_TZ`). The trading day rolls at `TRADING_DAY_START` (03:00),
  not midnight — `daily_logger.trading_day()` is the day key everywhere. Entries are
  allowed 03:00–`ENTRY_CUTOFF` (21:30); until `MARKET_CLOSED_START` (01:00) the engine
  analyzes and signals only; 01:00–03:00 it idles (gold/DXY closed).
- **`daily_logger.py`** appends structured events (one JSON line each: `bar`, `log`,
  `entry`, `close`, `signal_confirmed`, `error`) to `logs/<trading-day>.jsonl`.
  `build_report()` aggregates a day into `logs/report_<day>.json`; the engine loop
  builds it automatically at the 03:00 rollover and posts a Discord summary.
  `/api/report?day=YYYY-MM-DD` serves it on demand.
- `Trader` persists daily stats/settings/halt across restarts in `bot_state.json`
  (gitignored, like `logs/`).

## Broker/symbol gotchas

- Symbol names are broker-specific: Exness Standard uses an `m` suffix
  (`XAUUSDm`, `EURUSDm`); MetaQuotes-Demo uses plain names (`XAUUSD`, `EURUSD`).
  `GOLD_SYMBOL` and the keys of `DXY_COMPONENTS` must both match the broker.
- On MetaQuotes-Demo, `USDX` is a Nasdaq ETF, **not** the dollar index — it must not be
  in `DXY_CANDIDATES` or the engine silently correlates gold against the wrong
  instrument. When in doubt, verify with `mt5.symbol_info(sym).description`.
- Auto-trading requires Algo Trading enabled in the MT5 terminal (toolbar button +
  Tools → Options → Expert Advisors).
