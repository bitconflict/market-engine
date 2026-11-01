"""Decision ledger: append-only snapshots + self-scoring track record."""
import json, os, time, datetime
from . import paths

ROOT = paths.home()
LED = paths.ledger_dir()
DEC = paths.decisions_path()
NAV = paths.nav_path()
os.makedirs(LED, exist_ok=True)


def today():
    return datetime.date.today().isoformat()


def snapshot(state):
    """Append today's decisions + NAV. One entry per calendar day (overwrites same-day)."""
    d = today()
    entry = {"date": d, "regime": state["regime"]["label"],
             "decisions": {h["ticker"]: {"action": h["action"], "composite": h.get("composite"),
                                         "px": h.get("px") or (h.get("value") / h["shares"] if h.get("shares") else None)}
                           for h in state["holdings"] + state["watchlist"] if h.get("action")}}
    rows = [json.loads(l) for l in open(DEC)] if os.path.exists(DEC) else []
    rows = [r for r in rows if r["date"] != d]
    rows.append(entry)
    rows.sort(key=lambda r: r["date"])
    with open(DEC, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    # NAV
    nav = json.load(open(NAV)) if os.path.exists(NAV) else []
    nav = [n for n in nav if n["date"] != d]
    nav.append({"date": d, "total": state["portfolio"]["total"], "invested": state["portfolio"]["invested"],
                "cash": state["portfolio"]["cash"], "regime": state["regime"]["label"]})
    nav.sort(key=lambda n: n["date"])
    json.dump(nav, open(NAV, "w"))


def _closes_by_date(rec):
    p = rec.get("prices") or {}
    return dict(zip(p.get("dates", []), p.get("close", [])))


def track_record(recs):
    """Score decisions N days ago against SPY over the same window. BUY/ADD win if beat SPY;
    SELL/TRIM/AVOID win if the ticker lagged SPY."""
    if not os.path.exists(DEC):
        return {"windows": {}, "log": []}
    rows = [json.loads(l) for l in open(DEC)]
    spy = _closes_by_date(recs.get("SPY") or {})
    spy_dates = sorted(spy.keys())
    out_windows = {}
    log = []
    for horizon in (30, 60, 90):
        wins, losses, excess = 0, 0, []
        for snap in rows:
            d0 = snap["date"]
            if d0 not in spy:
                continue
            i0 = spy_dates.index(d0)
            # find spy date ~horizon trading days later (use index arithmetic on trade dates)
            j = i0 + horizon
            if j >= len(spy_dates):
                continue
            d1 = spy_dates[j]
            spy_ret = spy[d1] / spy[d0] - 1.0
            for tk, dec in snap["decisions"].items():
                act = dec.get("action")
                if act not in ("BUY", "ADD", "SELL", "TRIM", "AVOID"):
                    continue
                cl = _closes_by_date(recs.get(tk) or {})
                if d0 not in cl or d1 not in cl or not cl[d0]:
                    continue
                tk_ret = cl[d1] / cl[d0] - 1.0
                ex = tk_ret - spy_ret
                won = ex > 0 if act in ("BUY", "ADD") else ex < 0
                wins += 1 if won else 0
                losses += 0 if won else 1
                excess.append(ex)
                log.append({"decision_date": d0, "horizon": horizon, "ticker": tk, "action": act,
                            "ticker_ret": round(tk_ret, 4), "spy_ret": round(spy_ret, 4),
                            "excess": round(ex, 4), "win": won})
        n = wins + losses
        out_windows[horizon] = {"calls": n, "wins": wins,
                                "hit_rate": round(wins / n, 3) if n else None,
                                "avg_excess_vs_spy": round(sum(excess) / len(excess), 4) if excess else None,
                                "note": None if n else "no matured calls yet (needs >= " + str(horizon) + " trading days of ledger)"}
    log.sort(key=lambda x: (x["decision_date"], x["horizon"]))
    return {"windows": out_windows, "log": log[-200:]}
