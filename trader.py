"""
trader.py — MT5 execution layer with risk management:
  - lot sizing from equity risk % (AUTO) or fixed user lot (MANUAL)
  - ATR-based SL, fixed-RR TP
  - one position at a time
  - daily loss limit / profit target -> analysis only, no new entries
  - cooldown after loss; HALT after N consecutive losses (resume from dashboard)
"""
import json
import os
import time
from datetime import datetime, date, timedelta

import MetaTrader5 as mt5
import requests

import config as cfg
import daily_logger as dlog
from signal_engine import in_entry_window

# daily stats + settings survive bot restarts in this file
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


class Trader:
    def __init__(self):
        self.trades_today = 0
        self.day = dlog.trading_day()   # trading day rolls at 03:00 PKT, not midnight
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.log: list[dict] = []
        self._known_position_tickets: set[int] = set()
        self._last_block: tuple | None = None  # dedupe repeated blocked-signal logs
        # daily P/L limits (runtime-adjustable from dashboard)
        self.daily_pnl = 0.0
        self.daily_loss_limit = float(cfg.DAILY_LOSS_LIMIT)
        self.daily_profit_target = float(cfg.DAILY_PROFIT_TARGET)
        self.daily_limit_hit: str | None = None   # None | "loss" | "profit"
        # halt state — set after MAX_CONSECUTIVE_LOSSES, cleared only via resume()
        self.halted = False
        # lot sizing mode (runtime-adjustable from dashboard)
        self.lot_mode = cfg.LOT_MODE
        self.manual_lot = float(cfg.MANUAL_LOT)
        # daily win/loss stats
        self.wins = 0
        self.losses = 0
        self._load_state()   # restore stats/settings/halt from previous run

    # ---------- persistence ----------
    def _save_state(self):
        data = {
            "day": self.day.isoformat(),
            "trades_today": self.trades_today,
            "wins": self.wins,
            "losses": self.losses,
            "daily_pnl": self.daily_pnl,
            "daily_limit_hit": self.daily_limit_hit,
            "consecutive_losses": self.consecutive_losses,
            "halted": self.halted,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_profit_target": self.daily_profit_target,
            "lot_mode": self.lot_mode,
            "manual_lot": self.manual_lot,
            "known_tickets": sorted(self._known_position_tickets),
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
        except Exception:
            return
        # settings + halt survive any restart, whatever the day
        self.halted = bool(d.get("halted", False))
        self.daily_loss_limit = float(d.get("daily_loss_limit", self.daily_loss_limit))
        self.daily_profit_target = float(d.get("daily_profit_target", self.daily_profit_target))
        if d.get("lot_mode") in ("AUTO", "MANUAL"):
            self.lot_mode = d["lot_mode"]
        self.manual_lot = float(d.get("manual_lot", self.manual_lot))
        # remembered tickets let us detect positions that closed while the bot was off
        self._known_position_tickets = set(d.get("known_tickets", []))
        if d.get("day") == dlog.trading_day().isoformat():
            self.trades_today = int(d.get("trades_today", 0))
            self.wins = int(d.get("wins", 0))
            self.losses = int(d.get("losses", 0))
            self.daily_pnl = float(d.get("daily_pnl", 0.0))
            self.daily_limit_hit = d.get("daily_limit_hit")
            self.consecutive_losses = int(d.get("consecutive_losses", 0))
            cu = d.get("cooldown_until")
            if cu:
                try:
                    self.cooldown_until = datetime.fromisoformat(cu)
                except ValueError:
                    pass
        elif self.halted:
            self.consecutive_losses = int(d.get("consecutive_losses", 0))

    # ---------- helpers ----------
    def _roll_day(self):
        if dlog.trading_day() != self.day:
            self.day = dlog.trading_day()
            self.trades_today = 0
            self.wins = 0
            self.losses = 0
            self.daily_pnl = 0.0
            self.daily_limit_hit = None
            self.cooldown_until = None
            if not self.halted:          # halt survives the day roll — only dashboard resumes it
                self.consecutive_losses = 0
            self._save_state()

    def _log(self, msg: str, level: str = "info", discord: bool = False):
        """Local engine log always; Discord only for events explicitly marked
        (entries, SL/TP closes, daily stops) — never the per-loop noise."""
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
        self.log.append(entry)
        self.log = self.log[-200:]
        dlog.log_event("log", level=level, msg=msg)
        if discord and cfg.DISCORD_WEBHOOK_URL:
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
        """Detect our positions that closed since the last poll; update daily P/L,
        loss streak, and daily-limit / halt states."""
        self._roll_day()
        current = {p["ticket"] for p in self.open_positions()}
        closed = self._known_position_tickets - current
        for ticket in closed:
            deals = mt5.history_deals_get(datetime.now() - timedelta(days=2), datetime.now(),
                                          position=ticket) or []
            pnl = sum(d.profit for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)
            self.daily_pnl = round(self.daily_pnl + pnl, 2)
            dlog.log_event("close", ticket=ticket, pnl=round(pnl, 2),
                           result="loss" if pnl < 0 else "win",
                           how="sl_hit" if pnl < 0 else "tp_hit")
            if pnl < 0:
                self.losses += 1
                self.consecutive_losses += 1
                self.cooldown_until = datetime.now() + timedelta(minutes=cfg.COOLDOWN_AFTER_LOSS_MIN)
                self._log(f"🔴 SL hit — position {ticket} closed at LOSS {pnl:.2f}. "
                          f"Cooldown {cfg.COOLDOWN_AFTER_LOSS_MIN}m. "
                          f"Streak {self.consecutive_losses}/{cfg.MAX_CONSECUTIVE_LOSSES}",
                          "warn", discord=True)
                if self.consecutive_losses >= cfg.MAX_CONSECUTIVE_LOSSES and not self.halted:
                    self.halted = True
                    self._log(f"⛔ BOT HALTED — {cfg.MAX_CONSECUTIVE_LOSSES} consecutive losses. "
                              f"No trades until RESUME is pressed on the dashboard.",
                              "error", discord=True)
            else:
                self.wins += 1
                self.consecutive_losses = 0
                self._log(f"🟢 TP hit — position {ticket} closed at PROFIT +{pnl:.2f}",
                          "good", discord=True)
            self._check_daily_limits()
        self._known_position_tickets = current
        if closed:
            self._save_state()

    def _check_daily_limits(self):
        if self.daily_limit_hit:
            return
        if self.daily_loss_limit > 0 and self.daily_pnl <= -self.daily_loss_limit:
            self.daily_limit_hit = "loss"
            self._log(f"🛑 DAILY LOSS LIMIT hit ({self.daily_pnl:.2f} / -{self.daily_loss_limit:.0f}). "
                      f"Analysis continues, no new entries today.", "error", discord=True)
        elif self.daily_profit_target > 0 and self.daily_pnl >= self.daily_profit_target:
            self.daily_limit_hit = "profit"
            self._log(f"🎯 DAILY PROFIT TARGET hit (+{self.daily_pnl:.2f} / {self.daily_profit_target:.0f}). "
                      f"Analysis continues, no new entries today.", "good", discord=True)

    def resume(self):
        """Dashboard button — clears the consecutive-loss halt."""
        self.halted = False
        self.consecutive_losses = 0
        self.cooldown_until = None
        self._last_block = None
        self._save_state()
        self._log("▶️ Trading RESUMED from dashboard — loss streak reset.", "good", discord=True)

    # ---------- gates ----------
    def can_trade(self) -> tuple[bool, str]:
        self._roll_day()
        if self.halted:
            return False, f"HALTED ({cfg.MAX_CONSECUTIVE_LOSSES} losses) — press RESUME on dashboard"
        if not in_entry_window():
            return False, (f"Outside entry hours ({cfg.TRADING_DAY_START}–{cfg.ENTRY_CUTOFF} PKT) "
                           f"— analysis & signals only")
        if self.daily_limit_hit == "loss":
            return False, "Daily loss limit hit — analysis only until tomorrow"
        if self.daily_limit_hit == "profit":
            return False, "Daily profit target hit — analysis only until tomorrow"
        if len(self.open_positions()) >= cfg.MAX_OPEN_POSITIONS:
            return False, "Max open positions reached"
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            mins = int((self.cooldown_until - datetime.now()).total_seconds() // 60) + 1
            return False, f"Loss cooldown — {mins}m remaining"
        return True, "OK"

    # ---------- sizing ----------
    def _lot_size(self, sl_distance: float) -> float:
        sym = mt5.symbol_info(cfg.GOLD_SYMBOL)
        step = (sym.volume_step if sym else 0) or 0.01
        if self.lot_mode == "MANUAL":
            lots = max(cfg.MIN_LOT, min(cfg.MAX_LOT, round(self.manual_lot / step) * step))
            return round(lots, 2)
        info = mt5.account_info()
        if not info or not sym or sl_distance <= 0:
            return cfg.MIN_LOT
        risk_money = info.equity * (cfg.RISK_PERCENT / 100.0)
        # value of 1.0 lot per 1 point of price movement
        tick_value = sym.trade_tick_value / sym.trade_tick_size if sym.trade_tick_size else 0
        if tick_value <= 0:
            return cfg.MIN_LOT
        lots = risk_money / (sl_distance * tick_value)
        lots = max(cfg.MIN_LOT, min(cfg.MAX_LOT, round(lots / step) * step))
        return round(lots, 2)

    # ---------- execution ----------
    def execute(self, direction: str, atr: float, score: float) -> bool:
        ok, reason = self.can_trade()
        if not ok:
            # log locally only once per (direction, reason) — no Discord, no repeat spam
            if self._last_block != (direction, reason):
                self._log(f"Signal {direction} (score {score}) blocked: {reason}", "warn")
                self._last_block = (direction, reason)
            return False
        self._last_block = None

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
        self._save_state()
        dlog.log_event("entry", ticket=result.order, direction=direction, lots=lots,
                       price=round(price, 2), sl=round(sl, 2), tp=round(tp, 2),
                       score=score, lot_mode=self.lot_mode)
        self._log(f"{'🟩' if direction == 'BUY' else '🟥'} ENTRY {direction} {lots} lots @ {price:.2f} "
                  f"| SL {sl:.2f} | TP {tp:.2f} | score {score} | lot mode {self.lot_mode}",
                  "good", discord=True)
        return True

    def close_all(self, reason: str = "manual close"):
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
                "comment": f"{cfg.TRADE_COMMENT}|close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = mt5.order_send(request)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                # count P/L now and forget the ticket so check_closed_trades
                # doesn't re-report this close as an SL/TP hit
                self._known_position_tickets.discard(p["ticket"])
                self.daily_pnl = round(self.daily_pnl + p["profit"], 2)
                if p["profit"] < 0:
                    self.losses += 1
                else:
                    self.wins += 1
                self._check_daily_limits()
                self._save_state()
                dlog.log_event("close", ticket=p["ticket"], pnl=p["profit"],
                               result="loss" if p["profit"] < 0 else "win",
                               how="early_close", reason=reason)
                self._log(f"⚠️ Closed {p['type']} #{p['ticket']} at P/L {p['profit']} — {reason}",
                          "info", discord=True)
            else:
                self._log(f"Close failed for #{p['ticket']} ({reason})", "error")
