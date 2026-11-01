"""Pipeline tests: scripts/refresh.py cost-basis overlay, sparklines, digest.

run() is exercised fully offline: config loader, fetcher and ledger are all
monkeypatched; nothing touches the network or the real book.
"""
import json
import os

import pytest

import refresh
from conftest import bench_recs, make_cfg, make_rec


# ---------- digest ----------

def _digest_state(**over):
    st = {"as_of": "2026-01-01 08:05", "baseline": {"date": "2025-10-17", "value": 100000.0},
          "portfolio": {"total": 110000.0, "cash_pct": 0.5, "cash_target": 0.05,
                        "speculative_pct": 0.04, "speculative_budget": 0.10},
          "regime": {"label": "bull"}, "actions": [], "candidates": []}
    st.update(over)
    return st


def test_digest_headline_and_actions():
    st = _digest_state(actions=[
        {"ticker": "SLOW", "action": "SELL", "composite": 28, "reasons": ["rev down"]},
        {"ticker": "FAST", "action": "TRIM", "composite": 55, "reasons": []},
    ], candidates=[{"ticker": "NEW1", "action": "BUY", "composite": 77, "px": 123.45},
                   {"ticker": "NEW2", "action": "WATCH", "composite": 63, "px": 50.0}])
    st["portfolio"]["cost_basis_total"] = 90000.0
    st["portfolio"]["unrealized_total"] = 15000.0
    st["portfolio"]["unrealized_total_pct"] = 16.7
    txt = refresh.digest(st)
    assert "🧭" in txt and "$110,000" in txt and "+10.0%" in txt
    assert "UNREALIZED".title() in txt or "Unrealized" in txt
    assert "SELL SLOW (28) — rev down" in txt
    assert "TRIM FAST (55)" in txt
    assert "Screener (outside book):" in txt and "BUY NEW1 (77)" in txt
    assert "NEW2" not in txt                       # WATCH candidates never shown
    assert "Ledger 30d" not in txt


def test_digest_stable_and_spartan():
    txt = refresh.digest(_digest_state())
    assert "Actions: none new" in txt
    assert "Unrealized" not in txt                 # no cost basis -> no line


def test_digest_over_cap_and_ledger():
    st = _digest_state()
    st["portfolio"]["over_cap"] = ["SCR1", "SCR2"]
    st["track"] = {"windows": {30: {"calls": 10, "wins": 7}}}
    txt = refresh.digest(st)
    assert "Over 12% cap: SCR1, SCR2" in txt
    assert "Ledger 30d: 7/10 calls right vs SPY" in txt


# ---------- run() end-to-end offline ----------

@pytest.fixture
def offline_pipeline(tmp_path, monkeypatch, tmp_engine):
    # point MARKET_ENGINE_HOME at a scratch proj so state.json writes land
    # there, while ledger constants stay on the tmp engine home — proving
    # ledger/NAV writes never follow the state-file location
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True)
    monkeypatch.setenv("MARKET_ENGINE_HOME", str(proj))

    cfg = make_cfg(positions={"FAST": 100, "SLOW": 50},
                   cost_basis={"FAST": 5.0, "SLOW": 90.0},
                   watchlist=["NEWB"], scan_universe=[], cash=100000.0,
                   baseline_date="2025-10-17", baseline_value=100000.0)
    fast = make_rec("FAST", n=300, start=5, drift=0.004)
    slow = make_rec("SLOW", n=300, start=100, drift=-0.004, healthy=False,
                    info={"revenueGrowth": -0.3, "earningsGrowth": -0.5, "debtToEquity": 300,
                          "returnOnEquity": -0.3, "freeCashflow": -1e9, "marketCap": 1e9,
                          "forwardPE": -2, "trailingPE": None, "pegRatio": None,
                          "enterpriseToEbitda": None, "shortName": "S"})
    newb = make_rec("NEWB", n=300, start=10, drift=0.004)
    recs = {**bench_recs(bull=True), "FAST": fast, "SLOW": slow, "NEWB": newb}

    from engine import data as ed, rules as er
    monkeypatch.setattr(er, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(ed, "fetch_many", lambda tks, force=False: recs)
    return proj, recs, tmp_engine


def test_run_produces_state_with_all_layers(offline_pipeline):
    tmp_path, recs, engine_tmp = offline_pipeline
    state, _ = refresh.run()

    # persisted + baseline + cost overlay
    out = tmp_path / "data" / "state.json"
    assert out.exists()
    assert state["baseline"]["value"] == 100000.0
    hs = {h["ticker"]: h for h in state["holdings"]}
    fast_px = hs["FAST"]["px"]
    assert hs["FAST"]["cost_per_share"] == 5.0
    assert abs(hs["FAST"]["cost_total"] - 500.0) < 1e-9
    assert abs(hs["FAST"]["unrealized"] - (fast_px * 100 - 500.0)) < 0.01
    assert hs["FAST"]["unrealized_pct"] == round(hs["FAST"]["unrealized"] / 500.0 * 100, 1)
    p = state["portfolio"]
    assert p["cost_basis_total"] == 500.0 + 4500.0
    assert abs(p["unrealized_total"] - (p["invested"] - 5000.0)) < 0.01

    # sparklines end at the quoted price
    for h in state["holdings"]:
        assert h["spark"] and abs(h["spark"][-1] - h["px"]) < 1e-6

    # ledger snapshot + NAV went to the TMP engine home, never the real book
    assert os.path.exists(engine_tmp["DEC"]) and os.path.exists(engine_tmp["NAV"])
    assert not os.path.exists(os.path.join(tmp_path, "ledger", "nav.json"))
    assert state["nav_history"] == []

    # watchlist graded, digest renders
    assert {w["ticker"] for w in state["watchlist"]} == {"NEWB"}
    txt = refresh.digest(state)
    assert "Book $" in txt and "regime: BULL" in txt
    assert "Unrealized" in txt
