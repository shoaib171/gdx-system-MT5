"""
daily_logger.py — daily JSON event log + report builder for monitoring/diagnosis.

Every event is appended as one JSON line to  logs/<trading-day>.jsonl
(bars analyzed, signal changes, confirmations, entries, closes, errors).
A "trading day" runs TRADING_DAY_START → TRADING_DAY_START (03:00 PKT),
not midnight — so overnight sessions stay in one day's file.

build_report() aggregates a day's events into logs/report_<day>.json.
"""
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import config as cfg

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def now_pkt() -> datetime:
    return datetime.now(ZoneInfo(cfg.SESSION_TZ))


def trading_day(dt: datetime | None = None) -> date:
    """Day key for logs/stats — a new day starts at TRADING_DAY_START (03:00 PKT)."""
    dt = dt or now_pkt()
    h, m = map(int, cfg.TRADING_DAY_START.split(":"))
    return (dt - timedelta(hours=h, minutes=m)).date()


def log_event(event: str, **fields):
    rec = {"ts": now_pkt().isoformat(timespec="seconds"), "event": event, **fields}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{trading_day()}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass    # logging must never break the engine


def _read_day(day) -> list[dict]:
    path = os.path.join(LOG_DIR, f"{day}.jsonl")
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return events


def build_report(day=None, save: bool = True) -> dict:
    day = str(day or trading_day())
    ev = _read_day(day)
    entries = [e for e in ev if e["event"] == "entry"]
    closes = [e for e in ev if e["event"] == "close"]
    wins = [c for c in closes if c.get("pnl", 0) >= 0]
    losses = [c for c in closes if c.get("pnl", 0) < 0]
    logs = [e for e in ev if e["event"] == "log"]
    report = {
        "day": day,
        "generated_at": now_pkt().isoformat(timespec="seconds"),
        "total_events": len(ev),
        "bars_analyzed": sum(1 for e in ev if e["event"] == "bar"),
        "signals_confirmed": sum(1 for e in ev if e["event"] == "signal_confirmed"),
        "signals_blocked": sum(1 for e in logs if "blocked:" in e.get("msg", "")),
        "entries": len(entries),
        "closes": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": round(sum(c.get("pnl", 0) for c in closes), 2),
        "errors": sum(1 for e in ev if e["event"] == "error"),
        "trades": entries,
        "close_details": closes,
        "confirmed_signals": [e for e in ev if e["event"] == "signal_confirmed"],
    }
    if save:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, f"report_{day}.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    return report
