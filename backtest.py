"""
backtest.py — replay the GDX-CORR strategy on historical M15 data from MT5.

    python backtest.py [--days 30] [--balance 10000] [--no-filters]

Runs the same rules as the live engine: scoring, next-candle confirmation,
entry quality filters (spike / overextension / regime stability), operating
hours (PKT), risk sizing, cooldown, consecutive-loss halt and daily limits.

Approximations vs live (bar-close simulation):
- signals are evaluated once per closed M15 candle (live loop is every 5s
  on the forming candle) — intra-bar whipsaws are approximated by bar high/low
- entries execute at the next bar's open, plus the recorded spread
- if SL and TP both fall inside one bar, SL is assumed hit first (conservative)
- the 3-loss halt auto-resumes next trading day (live needs dashboard RESUME)
"""
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

import config as cfg

POINT_VALUE = 100.0          # $ per 1.0 lot per $1 move (XAUUSD, 100 oz)
SERVER_TO_PKT = timedelta(hours=2)   # MetaQuotes server (UTC+3) -> PKT (UTC+5)
BAR = timedelta(minutes=15)


def _mins(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def fetch(symbol, bars):
    r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, bars)
    if r is None or len(r) == 0:
        raise SystemExit(f"no data for {symbol}: {mt5.last_error()}")
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df.set_index("time")


def build_dataset(bars):
    gold = fetch(cfg.GOLD_SYMBOL, bars)
    frames = {p: fetch(p, bars)["close"] for p in cfg.DXY_COMPONENTS}
    aligned = pd.DataFrame(frames).dropna()
    dxy = pd.Series(cfg.DXY_CONSTANT, index=aligned.index)
    for pair, (w, inv) in cfg.DXY_COMPONENTS.items():
        dxy = dxy * (aligned[pair] ** (-w if inv else w))

    df = pd.DataFrame({
        "gold": gold["close"], "open": gold["open"],
        "high": gold["high"], "low": gold["low"],
        "spread": gold["spread"], "dxy": dxy,
    }).dropna()

    rets = df[["gold", "dxy"]].pct_change()
    df["corr"] = rets["gold"].rolling(cfg.CORR_WINDOW).corr(rets["dxy"])
    m = df["corr"].rolling(cfg.CORR_Z_WINDOW, min_periods=cfg.CORR_WINDOW).mean()
    s = df["corr"].rolling(cfg.CORR_Z_WINDOW, min_periods=cfg.CORR_WINDOW).std()
    df["corr_z"] = (df["corr"] - m) / s.replace(0, np.nan)
    df["dxy_ef"] = df["dxy"].ewm(span=cfg.EMA_FAST).mean()
    df["dxy_es"] = df["dxy"].ewm(span=cfg.EMA_SLOW).mean()
    df["dxy_roc"] = df["dxy"].pct_change(cfg.ROC_PERIOD) * 100
    df["g_ef"] = df["gold"].ewm(span=cfg.EMA_FAST).mean()
    df["g_es"] = df["gold"].ewm(span=cfg.EMA_SLOW).mean()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["gold"].shift()).abs()
    lc = (df["low"] - df["gold"].shift()).abs()
    df["atr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(cfg.ATR_PERIOD).mean()
    df["range"] = hl
    # swing levels from CLOSED bars before each candle (TRADE_MANAGEMENT.md)
    df["swing_low"] = df["low"].rolling(cfg.SL_SWING_LOOKBACK).min().shift(1)
    df["swing_high"] = df["high"].rolling(cfg.SL_SWING_LOOKBACK).max().shift(1)
    return df


def score_bar(df, i, pkt_close, use_regime_stability):
    """Same scoring model as signal_engine.evaluate, on bar-close data."""
    w = cfg.SCORE_WEIGHTS
    row = df.iloc[i]

    n = max(1, int(cfg.REGIME_MIN_BARS)) if use_regime_stability else 1
    corr_tail = df["corr"].iloc[i - n + 1: i + 1]
    regime = bool((corr_tail <= cfg.CORR_REGIME_THRESHOLD).all())
    regime_corr = row["corr"]
    if regime:
        depth = min(1.0, (abs(regime_corr) - abs(cfg.CORR_REGIME_THRESHOLD)) /
                    (1.0 - abs(cfg.CORR_REGIME_THRESHOLD)) + 0.5)
        regime_pts = round(w["regime"] * max(0.0, min(1.0, depth)), 1)
    else:
        regime_pts = 0.0

    dxy_bear = row["dxy_ef"] < row["dxy_es"] and row["dxy_roc"] < -cfg.ROC_THRESHOLD
    dxy_bull = row["dxy_ef"] > row["dxy_es"] and row["dxy_roc"] > cfg.ROC_THRESHOLD
    if dxy_bear:
        direction, mom_pts = "BUY", float(w["dxy_momentum"])
    elif dxy_bull:
        direction, mom_pts = "SELL", float(w["dxy_momentum"])
    else:
        direction, mom_pts = None, 0.0

    gold_bull = row["g_ef"] > row["g_es"]
    gold_agrees = (direction == "BUY" and gold_bull) or (direction == "SELL" and not gold_bull)
    gold_pts = float(w["gold_momentum"]) if (direction and gold_agrees) else 0.0

    z = row["corr_z"]
    dec_pts = float(w["decoupling"]) if (direction and not np.isnan(z) and abs(z) >= 1.5) else 0.0

    mins = pkt_close.hour * 60 + pkt_close.minute
    session = _mins(cfg.SESSION_START) <= mins <= _mins(cfg.SESSION_END)
    sess_pts = float(w["session"]) if session else 0.0

    score = round(regime_pts + mom_pts + gold_pts + dec_pts + sess_pts, 1)
    fire = direction is not None and regime and score >= cfg.SIGNAL_THRESHOLD
    return {"direction": direction, "score": score, "fire": fire}


def run(df, sim_start, balance0, filters_on, label):
    balance = balance0
    peak = balance0
    max_dd = 0.0
    pos = None
    pending = None            # entry decided at bar close, executed next bar open
    prev_fire = None          # (direction) fired at previous bar close
    trades = []
    blocked = {"spike": 0, "overext": 0, "hours": 0, "cooldown": 0, "halt": 0, "limit": 0}
    cooldown_until = None
    streak = 0
    halted = False
    cur_day = None
    daily_pnl = 0.0
    limit_hit = False
    gross_win = gross_loss = 0.0

    def close_pos(exit_px, when, how):
        nonlocal balance, streak, cooldown_until, halted, daily_pnl, limit_hit, peak, max_dd
        nonlocal gross_win, gross_loss
        d = 1 if pos["dir"] == "BUY" else -1
        pnl = (exit_px - pos["entry"]) * d * POINT_VALUE * pos["lots"] - pos["spread_cost"]
        balance += pnl
        daily_pnl += pnl
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
        if pnl < 0:
            streak += 1
            cooldown_until = when + timedelta(minutes=cfg.COOLDOWN_AFTER_LOSS_MIN)
            if streak >= cfg.MAX_CONSECUTIVE_LOSSES:
                halted = True
            gross_loss += -pnl
        else:
            streak = 0
            gross_win += pnl
        if cfg.DAILY_LOSS_LIMIT > 0 and daily_pnl <= -cfg.DAILY_LOSS_LIMIT:
            limit_hit = True
        if cfg.DAILY_PROFIT_TARGET > 0 and daily_pnl >= cfg.DAILY_PROFIT_TARGET:
            limit_hit = True
        trades.append({**pos, "exit": exit_px, "exit_time": when, "how": how, "pnl": round(pnl, 2)})

    idx = df.index
    start_i = idx.searchsorted(sim_start)
    for i in range(start_i, len(df)):
        row = df.iloc[i]
        t_open = idx[i]
        pkt_open = t_open + SERVER_TO_PKT
        pkt_close = pkt_open + BAR

        tday = (pkt_open - timedelta(hours=3)).date()
        if tday != cur_day:
            cur_day = tday
            daily_pnl = 0.0
            limit_hit = False
            streak = 0
            halted = False        # backtest assumption: resume each morning

        # --- execute pending entry at this bar's open ---
        if pending and pos is None:
            mins = pkt_open.hour * 60 + pkt_open.minute
            if not (_mins(cfg.TRADING_DAY_START) <= mins <= _mins(cfg.ENTRY_CUTOFF)):
                blocked["hours"] += 1
            else:
                spread_d = row["spread"] * 0.01
                d = 1 if pending["dir"] == "BUY" else -1
                entry = row["open"] + (spread_d if d > 0 else 0.0)
                # SL beyond swing +/- ATR buffer, min $5 (TRADE_MANAGEMENT.md)
                swing = (pending["swing_low"] - cfg.SL_ATR_BUFFER * pending["atr"]) if d > 0 \
                    else (pending["swing_high"] + cfg.SL_ATR_BUFFER * pending["atr"])
                sl_dist = max(d * (entry - swing), cfg.SL_MIN_DOLLARS)
                raw = (balance * cfg.RISK_PERCENT / 100.0) / (sl_dist * POINT_VALUE)
                lots = max(cfg.MIN_LOT, min(cfg.MAX_LOT, np.floor(raw / 0.01) * 0.01))
                pos = {"dir": pending["dir"], "entry": entry, "lots": round(lots, 2),
                       "sl": entry - d * sl_dist,
                       "tp1": entry + d * sl_dist * cfg.TP1_RR,
                       "tp2": entry + d * sl_dist * cfg.TP2_RR,
                       "tp1_done": False,
                       "entry_time": t_open, "score": pending["score"],
                       "spread_cost": spread_d * POINT_VALUE * lots}
        pending = None

        # --- manage open position within this bar (TRADE_MANAGEMENT.md) ---
        if pos is not None and idx[i] >= pos["entry_time"]:
            d = 1 if pos["dir"] == "BUY" else -1
            favor = row["high"] if d > 0 else row["low"]
            adverse = row["low"] if d > 0 else row["high"]
            if not pos["tp1_done"]:
                if d * (adverse - pos["sl"]) <= 0:
                    close_pos(pos["sl"], t_open, "sl"); pos = None
                elif d * (favor - pos["tp1"]) >= 0:
                    # TP1 touched: SL -> breakeven + cushion, full lot stays on
                    pos["tp1_done"] = True
                    pos["sl"] = pos["entry"] + d * cfg.BE_CUSHION
                    if d * (favor - pos["tp2"]) >= 0:
                        close_pos(pos["tp2"], t_open, "tp2"); pos = None
                    else:
                        trail = row["gold"] - d * cfg.TRAIL_ATR_MULT * row["atr"]
                        if d * (trail - pos["sl"]) > 0:
                            pos["sl"] = trail
            else:
                if d * (adverse - pos["sl"]) <= 0:
                    close_pos(pos["sl"], t_open, "trail"); pos = None
                elif d * (favor - pos["tp2"]) >= 0:
                    close_pos(pos["tp2"], t_open, "tp2"); pos = None
                else:
                    trail = row["gold"] - d * cfg.TRAIL_ATR_MULT * row["atr"]
                    if d * (trail - pos["sl"]) > 0:
                        pos["sl"] = trail

        # --- signal at bar close ---
        sig = score_bar(df, i, pkt_close, use_regime_stability=filters_on)
        fire = sig["direction"] if sig["fire"] else None
        confirmed = fire is not None and prev_fire == fire if filters_on else fire is not None
        prev_fire = fire

        # opposite-signal exit (confirmed opposite, score >= threshold)
        if (pos is not None and cfg.EXIT_ON_OPPOSITE and confirmed and fire
                and fire != pos["dir"] and sig["score"] >= cfg.OPPOSITE_EXIT_SCORE):
            close_pos(row["gold"], t_open, "opposite"); pos = None

        # --- entry decision ---
        if pos is None and confirmed and fire and sig["score"] >= cfg.AUTO_TRADE_THRESHOLD:
            if halted:
                blocked["halt"] += 1
            elif limit_hit:
                blocked["limit"] += 1
            elif cooldown_until is not None and t_open + BAR < cooldown_until:
                blocked["cooldown"] += 1
            elif filters_on and cfg.SPIKE_BAR_ATR_RATIO > 0 and row["atr"] > 0 and \
                    max(row["range"], df["range"].iloc[i - 1]) > cfg.SPIKE_BAR_ATR_RATIO * row["atr"]:
                blocked["spike"] += 1
            elif filters_on and cfg.MAX_EXTENSION_ATR > 0 and row["atr"] > 0 and \
                    ((row["gold"] - row["g_es"]) if fire == "BUY" else (row["g_es"] - row["gold"])) \
                    > cfg.MAX_EXTENSION_ATR * row["atr"]:
                blocked["overext"] += 1
            else:
                pending = {"dir": fire, "score": sig["score"], "atr": row["atr"],
                           "swing_low": row["swing_low"], "swing_high": row["swing_high"]}

    if pos is not None:
        close_pos(df["gold"].iloc[-1], idx[-1], "eod")

    wins = [t for t in trades if t["pnl"] >= 0]
    losses = [t for t in trades if t["pnl"] < 0]
    print(f"\n{'=' * 62}\n  {label}\n{'=' * 62}")
    print(f"  start balance   : ${balance0:,.2f}")
    print(f"  end balance     : ${balance:,.2f}   ({(balance / balance0 - 1) * 100:+.2f}%)")
    print(f"  net P/L         : ${balance - balance0:+,.2f}")
    print(f"  trades          : {len(trades)}  (W {len(wins)} / L {len(losses)}"
          f"  ->  {100 * len(wins) / len(trades):.0f}% win rate)" if trades else "  trades          : 0")
    if trades:
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        print(f"  profit factor   : {pf:.2f}")
        print(f"  max drawdown    : ${max_dd:,.2f} ({100 * max_dd / balance0:.1f}%)")
        by_how = {}
        for t in trades:
            by_how[t["how"]] = by_how.get(t["how"], 0) + 1
        print(f"  exits           : {by_how}")
    print(f"  blocked entries : {blocked}")
    if trades:
        print(f"\n  {'entry time (PKT)':19} {'dir':4} {'lots':5} {'entry':9} {'exit':9} {'how':8} {'pnl':>10}")
        for t in trades:
            et = (t['entry_time'] + SERVER_TO_PKT).strftime('%m-%d %H:%M')
            print(f"  {et:19} {t['dir']:4} {t['lots']:<5} {t['entry']:<9.2f} "
                  f"{t['exit']:<9.2f} {t['how']:8} {t['pnl']:>10.2f}")
    return balance


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--balance", type=float, default=10_000)
    ap.add_argument("--bars", type=int, default=4000)
    args = ap.parse_args()

    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
    df = build_dataset(args.bars)
    mt5.shutdown()

    sim_start = df.index[-1] - timedelta(days=args.days)
    warmup_ok = df.index.searchsorted(sim_start) > cfg.CORR_Z_WINDOW
    print(f"data: {df.index[0]} .. {df.index[-1]} (server time), {len(df)} bars, "
          f"simulating last {args.days} days, warmup ok: {warmup_ok}")
    print(f"daily limits: loss ${cfg.DAILY_LOSS_LIMIT:.0f} / target ${cfg.DAILY_PROFIT_TARGET:.0f}"
          f" | risk {cfg.RISK_PERCENT}% | SL swing({cfg.SL_SWING_LOOKBACK})+{cfg.SL_ATR_BUFFER}xATR"
          f" min ${cfg.SL_MIN_DOLLARS:.0f} | TP1 {cfg.TP1_RR}R->BE+${cfg.BE_CUSHION:.0f}"
          f" | TP2 {cfg.TP2_RR}R | trail {cfg.TRAIL_ATR_MULT}xATR")

    run(df, sim_start, args.balance, filters_on=False,
        label="OLD SYSTEM — no confirmation, no quality filters")
    run(df, sim_start, args.balance, filters_on=True,
        label="NEW SYSTEM — confirmation + spike + overextension + regime stability")
