"""
signal_engine.py — converts the correlation snapshot into a scored signal.

Logic (inverse-correlation regime model):
  Gold and DXY normally move opposite. When that regime is ACTIVE
  (rolling corr <= -0.60), trade gold AGAINST dollar momentum:
    DXY falling  -> BUY XAUUSD
    DXY rising   -> SELL XAUUSD
  Bonus when correlation z-score shows gold decoupling in trade direction.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import config as cfg


def in_session(now: datetime | None = None) -> bool:
    tz = ZoneInfo(cfg.SESSION_TZ)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    start_h, start_m = map(int, cfg.SESSION_START.split(":"))
    end_h, end_m = map(int, cfg.SESSION_END.split(":"))
    minutes = now.hour * 60 + now.minute
    return (start_h * 60 + start_m) <= minutes <= (end_h * 60 + end_m)


def evaluate(snap: dict) -> dict:
    w = cfg.SCORE_WEIGHTS
    corr = snap["correlation"]

    # --- 1. Regime: inverse correlation active ---
    regime_active = corr <= cfg.CORR_REGIME_THRESHOLD
    # graded: -0.60 -> partial, -1.0 -> full weight
    if regime_active:
        depth = min(1.0, (abs(corr) - abs(cfg.CORR_REGIME_THRESHOLD)) /
                    (1.0 - abs(cfg.CORR_REGIME_THRESHOLD)) + 0.5)
        regime_pts = round(w["regime"] * min(1.0, depth), 1)
    else:
        regime_pts = 0.0

    # --- 2. DXY momentum direction ---
    dxy_bear = (snap["dxy_ema_fast"] < snap["dxy_ema_slow"]) and (snap["dxy_roc"] < -cfg.ROC_THRESHOLD)
    dxy_bull = (snap["dxy_ema_fast"] > snap["dxy_ema_slow"]) and (snap["dxy_roc"] > cfg.ROC_THRESHOLD)
    if dxy_bear:
        direction = "BUY"    # dollar falling -> gold up
        momentum_pts = float(w["dxy_momentum"])
    elif dxy_bull:
        direction = "SELL"   # dollar rising -> gold down
        momentum_pts = float(w["dxy_momentum"])
    else:
        direction = "NEUTRAL"
        momentum_pts = 0.0

    # --- 3. Gold's own momentum agrees ---
    gold_bull = snap["gold_ema_fast"] > snap["gold_ema_slow"]
    gold_agrees = (direction == "BUY" and gold_bull) or (direction == "SELL" and not gold_bull)
    gold_pts = float(w["gold_momentum"]) if (direction != "NEUTRAL" and gold_agrees) else 0.0

    # --- 4. Decoupling bonus (gold showing independent strength/weakness) ---
    z = snap["corr_z"]
    decoupling = abs(z) >= 1.5
    decouple_pts = float(w["decoupling"]) if (direction != "NEUTRAL" and decoupling) else 0.0

    # --- 5. Session ---
    session_ok = in_session()
    session_pts = float(w["session"]) if session_ok else 0.0

    score = round(regime_pts + momentum_pts + gold_pts + decouple_pts + session_pts, 1)
    fire = direction != "NEUTRAL" and regime_active and score >= cfg.SIGNAL_THRESHOLD

    return {
        "direction": direction if fire else ("LEAN " + direction if direction != "NEUTRAL" else "NEUTRAL"),
        "raw_direction": direction,
        "score": score,
        "fire": fire,
        "auto_eligible": fire and score >= cfg.AUTO_TRADE_THRESHOLD,
        "regime_active": regime_active,
        "session_ok": session_ok,
        "breakdown": {
            "Inverse regime (corr {:+.2f})".format(corr): regime_pts,
            "DXY momentum": momentum_pts,
            "Gold momentum agrees": gold_pts,
            "Decoupling z {:+.1f}".format(z): decouple_pts,
            "London/NY session": session_pts,
        },
    }
