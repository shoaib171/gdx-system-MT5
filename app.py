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
from signal_engine import evaluate, in_session
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
}
LOCK = threading.Lock()


def engine_loop():
    while True:
        try:
            if not STATE["connected"]:
                if engine.connect():
                    with LOCK:
                        STATE["connected"] = True
                        STATE["error"] = None
                else:
                    with LOCK:
                        STATE["error"] = engine.last_error
                    time.sleep(10)
                    continue

            data = engine.fetch()
            if data is None:
                with LOCK:
                    STATE["error"] = engine.last_error
                time.sleep(cfg.REFRESH_SECONDS)
                continue

            snap = data["snapshot"]
            sig = evaluate(snap)
            trader.check_closed_trades()

            # ---- auto-trade (once per bar) ----
            if (STATE["auto_trade"] and sig["auto_eligible"]
                    and snap["bar_time"] != STATE["last_executed_bar"]):
                executed = trader.execute(sig["raw_direction"], snap["atr"], sig["score"])
                if executed:
                    with LOCK:
                        STATE["last_executed_bar"] = snap["bar_time"]

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
                })
        except Exception:
            with LOCK:
                STATE["error"] = traceback.format_exc(limit=2)
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
    payload["trades_today"] = trader.trades_today
    payload["max_trades"] = cfg.MAX_TRADES_PER_DAY
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
    trader.close_all()
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=engine_loop, daemon=True).start()
    print(f"GDX-CORR dashboard -> http://localhost:{cfg.DASHBOARD_PORT}")
    app.run(host=cfg.DASHBOARD_HOST, port=cfg.DASHBOARD_PORT, debug=False)
