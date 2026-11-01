#!/usr/bin/env python3
"""Build deterministic test fixtures: complete books (config + records) with
golden engine outputs, plus a mixed-outcome ledger.

Run from the repo root:  python tests/fixtures/_build.py
Fixtures are committed; regenerate ONLY when a rules change intentionally
reshapes behavior (review the golden diff in git before keeping it).

Each book fixture is a full mini-instance:
    books/<name>/config.json   a valid engine config (synthetic tickers only)
    books/<name>/recs.json     the exact fetch-layer records the engine sees
    books/<name>/golden.json   engine output captured at build time: regime,
                               per-ticker action/composite/flags, portfolio
                               invariants, candidates
The ledgers/mixed_track fixture has hand-designed outcomes (winners, losers,
sell-calls that were right) with fixed dates inside the SPY price series, so
track_record() scoring is deterministic without clock dependence.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from conftest import make_rec, bench_recs, make_cfg, dates, WEAK_INFO, STRONG_INFO  # noqa: E402
from engine import rules, ledger  # noqa: E402

FIX = HERE


def dump(rel, obj):
    p = os.path.join(FIX, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, indent=1, default=str)
            f.write("\n")
    print("wrote", rel)


def neutral_info(**over):
    """Mid-range fundamentals: pillars land near 50 unless overridden."""
    info = dict(STRONG_INFO)
    info.update({"revenueGrowth": 0.03, "earningsGrowth": 0.02, "trailingPE": 18.0,
                 "forwardPE": 17.0, "pegRatio": 2.2, "returnOnEquity": 0.08,
                 "returnOnAssets": 0.03, "profitMargins": 0.06, "grossMargins": 0.25,
                 "shortName": "Neutral Co"})
    info.update(over)
    return info


def build_book(name, cfg, recs):
    """Run the pure engine over (recs, cfg) and capture the golden."""
    state = rules.portfolio_state(recs, cfg, cash=cfg.get("cash", 0.0))
    golden = {
        "regime": state["regime"]["label"],
        "actions": {h["ticker"]: {"action": h["action"],
                                  "composite": round(h["composite"], 1) if h.get("composite") is not None else None,
                                  "flags": sorted(h.get("flags") or [])}
                    for h in state["holdings"] + state["watchlist"] + state.get("candidates", [])},
        "portfolio": {k: state["portfolio"].get(k) for k in
                      ("cash_pct", "cash_target", "speculative_pct", "speculative_budget",
                       "invested", "total", "over_cap")},
        "candidates": [{"ticker": c["ticker"], "action": c["action"]}
                       for c in state.get("candidates", [])],
    }
    dump(f"books/{name}/config.json", cfg)
    dump(f"books/{name}/recs.json", recs)
    dump(f"books/{name}/golden.json", golden)
    return state, golden


# ---------------------------------------------------------------- book 1
def bull_growth():
    b = bench_recs(bull=True, vix=18.0)
    cfg = make_cfg(
        positions={"FASTG": 500, "SLOWD": 1000, "IDXB": 500, "BIGC": 9000,
                   "RISKY": 3000, "BURNR": 600},
        cost_basis={"FASTG": 20.0, "SLOWD": 80.0, "IDXB": 30.0, "BIGC": 5.0,
                    "RISKY": 5.0, "BURNR": 30.0},
        cash=250000.0,
        watchlist=["NEWA", "MIDP"],
        scan_universe=["NEWA", "MIDP", "BANGR", "STEADY"],
        etf_tickers=["IDXB"],
        speculative_tickers=["RISKY"],
        baseline_date="2024-06-01", baseline_value=450000.0,
        manual_overlay={"RISKY": "pre-earn flyer; sized small on purpose"},
    )
    recs = dict(b, **{
        "FASTG": make_rec("FASTG", start=30.0, drift=0.004, current_price=None),
        "SLOWD": make_rec("SLOWD", start=90.0, drift=-0.004, healthy=False,
                          info={"revenueGrowth": -0.25, "earningsGrowth": -0.4, "debtToEquity": 260.0,
                                "returnOnEquity": -0.25, "freeCashflow": -8e8, "marketCap": 9e9,
                                "forwardPE": None, "trailingPE": None, "pegRatio": None,
                                "enterpriseToEbitda": None, "shortName": "SLOWD"}),
        "IDXB": make_rec("IDXB", start=33.0, drift=0.0008, info=WEAK_INFO),
        "BIGC": make_rec("BIGC", start=6.1, drift=0.0012),
        "RISKY": make_rec("RISKY", start=7.4, drift=0.0028),
        "BURNR": make_rec("BURNR", start=25.0, drift=0.001, healthy=False,
                          info={"revenueGrowth": -0.05, "earningsGrowth": -0.1, "debtToEquity": 90.0,
                                "returnOnEquity": 0.04, "freeCashflow": -6e8, "marketCap": 8e9,
                                "forwardPE": 40.0, "trailingPE": None, "pegRatio": None,
                                "enterpriseToEbitda": None, "shortName": "BURNR"}),
        "NEWA": make_rec("NEWA", start=22.0, drift=0.0042),
        "MIDP": make_rec("MIDP", start=40.0, drift=0.0004, info=neutral_info()),
        "BANGR": make_rec("BANGR", start=50.0, drift=-0.005, healthy=False,
                          info={"revenueGrowth": -0.35, "earningsGrowth": -0.5, "debtToEquity": 300.0,
                                "returnOnEquity": -0.3, "freeCashflow": -9e8, "marketCap": 3e9,
                                "forwardPE": None, "trailingPE": None, "pegRatio": None,
                                "enterpriseToEbitda": None, "shortName": "BANGR"}),
        "STEADY": make_rec("STEADY", start=100.0, drift=0.0009, info=neutral_info()),
    })
    # px: prices use last close; current_price unset -> metrics fall back to last close
    state, gold = build_book("bull_growth", cfg, recs)
    return gold


# ---------------------------------------------------------------- book 2
def bear_defensive():
    b = bench_recs(bull=False, vix=28.0)
    cfg = make_cfg(
        positions={"QUALI": 300, "CRUCH": 1200},
        cost_basis={"QUALI": 40.0, "CRUCH": 70.0},
        cash=300000.0,
        watchlist=["NEWB"],
        scan_universe=["NEWB", "STEADY"],
        baseline_date="2024-06-01", baseline_value=500000.0,
    )
    recs = dict(b, **{
        "QUALI": make_rec("QUALI", start=55.0, drift=0.0028),
        "CRUCH": make_rec("CRUCH", start=78.0, drift=-0.0045, healthy=False,
                          info={"revenueGrowth": -0.3, "earningsGrowth": -0.45, "debtToEquity": 280.0,
                                "returnOnEquity": -0.28, "freeCashflow": -7e8, "marketCap": 7e9,
                                "forwardPE": None, "trailingPE": None, "pegRatio": None,
                                "enterpriseToEbitda": None, "shortName": "CRUCH"}),
        "NEWB": make_rec("NEWB", start=30.0, drift=0.004),
        "STEADY": make_rec("STEADY", start=100.0, drift=0.0009, info=neutral_info()),
    })
    state, gold = build_book("bear_defensive", cfg, recs)
    assert state["regime"]["label"] == "bear", state["regime"]
    return gold


# ---------------------------------------------------------------- book 3
def flagged_gates():
    """One ticker per red-flag gate + a clean control, so every downgrade
    path is pinned by name."""
    b = bench_recs(bull=True, vix=18.0)
    names = ["G_FS", "G_DIL", "G_PE", "G_DD", "G_TT", "G_LOS", "G_TWO", "G_CLEAN"]
    cfg = make_cfg(
        positions={n: 200 for n in names},
        cost_basis={n: 40.0 for n in names},
        cash=150000.0,
        baseline_date="2024-06-01", baseline_value=160000.0,
    )
    weak = {"debtToEquity": 240.0, "freeCashflow": -5e8, "marketCap": 6e9,
            "enterpriseToEbitda": None, "pegRatio": None, "trailingPE": None}
    recs = dict(b)
    recs["G_FS"] = make_rec("G_FS", start=40.0, drift=-0.002, healthy=False,
                            info={**weak, "revenueGrowth": -0.10, "earningsGrowth": -0.15,
                                  "returnOnEquity": -0.05, "forwardPE": None, "shortName": "G_FS"})
    recs["G_DIL"] = make_rec("G_DIL", start=40.0, drift=-0.001, healthy=False,
                             info={**weak, "revenueGrowth": 0.02, "earningsGrowth": 0.0,
                                   "returnOnEquity": 0.03, "forwardPE": 35.0, "shortName": "G_DIL"})
    recs["G_PE"] = make_rec("G_PE", start=40.0, drift=0.0012, healthy=True,
                            info={**STRONG_INFO, "forwardPE": 240.0, "pegRatio": 6.0, "shortName": "G_PE"})
    recs["G_DD"] = make_rec("G_DD", start=220.0, n=300, drift=0.0, info=neutral_info())
    # engineer >50% off high: force a declining path then flat tail
    dd = make_rec("G_DD", start=220.0, drift=-0.004)
    recs["G_DD"] = dd
    recs["G_TT"] = make_rec("G_TT", start=80.0, drift=-0.003, info=neutral_info())
    recs["G_LOS"] = make_rec("G_LOS", start=40.0, drift=0.0008,
                             info={**neutral_info(), "forwardPE": -12.0, "shortName": "G_LOS"})
    recs["G_TWO"] = make_rec("G_TWO", start=40.0, drift=-0.003, healthy=False,
                             info={**weak, "revenueGrowth": -0.2, "earningsGrowth": -0.3,
                                   "returnOnEquity": -0.2, "forwardPE": None, "shortName": "G_TWO"})
    recs["G_CLEAN"] = make_rec("G_CLEAN", start=40.0, drift=0.0035)
    state, gold = build_book("flagged_gates", cfg, recs)
    return gold


# ---------------------------------------------------------------- ledger
def mixed_ledger():
    """Fixed-date ledger whose grading outcome is hand-designed:
    WINB  BUY  at d100, +25% run      -> win (outperformed SPY)
    LOSEB BUY  at d100, -20%          -> loss
    SELLW SELL at d100, -18% after    -> win (lagged SPY as a sell)
    TRIMW TRIM at d100, -12% after    -> win
    HOLDX HOLD at d100 (never graded, must be excluded)
    SPY drifts gently up on a 500-day series; snapshot date d100 leaves
    90+ trading days of runway for every horizon.
    """
    n = 500
    dts = dates(n, start="2023-06-01")
    d0 = 100

    def series(start, pre, post, k):
        closes = []
        p = start
        for i in range(n):
            closes.append(round(p, 4))
            p *= (1 + (pre if i < k else post))
        return closes

    spy = series(100.0, 0.0006, 0.0006, d0)
    spyrec = {"ticker": "SPY", "ok": True,
              "prices": {"dates": dts, "close": spy, "volume": [1e6] * n}, "info": {}}
    book = {"WINB": (60.0, 0.0, 0.0028, 8e9), "LOSEB": (30.0, 0.0, -0.0025, 4e9),
            "SELLW": (45.0, 0.0, -0.0022, 5e9), "TRIMW": (25.0, 0.0, -0.0015, 3e9),
            "HOLDX": (50.0, 0.0, -0.0040, 6e9)}
    recs = {"SPY": spyrec}
    for tk, (st, pre, post, mc) in book.items():
        rec = make_rec(tk, start=st, n=n, drift=0.0001,
                       info={**STRONG_INFO, "marketCap": mc, "shortName": tk})
        cl = series(st, pre, post, d0)
        rec["prices"] = {"dates": dts, "close": cl, "volume": [1e6] * n}
        rec["_closes"] = cl
        recs[tk] = rec

    entry = {"date": dts[d0], "regime": "bull", "decisions": {
        "WINB": {"action": "BUY", "composite": 78.0, "px": 60.0},
        "LOSEB": {"action": "BUY", "composite": 71.0, "px": 30.0},
        "SELLW": {"action": "SELL", "composite": 22.0, "px": 45.0},
        "TRIMW": {"action": "TRIM", "composite": 38.0, "px": 25.0},
        "HOLDX": {"action": "HOLD", "composite": 55.0, "px": 50.0},
    }}
    jsonl = "\n".join(json.dumps(e) for e in [entry]) + "\n"
    nav = [{"date": dts[d0], "total": 500000.0, "cash": 200000.0, "invested": 300000.0,
            "baseline_pct": 0.0, "holdings": {}}]

    # golden: run the real scorer against the fixture (temporarily repoint DEC)
    tmp = os.path.join(FIX, ".tmp_dec.jsonl")
    open(tmp, "w").write(jsonl)
    real = ledger.DEC
    try:
        ledger.DEC = tmp
        tr = ledger.track_record(recs)
    finally:
        ledger.DEC = real
        os.remove(tmp)
    golden = {"windows": {str(k): v for k, v in tr["windows"].items()},
              "spot_log": [l for l in tr["log"] if l["horizon"] == 30]}
    dump("ledgers/mixed_track/recs.json", recs)
    dump("ledgers/mixed_track/decisions.jsonl", jsonl)
    dump("ledgers/mixed_track/nav.json", nav)
    dump("ledgers/mixed_track/golden_track.json", golden)
    return golden


if __name__ == "__main__":
    import io, contextlib
    for fn in (bull_growth, bear_defensive, flagged_gates, mixed_ledger):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gold = fn()
        print(f"== {fn.__name__} ==")
        if fn is mixed_ledger:
            print(json.dumps(gold["windows"], indent=1))
        else:
            print(" regime:", gold["regime"], "| over_cap:", gold["portfolio"]["over_cap"])
            for t, v in gold["actions"].items():
                print(f"   {t:7s} {str(v['action']):5s} comp={v['composite']} flags={v['flags']}")
            print("   candidates:", gold["candidates"])
