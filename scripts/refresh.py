"""Refresh pipeline: fetch data -> score -> ledger snapshot -> digest (stdout, for cron).

Run from your portfolio instance dir (the one holding config.json), or export
MARKET_ENGINE_HOME to point at it. Writes data/state.json + ledger snapshots
there; prints a Telegram-ready digest to stdout.
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # bare checkouts without `pip install -e .`
import warnings
warnings.filterwarnings("ignore")
from engine import data, rules, ledger, paths


def run(force=False, cash=None):
    cfg = rules.load_config()
    tickers = list(cfg["positions"].keys()) + [t for t in cfg.get("watchlist", [])] + cfg.get("scan_universe", []) + cfg.get("benchmarks", [])
    tickers = list(dict.fromkeys(tickers))
    recs = data.fetch_many(tickers, force=force)
    cash = cfg.get("cash", 0.0) if cash is None else cash
    state = rules.portfolio_state(recs, cfg, cash=cash)
    state["baseline"] = {"date": cfg.get("baseline_date"), "value": cfg.get("baseline_value")}
    # Real cost basis from broker (e.g. Robinhood avg buy price) -> true unrealized P&L
    cb = cfg.get("cost_basis") or {}
    tot_cost = tot_value = 0.0
    for h in state.get("holdings", []):
        c = cb.get(h["ticker"])
        if c:
            h["cost_per_share"] = c
            h["cost_total"] = round(c * h.get("shares", 0), 2)
            h["unrealized"] = round(h.get("value", 0) - h["cost_total"], 2)
            h["unrealized_pct"] = round(h["unrealized"] / h["cost_total"] * 100, 1) if h["cost_total"] else None
            tot_cost += h["cost_total"]
            tot_value += h.get("value", 0)
        else:
            h["cost_per_share"] = h["cost_total"] = h["unrealized"] = h["unrealized_pct"] = None
    if tot_cost:
        p = state["portfolio"]
        p["cost_basis_total"] = round(tot_cost, 2)
        p["unrealized_total"] = round(tot_value - tot_cost, 2)
        p["unrealized_total_pct"] = round((tot_value - tot_cost) / tot_cost * 100, 2)
    ledger.snapshot(state)
    try:
        state["track"] = ledger.track_record(recs)
    except Exception as e:
        state["track"] = {"windows": {}, "log": [], "error": str(e)[:120]}
    # nav history
    navf = paths.nav_path()
    state["nav_history"] = json.load(open(navf)) if os.path.exists(navf) else []
    # compact sparklines for UI
    for h in state["holdings"] + state["watchlist"] + state.get("candidates", []):
        pr = (recs.get(h["ticker"]) or {}).get("prices") or {}
        cl = pr.get("close") or []
        src = cl[-130:] if len(cl) > 130 else cl
        sp = src[::2]
        if sp and sp[-1] != cl[-1]:
            sp = sp + [cl[-1]]  # never drop the newest bar: sparkline must end at the quoted price
        h["spark"] = sp
    out = paths.state_path()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(state, open(out, "w"), default=str)
    return state, recs


def digest(state):
    p, rg = state["portfolio"], state["regime"]
    base = state["baseline"].get("value")
    bdate = state["baseline"].get("date")
    vs = ""
    if base:
        chg = (p["total"] - base) / base * 100
        vs = f" ({chg:+.1f}% vs {bdate or 'baseline'} baseline)"
    lines = [f"🧭 Strategist — {state['as_of']}"]
    lines.append(f"Book ${p['total']:,.0f}{vs} | regime: {rg['label'].upper()} | cash {p['cash_pct']*100:.0f}% (target {p['cash_target']*100:.0f}%) | spec {p['speculative_pct']*100:.0f}% (budget {p['speculative_budget']*100:.0f}%)")
    if p.get("cost_basis_total"):
        lines.append(f"Unrealized vs broker cost: ${p['unrealized_total']:+,.0f} ({p['unrealized_total_pct']:+.1f}%) on ${p['cost_basis_total']:,.0f} invested")
    acts = state["actions"]
    if acts:
        lines.append("Actions:")
        for a in acts[:12]:
            why = "; ".join((a.get("reasons") or [])[:2]) or "; ".join((a.get("flags") or [])[:2])
            lines.append(f"  {a['action']} {a['ticker']}" + (f" ({a['composite']:.0f})" if a.get("composite") is not None else "") + (f" — {why}" if why else ""))
    else:
        lines.append("Actions: none new — book is stable per engine")
    cands = [c for c in state.get("candidates", []) if c.get("action") == "BUY"][:3]
    if cands:
        lines.append("Screener (outside book):")
        for c in cands:
            lines.append(f"  BUY {c['ticker']} ({c['composite']:.0f}) — ${c['px']:,.2f}")
    if p.get("over_cap"):
        lines.append(f"Over {round(p.get('max_position_weight', 0.12)*100)}% cap: {', '.join(p['over_cap'])}")
    tr = state.get("track", {}).get("windows", {}).get(30, {})
    if tr.get("calls"):
        lines.append(f"Ledger 30d: {tr['wins']}/{tr['calls']} calls right vs SPY")
    return "\n".join(lines)


if __name__ == "__main__":
    force = "--force" in sys.argv
    try:
        st, _ = run(force=force)
    except FileNotFoundError as e:
        if "config.json" in str(e):
            sys.exit(f"error: no config.json in instance dir ({paths.home()}).\n"
                     "Run from your portfolio directory or set MARKET_ENGINE_HOME; "
                     "template: config.example.json")
        raise
    print(digest(st))
