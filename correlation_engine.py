"""
correlation_engine.py — pulls XAUUSD + DXY data from MT5,
builds synthetic DXY if broker has no dollar index symbol,
computes rolling correlation, momentum indicators and ATR.
"""
import time
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

import config as cfg

TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
}


class CorrelationEngine:
    def __init__(self):
        self.dxy_symbol = None      # broker DXY symbol if found, else None (synthetic)
        self.synthetic = False
        self.last_error = None

    # ---------- connection ----------
    def connect(self) -> bool:
        kwargs = {}
        if cfg.MT5_TERMINAL_PATH:
            kwargs["path"] = cfg.MT5_TERMINAL_PATH
        if cfg.MT5_LOGIN:
            kwargs.update(login=cfg.MT5_LOGIN, password=cfg.MT5_PASSWORD, server=cfg.MT5_SERVER)
        if not mt5.initialize(**kwargs):
            self.last_error = f"MT5 init failed: {mt5.last_error()}"
            return False
        self._resolve_dxy()
        mt5.symbol_select(cfg.GOLD_SYMBOL, True)
        return True

    def _resolve_dxy(self):
        for sym in cfg.DXY_CANDIDATES:
            info = mt5.symbol_info(sym)
            if info is not None:
                mt5.symbol_select(sym, True)
                self.dxy_symbol = sym
                self.synthetic = False
                return
        # fall back to synthetic DXY from component pairs
        self.synthetic = True
        for pair in cfg.DXY_COMPONENTS:
            mt5.symbol_select(pair, True)

    @staticmethod
    def _structure_levels(gold: pd.DataFrame, atr: float) -> list:
        """S/R zones: fractal swing highs/lows over SR_LOOKBACK closed bars,
        clustered within SR_CLUSTER_ATR x ATR. Returns [[price, touches], ...]."""
        window = gold.iloc[-(cfg.SR_LOOKBACK + 1):-1]   # closed bars only
        highs = window["high"].values
        lows = window["low"].values
        pts = []
        for j in range(2, len(window) - 2):
            if highs[j] == highs[j - 2:j + 3].max():
                pts.append(float(highs[j]))
            if lows[j] == lows[j - 2:j + 3].min():
                pts.append(float(lows[j]))
        if not pts:
            return []
        pts.sort()
        tol = cfg.SR_CLUSTER_ATR * atr if atr > 0 else 1.0
        zones = []
        cluster = [pts[0]]
        for p in pts[1:]:
            if p - cluster[-1] <= tol:
                cluster.append(p)
            else:
                zones.append([round(sum(cluster) / len(cluster), 2), len(cluster)])
                cluster = [p]
        zones.append([round(sum(cluster) / len(cluster), 2), len(cluster)])
        return zones

    # ---------- data ----------
    def _rates(self, symbol: str, bars: int) -> pd.DataFrame | None:
        rates = mt5.copy_rates_from_pos(symbol, TF_MAP[cfg.TIMEFRAME], 0, bars)
        if rates is None or len(rates) == 0:
            self.last_error = f"No rates for {symbol}: {mt5.last_error()}"
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time")

    def _synthetic_dxy(self, bars: int) -> pd.DataFrame | None:
        frames = {}
        for pair, (weight, inverted) in cfg.DXY_COMPONENTS.items():
            df = self._rates(pair, bars)
            if df is None:
                return None
            frames[pair] = df["close"]
        aligned = pd.DataFrame(frames).dropna()
        dxy = pd.Series(cfg.DXY_CONSTANT, index=aligned.index)
        for pair, (weight, inverted) in cfg.DXY_COMPONENTS.items():
            exp = -weight if inverted else weight
            dxy = dxy * (aligned[pair] ** exp)
        return pd.DataFrame({"close": dxy})

    def fetch(self) -> dict | None:
        """Returns aligned dataframe + indicator snapshot, or None on failure."""
        gold = self._rates(cfg.GOLD_SYMBOL, cfg.BARS_LOOKBACK)
        if gold is None:
            return None
        if self.dxy_symbol:
            dxy = self._rates(self.dxy_symbol, cfg.BARS_LOOKBACK)
        else:
            dxy = self._synthetic_dxy(cfg.BARS_LOOKBACK)
        if dxy is None:
            return None

        df = pd.DataFrame({
            "gold": gold["close"],
            "dxy": dxy["close"],
        }).dropna()
        if len(df) < cfg.CORR_WINDOW + 10:
            self.last_error = "Not enough aligned bars"
            return None

        # returns + rolling correlation
        rets = df.pct_change().dropna()
        df["corr"] = rets["gold"].rolling(cfg.CORR_WINDOW).corr(rets["dxy"])

        # correlation z-score (decoupling detector)
        cw = min(cfg.CORR_Z_WINDOW, len(df) - 5)
        corr_mean = df["corr"].rolling(cw, min_periods=cfg.CORR_WINDOW).mean()
        corr_std = df["corr"].rolling(cw, min_periods=cfg.CORR_WINDOW).std()
        df["corr_z"] = (df["corr"] - corr_mean) / corr_std.replace(0, np.nan)

        # momentum — DXY
        df["dxy_ema_f"] = df["dxy"].ewm(span=cfg.EMA_FAST).mean()
        df["dxy_ema_s"] = df["dxy"].ewm(span=cfg.EMA_SLOW).mean()
        df["dxy_roc"] = df["dxy"].pct_change(cfg.ROC_PERIOD) * 100

        # momentum — gold
        df["gold_ema_f"] = df["gold"].ewm(span=cfg.EMA_FAST).mean()
        df["gold_ema_s"] = df["gold"].ewm(span=cfg.EMA_SLOW).mean()

        # ATR on gold (needs OHLC)
        hl = gold["high"] - gold["low"]
        hc = (gold["high"] - gold["close"].shift()).abs()
        lc = (gold["low"] - gold["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = tr.rolling(cfg.ATR_PERIOD).mean()
        df["atr"] = atr.reindex(df.index).ffill()

        df = df.dropna(subset=["corr"])
        last = df.iloc[-1]

        # ---- swing levels for SL placement (TRADE_MANAGEMENT.md) ----
        # last N CLOSED candles (forming candle excluded)
        lb = cfg.SL_SWING_LOOKBACK
        closed_gold = gold.iloc[-(lb + 1):-1] if len(gold) > lb else gold
        swing_low = float(closed_gold["low"].min())
        swing_high = float(closed_gold["high"].max())

        # ---- support/resistance zones for target planning ----
        # fractal swing points over SR_LOOKBACK closed bars, clustered into
        # zones; a zone price visited repeatedly is a real target level
        levels = self._structure_levels(gold, float(atr.iloc[-1]) if len(atr) else 0.0)

        tick = mt5.symbol_info_tick(cfg.GOLD_SYMBOL)
        snapshot = {
            "gold_price": float(tick.bid) if tick else float(last["gold"]),
            "gold_ask": float(tick.ask) if tick else float(last["gold"]),
            "dxy_value": float(last["dxy"]),
            "dxy_source": self.dxy_symbol or "SYNTHETIC (6-pair basket)",
            "correlation": float(last["corr"]),
            "swing_low": swing_low,
            "swing_high": swing_high,
            "levels": levels,
            "corr_z": float(last["corr_z"]) if not np.isnan(last["corr_z"]) else 0.0,
            "dxy_ema_fast": float(last["dxy_ema_f"]),
            "dxy_ema_slow": float(last["dxy_ema_s"]),
            "dxy_roc": float(last["dxy_roc"]),
            "gold_ema_fast": float(last["gold_ema_f"]),
            "gold_ema_slow": float(last["gold_ema_s"]),
            "atr": float(last["atr"]),
            "bar_time": str(df.index[-1]),
        }
        return {"df": df, "snapshot": snapshot}
