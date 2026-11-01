"""Integration tests: engine.rules.portfolio_state — book-level invariants."""
import pytest

from engine.rules import portfolio_state

from conftest import bench_recs, make_cfg, make_rec


@pytest.fixture
def book():
    """FAST (huge winner, over cap), SLOW (broken downtrend), FUND (ETF),
    plus NEWB (watch, strong) and SCR1/SCR2 (screener: strong/weak)."""
    recs = bench_recs(bull=True, vix=18)
    fast = make_rec("FAST", n=300, start=5, drift=0.004)
    slow = make_rec("SLOW", n=300, start=100, drift=-0.004,
                    info={"revenueGrowth": -0.3, "earningsGrowth": -0.5, "debtToEquity": 300,
                          "returnOnEquity": -0.3, "freeCashflow": -1e9, "marketCap": 1e9,
                          "forwardPE": -2, "trailingPE": None, "pegRatio": None,
                          "enterpriseToEbitda": None, "shortName": "Slow Co"}, healthy=False)
    fund = make_rec("FUND", n=300, start=30, drift=0.0005)
    newb = make_rec("NEWB", n=300, start=10, drift=0.004)
    scr1 = make_rec("SCR1", n=300, start=10, drift=0.004)
    scr2 = make_rec("SCR2", n=300, start=100, drift=-0.004,
                    info={"revenueGrowth": -0.3, "earningsGrowth": -0.5, "debtToEquity": 300,
                          "returnOnEquity": -0.3, "freeCashflow": -1e9, "marketCap": 1e9,
                          "forwardPE": -2, "trailingPE": None, "pegRatio": None,
                          "enterpriseToEbitda": None, "shortName": "Screen Loser"}, healthy=False)
    recs.update({"FAST": fast, "SLOW": slow, "FUND": fund,
                 "NEWB": newb, "SCR1": scr1, "SCR2": scr2})
    cfg = make_cfg(positions={"FAST": 5000, "SLOW": 100, "FUND": 100},
                   watchlist=["NEWB", "FAST"],          # FAST also in watch: must be excluded
                   scan_universe=["SCR1", "SCR2", "NEWB", "FAST"],
                   etf_tickers=["FUND"], speculative_tickers=["SLOW"],
                   cash=100000.0, manual_overlay={"FAST": "hold for taxes"})
    return portfolio_state(recs, cfg, cash=100000.0)


def _total(st):
    return st["portfolio"]["total"]


def test_holdings_basics(book):
    hs = {h["ticker"]: h for h in book["holdings"]}
    assert set(hs) == {"FAST", "SLOW", "FUND"}
    tot = _total(book)
    for t, h in hs.items():
        assert abs(h["value"] - h["shares"] * h["px"]) < 1e-6
        assert abs(h["weight"] - h["value"] / tot) < 1e-9
    assert abs(book["portfolio"]["invested"] - sum(h["value"] for h in hs.values())) < 1e-6


def test_holdings_sorted_by_value_desc(book):
    vals = [h["value"] for h in book["holdings"]]
    assert vals == sorted(vals, reverse=True)


def test_etf_never_scored(book):
    fund = next(h for h in book["holdings"] if h["ticker"] == "FUND")
    assert fund["action"] == "HOLD" and fund["composite"] is None
    assert "anchor" in fund["explain"][0]["detail"].lower()


def test_over_cap_excludes_etf(book):
    oc = book["portfolio"]["over_cap"]
    assert "FAST" in oc                        # FAST is ~40% of the total book
    assert "FUND" not in oc                    # ETF sleeve exempt from the cap


def test_broken_ticker_downgraded(book):
    slow = next(h for h in book["holdings"] if h["ticker"] == "SLOW")
    assert slow["action"] in ("TRIM", "SELL")
    assert slow["composite"] < 45


def test_watchlist_excludes_holdings_and_tags(book):
    wt = {w["ticker"] for w in book["watchlist"]}
    assert "NEWB" in wt and "FAST" not in wt
    for w in book["watchlist"]:
        assert w["list"] == "watch"


def test_candidates_rules(book):
    cands = book["candidates"]
    assert len(cands) <= 12
    names = {c["ticker"] for c in cands}
    assert names.isdisjoint({"FAST", "SLOW", "FUND", "NEWB"})   # no book/watch dupes
    assert all(c.get("composite", 0) >= 45 for c in cands)
    comps = [c["composite"] for c in cands]
    assert comps == sorted(comps, reverse=True)
    for c in cands:
        assert c["list"] == "screener"
    # SCR1 is a clean uptrend strong-info name -> must surface; SCR2 must not
    assert "SCR1" in names and "SCR2" not in names


def test_cash_target_follows_regime(book):
    assert book["regime"]["label"] == "bull"
    assert book["portfolio"]["cash_target"] == 0.05
    assert abs(book["portfolio"]["cash_pct"] - 100000.0 / _total(book)) < 1e-9


def test_speculative_pct_tracked(book):
    st = {h["ticker"]: h["weight"] for h in book["holdings"]}
    assert book["speculative_tickers"] == ["SLOW"]      # top-level state key
    assert abs(book["portfolio"]["speculative_pct"] - st["SLOW"]) < 1e-9


def test_actions_sorted_severity_first(book):
    order = {"SELL": 0, "TRIM": 1, "AVOID": 2, "PASS": 3, "HOLD": 4,
             "WATCH": 5, "WAIT": 6, "BUY": 7, "ADD": 8}
    seq = [order[a["action"]] for a in book["actions"]]
    assert seq == sorted(seq)
    acts = {(a["ticker"], a["action"]) for a in book["actions"]}
    assert any(t == "SLOW" for t, _ in acts)          # broken name is actionable
    assert all(a["action"] in ("SELL", "TRIM", "BUY", "ADD") for a in book["actions"])


def test_manual_overlay_passes_through(book):
    fast = next(h for h in book["holdings"] if h["ticker"] == "FAST")
    assert fast["overlay_note"] == "hold for taxes"


def test_no_data_position_holds_quietly(book):
    cfg = make_cfg(positions={"GHOST": 10}, cash=1000.0)
    st = portfolio_state(bench_recs(), cfg, cash=1000.0)
    g = st["holdings"][0]
    assert g["action"] == "HOLD" and g["composite"] is None
    assert g["reasons"] == ["no data"] and g["confidence"] == "low"


def test_empty_book_is_safe():
    st = portfolio_state(bench_recs(bull=False), make_cfg(), cash=0.0)
    assert st["holdings"] == [] and st["actions"] == []
    assert st["portfolio"]["total"] == 0.0
