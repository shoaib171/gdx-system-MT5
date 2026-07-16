"""
app.py — GDX-CORR dashboard server.
Run on the Windows VPS where MT5 terminal is installed:

    pip install -r requirements.txt
    python app.py

Dashboard:  http://localhost:5077   (or http://<VPS-IP>:5077 from your phone)
"""
import threading
import time
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import config as cfg
from correlation_engine import CorrelationEngine
from daily_logger import build_report, log_event, trading_day
from signal_engine import evaluate, in_entry_window, in_session, market_closed
from trader import Trader

app = Flask(__name__)
engine = CorrelationEngine()
trader = Trader()

STATE = {
    "connected": False,
    "auto_trade": False,          # OFF by default — flip from dashboard
    "last_update": None,
    "snapshot": {},
    "signal": {},
    "series": {"time": [], "gold": [], "dxy": [], "corr": []},
    "positions": [],
    "account": {},
    "log": [],
    "error": None,
    "last_executed_bar": None,    # prevents duplicate entries on same bar
    "phase": "starting engine…",  # live scanner text for the dashboard
    "pending_signal": None,       # {"dir","score","bar"} awaiting next-candle confirmation
    "confirmed_signal": None,     # direction confirmed on a new candle
}
LOCK = threading.Lock()


def set_phase(text: str):
    with LOCK:
        STATE["phase"] = text


def _signal_flags(sig: dict) -> dict:
    """Stable booleans for change detection (breakdown keys embed live numbers)."""
    bk = sig.get("breakdown", {})
    get = lambda prefix: next((v for k, v in bk.items() if k.startswith(prefix)), 0)
    return {
        "dxy_dir": sig["raw_direction"],
        "regime": sig["regime_active"],
        "gold_agrees": get("Gold momentum") > 0,
        "decoupling": get("Decoupling") > 0,
        "session": sig["session_ok"],
    }


def _change_reasons(old: dict, new: dict, snap: dict) -> list[str]:
    r = []
    if old["dxy_dir"] != new["dxy_dir"]:
        r.append(f"DXY momentum {old['dxy_dir']} → {new['dxy_dir']}")
    if old["regime"] != new["regime"]:
        r.append(f"inverse regime {'ACTIVATED' if new['regime'] else 'dropped'} "
                 f"(corr {snap['correlation']:+.2f})")
    if old["gold_agrees"] != new["gold_agrees"]:
        r.append("gold momentum " + ("now agrees" if new["gold_agrees"] else "no longer agrees"))
    if old["decoupling"] != new["decoupling"]:
        r.append("decoupling " + ("detected" if new["decoupling"] else "faded") +
                 f" (z {snap['corr_z']:+.1f})")
    if old["session"] != new["session"]:
        r.append("session " + ("opened" if new["session"] else "closed"))
    return r


def engine_loop():
    prev_sig = None            # last signal dict (for change logging)
    pending = None             # {"dir","score","bar"} — awaiting next-candle confirmation
    confirmed_dir = None       # currently confirmed firing direction
    fired_this_bar = False     # did the confirmed signal fire at least once this candle?
    expiry_bar = None          # candle being watched for expiry
    last_tday = trading_day()  # for the 03:00 PKT daily rollover report
    last_logged_bar = None     # one structured "bar" event per candle

    while True:
        try:
            # ---- 03:00 PKT rollover: final report for the day that just ended ----
            tday = trading_day()
            if tday != last_tday:
                rep = build_report(last_tday)
                trader._log(f"📊 Daily report {last_tday}: {rep['entries']} trades, "
                            f"{rep['wins']}W/{rep['losses']}L, P/L {rep['total_pnl']:+.2f}, "
                            f"{rep['signals_confirmed']} confirmed signals, "
                            f"{rep['bars_analyzed']} bars analyzed — fresh day started",
                            "info", discord=True)
                last_tday = tday

            # ---- 01:00–03:00 PKT: market closed, engine idles ----
            if market_closed():
                set_phase(f"market closed ({cfg.MARKET_CLOSED_START}–{cfg.MARKET_CLOSED_END} PKT) "
                          f"— gold & DXY offline, engine idle")
                time.sleep(30)
                continue

            if not STATE["connected"]:
                set_phase("connecting to MT5 terminal…")
                if engine.connect():
                    with LOCK:
                        STATE["connected"] = True
                        STATE["error"] = None
                else:
                    with LOCK:
                        STATE["error"] = engine.last_error
                    time.sleep(10)
                    continue

            set_phase("fetching market data — gold + DXY bars")
            data = engine.fetch()
            if data is None:
                with LOCK:
                    STATE["error"] = engine.last_error
                time.sleep(cfg.REFRESH_SECONDS)
                continue

            snap = data["snapshot"]
            set_phase("scoring signal — regime · momentum · session")
            sig = evaluate(snap)

            # ---- local log when the signal changes, with the reason ----
            if prev_sig is not None and sig["direction"] != prev_sig["direction"]:
                reasons = _change_reasons(_signal_flags(prev_sig), _signal_flags(sig), snap)
                trader._log(f"Signal {prev_sig['direction']} → {sig['direction']} "
                            f"(score {prev_sig['score']} → {sig['score']})"
                            + (": " + "; ".join(reasons) if reasons else ""))
            prev_sig = sig

            set_phase("checking positions & risk gates")
            trader.check_closed_trades()
            trader.manage_position(snap)   # TP1 breakeven / trailing / watchdog

            # ---- one structured diagnostics event per candle ----
            bar = snap["bar_time"]
            if bar != last_logged_bar:
                log_event("bar", bar=bar, gold=snap["gold_price"],
                          dxy=round(snap["dxy_value"], 3),
                          corr=round(snap["correlation"], 3),
                          corr_z=round(snap["corr_z"], 2),
                          score=sig["score"], direction=sig["direction"],
                          regime=sig["regime_active"])
                last_logged_bar = bar

            # ---- next-candle confirmation state machine ----
            fire_dir = sig["raw_direction"] if sig["fire"] else None

            # expiry is candle-based, symmetric with confirmation: a confirmed
            # signal dies only after a FULL candle passes without a single fire —
            # intra-bar momentum flickers (90 -> 30 for seconds) no longer kill it
            if confirmed_dir and bar != expiry_bar:
                if not fired_this_bar:
                    trader._log(f"⌛ Signal {confirmed_dir} EXPIRED — did not fire for a "
                                f"full {cfg.TIMEFRAME} candle (score now {sig['score']})",
                                "warn", discord=True)
                    confirmed_dir = None
                fired_this_bar = False
                expiry_bar = bar
            if confirmed_dir and fire_dir == confirmed_dir:
                fired_this_bar = True

            if fire_dir is None:
                if pending:
                    trader._log(f"Pending {pending['dir']} cancelled — signal faded "
                                f"(score {sig['score']})")
                    pending = None
            elif fire_dir == confirmed_dir:
                pending = None
            elif pending is None or pending["dir"] != fire_dir:
                pending = {"dir": fire_dir, "score": sig["score"], "bar": bar}
                trader._log(f"⏳ {fire_dir} signal firing (score {sig['score']}) — "
                            f"waiting for next {cfg.TIMEFRAME} candle to confirm")
            elif bar != pending["bar"]:
                # still firing on a NEW candle -> confirmed (the only Discord signal message)
                confirmed_dir = fire_dir
                pending = None
                fired_this_bar = True
                expiry_bar = bar
                # suggested trade levels (TRADE_MANAGEMENT.md) — makes the signal
                # actionable manually, especially outside entry hours
                px = snap["gold_ask"] if fire_dir == "BUY" else snap["gold_price"]
                plan, plan_reason = trader.plan_trade(fire_dir, snap, px)
                suffix = ("" if in_entry_window()
                          else " | ⚠️ outside entry hours — bot will NOT enter, manual levels")
                if plan:
                    log_event("signal_confirmed", direction=fire_dir, score=sig["score"],
                              corr=round(snap["correlation"], 3), bar=bar,
                              entry=round(px, 2), sl=round(plan["sl"], 2),
                              tp1=round(plan["tp1"], 2), tp2=round(plan["tp2"], 2))
                    trader._log(f"✅ SIGNAL CONFIRMED {fire_dir} — score {sig['score']}, "
                                f"corr {snap['correlation']:+.2f} | entry ~{px:.2f} | "
                                f"SL {plan['sl']:.2f} | TP1 {plan['tp1']:.2f} (S/R) | "
                                f"TP2 {plan['tp2']:.2f}{suffix}",
                                "good", discord=True)
                else:
                    log_event("signal_confirmed", direction=fire_dir, score=sig["score"],
                              corr=round(snap["correlation"], 3), bar=bar, no_plan=plan_reason)
                    trader._log(f"✅ SIGNAL CONFIRMED {fire_dir} — score {sig['score']}, "
                                f"corr {snap['correlation']:+.2f} | ⚠️ no trade plan: "
                                f"{plan_reason}{suffix}",
                                "good", discord=True)
                # opposite-signal exit: confirmed signal against the open position
                if cfg.EXIT_ON_OPPOSITE and sig["score"] >= cfg.OPPOSITE_EXIT_SCORE:
                    if any(p["type"] != fire_dir for p in trader.open_positions()):
                        set_phase(f"opposite signal — closing position against {fire_dir}")
                        trader.close_all(reason=f"confirmed opposite {fire_dir} "
                                                f"(score {sig['score']})")

            # ---- auto-trade (once per bar) ----
            if (STATE["auto_trade"] and sig["auto_eligible"]
                    and bar != STATE["last_executed_bar"]):
                set_phase(f"executing {sig['raw_direction']} — score {sig['score']}")
                executed = trader.execute(sig["raw_direction"], snap, sig["score"])
                if executed:
                    with LOCK:
                        STATE["last_executed_bar"] = bar

            # ---- chart series (last 150 bars) ----
            df = data["df"].tail(150)
            series = {
                "time": [t.strftime("%d %H:%M") for t in df.index],
                "gold": [round(v, 2) for v in df["gold"]],
                "dxy": [round(v, 3) for v in df["dxy"]],
                "corr": [round(v, 3) if v == v else None for v in df["corr"]],
            }

            with LOCK:
                STATE.update({
                    "snapshot": snap,
                    "signal": sig,
                    "series": series,
                    "positions": trader.open_positions(),
                    "account": trader.account(),
                    "log": trader.log[-50:],
                    "last_update": datetime.now().strftime("%H:%M:%S"),
                    "error": None,
                    "pending_signal": pending,
                    "confirmed_signal": confirmed_dir,
                    "phase": f"watching market — next scan in {cfg.REFRESH_SECONDS}s "
                             f"(candle {bar[-8:-3]})",
                })
        except Exception:
            tb = traceback.format_exc(limit=2)
            log_event("error", trace=tb)
            with LOCK:
                STATE["error"] = tb
                STATE["connected"] = False
        time.sleep(cfg.REFRESH_SECONDS)


@app.route("/")
def dashboard():
    return render_template("dashboard.html",
                           gold_symbol=cfg.GOLD_SYMBOL,
                           refresh_ms=cfg.REFRESH_SECONDS * 1000,
                           signal_threshold=cfg.SIGNAL_THRESHOLD)


@app.route("/api/state")
def api_state():
    with LOCK:
        payload = dict(STATE)
    payload["can_trade"] = trader.can_trade()
    payload["in_session"] = in_session()
    payload["entry_window"] = in_entry_window()
    payload["market_closed"] = market_closed()
    payload["trades_today"] = trader.trades_today
    payload["wins"] = trader.wins
    payload["losses"] = trader.losses
    payload["daily_pnl"] = trader.daily_pnl
    payload["daily_loss_limit"] = trader.daily_loss_limit
    payload["daily_profit_target"] = trader.daily_profit_target
    payload["daily_limit_hit"] = trader.daily_limit_hit
    payload["halted"] = trader.halted
    payload["consecutive_losses"] = trader.consecutive_losses
    payload["max_consecutive_losses"] = cfg.MAX_CONSECUTIVE_LOSSES
    payload["lot_mode"] = trader.lot_mode
    payload["manual_lot"] = trader.manual_lot
    return jsonify(payload)


@app.route("/api/toggle_auto", methods=["POST"])
def toggle_auto():
    with LOCK:
        STATE["auto_trade"] = not STATE["auto_trade"]
        val = STATE["auto_trade"]
    trader._log(f"Auto-trade turned {'ON' if val else 'OFF'} from dashboard",
                "warn" if val else "info")
    return jsonify({"auto_trade": val})


@app.route("/api/close_all", methods=["POST"])
def close_all():
    trader.close_all(reason="manual close (dashboard)")
    return jsonify({"ok": True})


@app.route("/api/report")
def report():
    """Today's report by default; ?day=YYYY-MM-DD for a past trading day."""
    day = request.args.get("day") or str(trading_day())
    return jsonify(build_report(day, save=False))


@app.route("/api/resume", methods=["POST"])
def resume():
    trader.resume()
    return jsonify({"ok": True, "halted": trader.halted})


@app.route("/api/settings", methods=["POST"])
def settings():
    body = request.get_json(silent=True) or {}
    changes = []
    if "daily_loss_limit" in body:
        trader.daily_loss_limit = max(0.0, float(body["daily_loss_limit"]))
        changes.append(f"daily loss limit {trader.daily_loss_limit:.0f}")
    if "daily_profit_target" in body:
        trader.daily_profit_target = max(0.0, float(body["daily_profit_target"]))
        changes.append(f"daily profit target {trader.daily_profit_target:.0f}")
    if body.get("lot_mode") in ("AUTO", "MANUAL"):
        trader.lot_mode = body["lot_mode"]
        changes.append(f"lot mode {trader.lot_mode}")
    if "manual_lot" in body:
        trader.manual_lot = max(cfg.MIN_LOT, min(cfg.MAX_LOT, float(body["manual_lot"])))
        changes.append(f"manual lot {trader.manual_lot}")
    if changes:
        # re-evaluate limit state against the new thresholds
        trader.daily_limit_hit = None
        trader._check_daily_limits()
        trader._save_state()
        trader._log("Settings updated: " + ", ".join(changes))
    return jsonify({
        "daily_loss_limit": trader.daily_loss_limit,
        "daily_profit_target": trader.daily_profit_target,
        "lot_mode": trader.lot_mode,
        "manual_lot": trader.manual_lot,
    })


if __name__ == "__main__":
    threading.Thread(target=engine_loop, daemon=True).start()
    print(f"GDX-CORR dashboard -> http://localhost:{cfg.DASHBOARD_PORT}")
    trader._log(f"🚀 Bot ACTIVE — engine running on port {cfg.DASHBOARD_PORT}, "
                f"auto-trade OFF (enable from dashboard)", "info", discord=True)
    try:
        app.run(host=cfg.DASHBOARD_HOST, port=cfg.DASHBOARD_PORT, debug=False)
    finally:
        trader._log("🛑 Bot CLOSED — engine offline, no new trades will open "
                    "(open positions keep their SL/TP on the MT5 server)", "warn", discord=True)
