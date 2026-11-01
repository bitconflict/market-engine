"""Fixture-book logic tests: pre-determined books with golden engine output.

Each fixture under tests/fixtures/books/<name>/ is a complete synthetic
instance (config.json + recs.json + golden.json, built by _build.py). These
tests run the real decision engine over each book and pin behavior two ways:

1. exact equality with the golden (composite to 0.1, action/flags exact), and
2. semantic invariants that must hold regardless of the golden — the actual
   design contract of the rules engine (gates only downgrade, bear pauses new
   buys, ETFs never scored, flags cap adds, caps exclude ETFs, ledger hygiene).

If a rules change intentionally reshapes behavior, regenerate goldens with
`python tests/fixtures/_build.py` and review the git diff before committing.
"""
import json
import os

import pytest

from conftest import ROOT  # noqa: F401  (sys.path side effect)
from engine import rules

FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "books")
BOOKS = sorted(d for d in os.listdir(FIXDIR)) if os.path.isdir(FIXDIR) else []
NEAR = 0.11  # composite tolerance (goldens round to 0.1)


def load(book, name):
    p = os.path.join(FIXDIR, book, name)
    if name.endswith(".jsonl"):
        return [json.loads(l) for l in open(p) if l.strip()]
    return json.load(open(p))


@pytest.fixture
def state(request):
    book = request.param
    cfg = load(book, "config.json")
    recs = load(book, "recs.json")
    return book, rules.portfolio_state(recs, cfg, cash=cfg.get("cash", 0.0))


@pytest.mark.parametrize("state", BOOKS, indirect=True)
def test_golden_regime_and_actions(state):
    book, st = state
    gold = load(book, "golden.json")
    assert st["regime"]["label"] == gold["regime"]
    got = {h["ticker"]: h for h in st["holdings"] + st["watchlist"] + st.get("candidates", [])}
    for tkr, g in gold["actions"].items():
        assert tkr in got, f"{book}: {tkr} missing from engine output"
        h = got[tkr]
        assert h["action"] == g["action"], f"{book}/{tkr}: {h['action']} != {g['action']}"
        assert sorted(h.get("flags") or []) == g["flags"], f"{book}/{tkr} flags drifted"
        if g["composite"] is None:
            assert h.get("composite") is None
        else:
            assert abs(h["composite"] - g["composite"]) < NEAR, f"{book}/{tkr} composite drifted"


@pytest.mark.parametrize("state", BOOKS, indirect=True)
def test_portfolio_invariants_match_golden(state):
    book, st = state
    gp = load(book, "golden.json")["portfolio"]
    p = st["portfolio"]
    assert sorted(p["over_cap"]) == sorted(gp["over_cap"])
    for k in ("cash_pct", "cash_target", "speculative_pct", "speculative_budget"):
        assert abs(p[k] - gp[k]) < 1e-6
    assert abs(p["invested"] - gp["invested"]) < 1.0
    assert abs(p["total"] - gp["total"]) < 1.0


@pytest.mark.parametrize("state", BOOKS, indirect=True)
def test_arithmetic_and_hygiene(state):
    book, st = state
    held = {h["ticker"] for h in st["holdings"]}
    for h in st["holdings"]:
        assert abs(h["value"] - h["px"] * h["shares"]) < 0.51
        assert abs(h["weight"] - h["value"] / st["portfolio"]["total"]) < 1e-6
    assert st["portfolio"]["invested"] <= st["portfolio"]["total"] + 0.01
    for c in st.get("candidates", []):
        assert c["ticker"] not in held
        assert (c.get("composite") or 0) >= 45
    # ETFs are anchors: never scored
    for h in st["holdings"]:
        if h["ticker"] in load(book, "config.json").get("etf_tickers", []):
            assert h.get("composite") is None
        # a flagged name can never sit in ADD/BUY (gates only downgrade)
        if h.get("flags"):
            assert h["action"] not in ("ADD", "BUY"), f"{h['ticker']} flagged yet {h['action']}"


# ---------- semantic contracts (survive intentional golden regeneration) ----

def test_bull_growth_book_behavior():
    cfg, recs = load("bull_growth", "config.json"), load("bull_growth", "recs.json")
    st = rules.portfolio_state(recs, cfg, cash=cfg["cash"])
    hs = {h["ticker"]: h for h in st["holdings"]}
    assert st["regime"]["label"] == "bull"
    assert st["portfolio"]["over_cap"] == ["BIGC"]           # exactly the one bloated name
    assert hs["SLOWD"]["action"] == "SELL"                    # broken thesis -> sell
    assert len(hs["SLOWD"]["flags"]) >= 3
    assert hs["IDXB"]["action"] == "HOLD" and hs["IDXB"].get("composite") is None  # ETF anchor
    p = st["portfolio"]
    assert p["speculative_pct"] > p["speculative_budget"]     # RISKY over its sleeve budget
    ws = {w["ticker"]: w for w in st["watchlist"]}
    assert ws["NEWA"]["action"] == "BUY" and ws["MIDP"]["action"] == "PASS"


def test_bear_defensive_book_behavior():
    cfg, recs = load("bear_defensive", "config.json"), load("bear_defensive", "recs.json")
    st = rules.portfolio_state(recs, cfg, cash=cfg["cash"])
    p = st["portfolio"]
    assert st["regime"]["label"] == "bear"
    assert abs(p["cash_target"] - cfg["cash_targets"]["bear"]) < 1e-9   # regime sets target
    ws = {w["ticker"]: w for w in st["watchlist"]}
    assert ws["NEWB"]["action"] == "WAIT"                     # bear pauses new buys...
    assert ws["NEWB"]["composite"] >= 70                      # ...even on a 70+ candidate
    # ...and no candidate anywhere is a BUY
    assert all(c["action"] != "BUY" for c in st.get("candidates", []))
    hs = {h["ticker"]: h for h in st["holdings"]}
    assert hs["CRUCH"]["action"] == "SELL"
    assert hs["QUALI"]["action"] == "ADD"                     # held winners are not force-sold


def test_flagged_gates_book_behavior():
    cfg, recs = load("flagged_gates", "config.json"), load("flagged_gates", "recs.json")
    st = rules.portfolio_state(recs, cfg, cash=cfg["cash"])
    hs = {h["ticker"]: h for h in st["holdings"]}
    def flagged(tkr, needle):
        assert any(needle in f for f in hs[tkr]["flags"]), f"{tkr} missing flag {needle!r}: {hs[tkr]['flags']}"
    flagged("G_FS", "F-score")
    flagged("G_DIL", "dilution")
    flagged("G_PE", "forward P/E > 150")
    flagged("G_LOS", "no forward earnings")
    flagged("G_DD", "50% below")
    flagged("G_TWO", "cash-burning")
    assert hs["G_CLEAN"]["flags"] == []                       # control is genuinely clean
    # single-flag caps: high composites that would be ADD are held back
    assert hs["G_PE"]["composite"] >= 70 and hs["G_PE"]["action"] != "ADD"
    assert hs["G_LOS"]["composite"] >= 70 and hs["G_LOS"]["action"] != "ADD"
    # multi-flag + weak composite escalates to SELL
    assert len(hs["G_TWO"]["flags"]) >= 2 and hs["G_TWO"]["action"] == "SELL"
    # broken trend downgrades a healthy-composite name
    assert hs["G_TT"]["composite"] >= 50 and hs["G_TT"]["action"] == "TRIM"
