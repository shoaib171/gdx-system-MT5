"""
trader.py — MT5 execution layer with risk management:
  - lot sizing from equity risk %
  - ATR-based SL, fixed-RR TP
  - one position at a time, daily trade cap
  - cooldown after loss, hard stop after consecutive losses
"""
import time
from datetime import datetime, date, timedelta

import MetaTrader5 as mt5
import requests

import config as cfg


class Trader:
    def __init__(self):
        self.trades_today = 0
        self.day = date.today()
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.log: list[dict] = []
        self._known_position_tickets: set[int] = set()

    # ---------- helpers ----------
    def _roll_day(self):
        if date.today() != self.day:
            self.day = date.today()
            self.trades_today = 0
            self.consecutive_losses = 0
            self.cooldown_until = None

    def _log(self, msg: str, level: str = "info"):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
        self.log.append(entry)
        self.log = self.log[-200:]
        if cfg.DISCORD_WEBHOOK_URL:
            try:
                requests.post(cfg.DISCORD_WEBHOOK_URL,
                              json={"content": f"**GDX-CORR** | {msg}"}, timeout=5)
            except Exception:
                pass

    # ---------- state ----------
    def open_positions(self) -> list[dict]:
        positions = mt5.positions_get(symbol=cfg.GOLD_SYMBOL) or []
        out = []
        for p in positions:
            if p.magic != cfg.MAGIC_NUMBER:
                continue
            out.append({
                "ticket": p.ticket,
                "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume": p.volume,
                "open_price": p.price_open,
                "sl": p.sl, "tp": p.tp,
                "profit": round(p.profit, 2),
            })
        return out

    def account(self) -> dict:
        info = mt5.account_info()
        if not info:
            return {}
        return {
            "balance": round(info.balance, 2),
            "equity": round(info.equity, 2),
            "margin_free": round(info.margin_free, 2),
            "currency": info.currency,
        }

    def check_closed_trades(self):
        """Detect our positions that closed since the last poll and update loss streak."""
        current = {p["ticket"] for p in self.open_positions()}
        closed = self._known_position_tickets - current
        for ticket in closed:
            deals = mt5.history_deals_get(datetime.now() - timedelta(days=2), datetime.now(),
                                          position=ticket) or []
            pnl = sum(d.profit for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)
            if pnl < 0:
                self.consecutive_losses += 1
                self.cooldown_until = datetime.now() + timedelta(minutes=cfg.COOLDOWN_AFTER_LOSS_MIN)
                self._log(f"Position {ticket} closed at LOSS {pnl:.2f}. "
                          f"Cooldown {cfg.COOLDOWN_AFTER_LOSS_MIN}m. "
                          f"Streak {self.consecutive_losses}/{cfg.MAX_CONSECUTIVE_LOSSES}", "warn")
            else:
                self.consecutive_losses = 0
                self._log(f"Position {ticket} closed at PROFIT {pnl:.2f}", "good")
        self._known_position_tickets = current

    # ---------- gates ----------
    def can_trade(self) -> tuple[bool, str]:
        self._roll_day()
        if len(self.open_positions()) >= cfg.MAX_OPEN_POSITIONS:
            return False, "Max open positions reached"
        if self.trades_today >= cfg.MAX_TRADES_PER_DAY:
            return False, "Daily trade cap reached"
        if self.consecutive_losses >= cfg.MAX_CONSECUTIVE_LOSSES:
            return False, "Consecutive-loss hard stop (resumes tomorrow)"
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            mins = int((self.cooldown_until - datetime.now()).total_seconds() // 60) + 1
            return False, f"Loss cooldown — {mins}m remaining"
        return True, "OK"

    # ---------- sizing ----------
    def _lot_size(self, sl_distance: float) -> float:
        info = mt5.account_info()
        sym = mt5.symbol_info(cfg.GOLD_SYMBOL)
        if not info or not sym or sl_distance <= 0:
            return cfg.MIN_LOT
        risk_money = info.equity * (cfg.RISK_PERCENT / 100.0)
        # value of 1.0 lot per 1 point of price movement
        tick_value = sym.trade_tick_value / sym.trade_tick_size if sym.trade_tick_size else 0
        if tick_value <= 0:
            return cfg.MIN_LOT
        lots = risk_money / (sl_distance * tick_value)
        step = sym.volume_step or 0.01
        lots = max(cfg.MIN_LOT, min(cfg.MAX_LOT, round(lots / step) * step))
        return round(lots, 2)

    # ---------- execution ----------
    def execute(self, direction: str, atr: float, score: float) -> bool:
        ok, reason = self.can_trade()
        if not ok:
            self._log(f"Signal {direction} (score {score}) blocked: {reason}", "warn")
            return False

        tick = mt5.symbol_info_tick(cfg.GOLD_SYMBOL)
        sym = mt5.symbol_info(cfg.GOLD_SYMBOL)
        if not tick or not sym:
            self._log("No tick/symbol info — order skipped", "error")
            return False

        sl_dist = cfg.SL_ATR_MULT * atr
        tp_dist = sl_dist * cfg.TP_RR
        if direction == "BUY":
            price = tick.ask
            sl, tp = price - sl_dist, price + tp_dist
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            sl, tp = price + sl_dist, price - tp_dist
            order_type = mt5.ORDER_TYPE_SELL

        lots = self._lot_size(sl_dist)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": cfg.GOLD_SYMBOL,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": round(sl, sym.digits),
            "tp": round(tp, sym.digits),
            "deviation": cfg.DEVIATION_POINTS,
            "magic": cfg.MAGIC_NUMBER,
            "comment": f"{cfg.TRADE_COMMENT}|{score}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            rc = result.retcode if result else "None"
            # retry once with FOK filling (some Exness symbols need it)
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                self._log(f"Order FAILED retcode={rc}", "error")
                return False

        self.trades_today += 1
        self._known_position_tickets.add(result.order)
        self._log(f"{direction} {lots} lots @ {price:.2f} | SL {sl:.2f} TP {tp:.2f} | score {score}", "good")
        return True

    def close_all(self):
        for p in self.open_positions():
            tick = mt5.symbol_info_tick(cfg.GOLD_SYMBOL)
            if not tick:
                continue
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": cfg.GOLD_SYMBOL,
                "volume": p["volume"],
                "type": mt5.ORDER_TYPE_SELL if p["type"] == "BUY" else mt5.ORDER_TYPE_BUY,
                "position": p["ticket"],
                "price": tick.bid if p["type"] == "BUY" else tick.ask,
                "deviation": cfg.DEVIATION_POINTS,
                "magic": cfg.MAGIC_NUMBER,
                "comment": f"{cfg.TRADE_COMMENT}|manual_close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(request)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                self._log(f"Closed {p['type']} #{p['ticket']} (P/L {p['profit']})", "info")
            else:
                self._log(f"Close failed for #{p['ticket']}", "error")
