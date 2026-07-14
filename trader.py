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
        # active-trade management state (TRADE_MANAGEMENT.md):
        # {"ticket","dir","entry","risk","tp1","tp2","tp1_done"}
        self.active: dict | None = None
        self._last_trail_log = 0.0
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
            "active_trade": self.active,
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
        self.active = d.get("active_trade")
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
        # PKT wall-clock for display — the Windows clock may be in another timezone
        entry = {"time": dlog.now_pkt().strftime("%H:%M:%S"), "msg": msg, "level": level}
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
            # NOTE: position= must be the ONLY selector — combining it with a date
            # range makes the MT5 API ignore it and return ALL account deals,
            # which mis-reported every close as the 2-day account total.
            deals = mt5.history_deals_get(position=ticket) or []
            out_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            pnl = sum(d.profit for d in out_deals)
            exit_px = out_deals[-1].price if out_deals else 0.0
            self.daily_pnl = round(self.daily_pnl + pnl, 2)

            # exit classification (TRADE_MANAGEMENT.md #4) — by total P/L and
            # the broker's real exit price, never assumed
            a = self.active if (self.active and self.active.get("ticket") == ticket) else None
            if pnl < 0:
                how = "sl_hit"
            elif a and a.get("tp2") and abs(exit_px - a["tp2"]) <= 0.5:
                how = "tp2_full"
            elif a and a.get("tp1_done"):
                how = "dynamic_tp2"
            else:
                how = "profit_close"
            dlog.log_event("close", ticket=ticket, pnl=round(pnl, 2),
                           exit=round(exit_px, 2),
                           result="loss" if pnl < 0 else "win", how=how)
            if a:
                self.active = None

            if pnl < 0:
                self.losses += 1
                self.consecutive_losses += 1
                self.cooldown_until = datetime.now() + timedelta(minutes=cfg.COOLDOWN_AFTER_LOSS_MIN)
                self._log(f"🔻 SL hit @ {exit_px:.2f} — position {ticket} closed at LOSS {pnl:.2f}. "
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
                if how == "tp2_full":
                    msg = f"🏆 TP2 FULL @ {exit_px:.2f} — position {ticket} closed at PROFIT +{pnl:.2f}"
                elif how == "dynamic_tp2":
                    msg = (f"🎯 Dynamic TP2 @ {exit_px:.2f} (trailing lock) — "
                           f"position {ticket} closed at PROFIT +{pnl:.2f}")
                else:
                    msg = f"🟢 Position {ticket} closed at PROFIT +{pnl:.2f} @ {exit_px:.2f}"
                self._log(msg, "good", discord=True)
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

    # ---------- trade management (TRADE_MANAGEMENT.md) ----------
    def _modify_sl(self, ticket: int, sl: float, tp: float, retries: int = 1) -> bool:
        sym = mt5.symbol_info(cfg.GOLD_SYMBOL)
        digits = sym.digits if sym else 2
        request = {"action": mt5.TRADE_ACTION_SLTP, "symbol": cfg.GOLD_SYMBOL,
                   "position": ticket, "sl": round(sl, digits),
                   "tp": round(tp, digits) if tp else 0.0}
        for attempt in range(retries):
            res = mt5.order_send(request)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            if attempt < retries - 1:
                time.sleep(cfg.SL_MODIFY_RETRY_WAIT)
        return False

    def manage_position(self, snap: dict):
        """TP1 -> breakeven+cushion; then ATR trailing; watchdog if SL ignored."""
        positions = self.open_positions()
        if not positions:
            if self.active is not None:
                self.active = None
                self._save_state()
            return
        p = positions[0]

        # adopt/reconstruct after restart or manual trade with our magic
        if not self.active or self.active.get("ticket") != p["ticket"]:
            d = 1 if p["type"] == "BUY" else -1
            risk = abs(p["tp"] - p["open_price"]) / cfg.TP2_RR if p["tp"] \
                else cfg.SL_MIN_ATR * snap["atr"]
            self.active = {"ticket": p["ticket"], "dir": p["type"], "entry": p["open_price"],
                           "risk": risk, "tp1": p["open_price"] + d * risk * cfg.TP1_RR,
                           "tp2": p["tp"], "tp1_done": False}
            self._save_state()
            self._log(f"Managing position #{p['ticket']} — TP1 {self.active['tp1']:.2f}, "
                      f"TP2 {p['tp']:.2f}")

        a = self.active
        d = 1 if a["dir"] == "BUY" else -1
        tick = mt5.symbol_info_tick(cfg.GOLD_SYMBOL)
        if not tick:
            return
        price = tick.bid if a["dir"] == "BUY" else tick.ask

        # watchdog: price is beyond SL but the broker hasn't closed the position
        if p["sl"] and d * (price - p["sl"]) < -0.10:
            self._log(f"⛔ WATCHDOG — price {price:.2f} beyond SL {p['sl']:.2f} and position "
                      f"still open; force-closing at market", "error", discord=True)
            self.close_all(reason="watchdog — SL not honored")
            self.active = None
            self._save_state()
            return

        if not a["tp1_done"]:
            # TP1 touched -> SL to breakeven + dynamic ATR cushion (full lot stays on)
            if d * (price - a["tp1"]) >= 0:
                cushion = round(cfg.BE_CUSHION_ATR * snap["atr"], 2)
                new_sl = a["entry"] + d * cushion
                if self._modify_sl(p["ticket"], new_sl, p["tp"],
                                   retries=cfg.SL_MODIFY_RETRIES):
                    a["tp1_done"] = True
                    a["cushion"] = cushion
                    self._save_state()
                    self._log(f"🎯 TP1 {a['tp1']:.2f} (1:1) reached — SL moved to breakeven"
                              f"{'+' if d > 0 else '-'}${cushion:.2f} ({new_sl:.2f}). "
                              f"Position is now RISK-FREE, trailing towards TP2 {a['tp2']:.2f}",
                              "good", discord=True)
                else:
                    self._log(f"⛔ SL move after TP1 FAILED {cfg.SL_MODIFY_RETRIES}x — "
                              f"safety-closing full lot", "error", discord=True)
                    self.close_all(reason="TP1 SL-move failed — safety close")
                    self.active = None
                    self._save_state()
        else:
            # trailing: gap = TRAIL_ATR_MULT x current ATR, forward only,
            # never behind the breakeven cushion
            desired = price - d * cfg.TRAIL_ATR_MULT * snap["atr"]
            floor_sl = a["entry"] + d * a.get("cushion", cfg.BE_CUSHION_ATR * snap["atr"])
            if d * (desired - floor_sl) < 0:
                desired = floor_sl
            cur_sl = p["sl"] or floor_sl
            if d * (desired - cur_sl) >= cfg.TRAIL_MIN_STEP:
                if self._modify_sl(p["ticket"], desired, p["tp"]):
                    if abs(desired - self._last_trail_log) >= 1.0:
                        self._log(f"Trailing SL → {desired:.2f} "
                                  f"(gap {cfg.TRAIL_ATR_MULT}x ATR = {cfg.TRAIL_ATR_MULT * snap['atr']:.2f})")
                        self._last_trail_log = desired

    # ---------- entry quality gates (STRATEGY_FILTERS.md) ----------
    def _quality_gates(self, direction: str, snap: dict) -> tuple[bool, str]:
        atr = snap["atr"]
        if atr <= 0:
            return True, "OK"
        # spike filter — current/last candle abnormally large vs ATR
        if cfg.SPIKE_BAR_ATR_RATIO > 0:
            ratio = snap.get("bar_range", 0.0) / atr
            if ratio > cfg.SPIKE_BAR_ATR_RATIO:
                return False, (f"Spike candle (range {ratio:.1f}x ATR) — "
                               f"waiting for market to settle")
        # overextension filter — don't buy tops / sell bottoms
        if cfg.MAX_EXTENSION_ATR > 0:
            price = snap["gold_ask"] if direction == "BUY" else snap["gold_price"]
            ema = snap["gold_ema_slow"]
            ext = (price - ema) if direction == "BUY" else (ema - price)
            if ext > cfg.MAX_EXTENSION_ATR * atr:
                return False, (f"Overextended ({ext / atr:.1f}x ATR from EMA21) — "
                               f"waiting for pullback")
        return True, "OK"

    # ---------- execution ----------
    def execute(self, direction: str, snap: dict, score: float) -> bool:
        atr = snap["atr"]
        ok, reason = self.can_trade()
        if ok:
            ok, reason = self._quality_gates(direction, snap)
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

        # SL beyond the swing point + ATR buffer, never closer than SL_MIN_DOLLARS
        # (TRADE_MANAGEMENT.md #1); order TP is TP2, TP1 is a management level
        if direction == "BUY":
            price = tick.ask
            swing_sl = snap["swing_low"] - cfg.SL_ATR_BUFFER * atr
            sl_dist = max(price - swing_sl, cfg.SL_MIN_ATR * atr)
            sl, tp = price - sl_dist, price + sl_dist * cfg.TP2_RR
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            swing_sl = snap["swing_high"] + cfg.SL_ATR_BUFFER * atr
            sl_dist = max(swing_sl - price, cfg.SL_MIN_ATR * atr)
            sl, tp = price + sl_dist, price - sl_dist * cfg.TP2_RR
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
        d = 1 if direction == "BUY" else -1
        tp1 = price + d * sl_dist * cfg.TP1_RR
        self.active = {"ticket": result.order, "dir": direction, "entry": price,
                       "risk": sl_dist, "tp1": tp1, "tp2": round(tp, sym.digits),
                       "tp1_done": False}
        self._save_state()
        dlog.log_event("entry", ticket=result.order, direction=direction, lots=lots,
                       price=round(price, 2), sl=round(sl, 2), tp1=round(tp1, 2),
                       tp2=round(tp, 2), score=score, lot_mode=self.lot_mode)
        self._log(f"{'🟩' if direction == 'BUY' else '🟥'} ENTRY {direction} {lots} lots @ {price:.2f} "
                  f"| SL {sl:.2f} | TP1 {tp1:.2f} | TP2 {tp:.2f} | score {score} "
                  f"| lot mode {self.lot_mode}", "good", discord=True)
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
                if self.active and self.active.get("ticket") == p["ticket"]:
                    self.active = None
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
