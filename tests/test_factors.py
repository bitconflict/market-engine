"""Unit tests: engine.factors."""
import math

from engine.factors import _valid, sma, _ret, _piecewise, pct_rank, metrics, pillar_scores

from conftest import make_rec, statements


def test_valid():
    assert _valid(1) and _valid(0.5) and _valid(-3)
    assert not _valid(None) and not _valid("x") and not _valid(float("nan"))
    assert not _valid(float("inf")) and not _valid(float("-inf"))


def test_sma():
    assert sma([1, 2, 3], 4) is None            # not enough
    assert sma([1, 2, 3, 4], 2) == 3.5          # last n
    assert sma([1, 2, float("nan"), 4], 2) == 3.0  # skips invalid before slicing


def test_ret():
    # engine semantics: a = closes[end - lookback] -> ratio over (lookback-1) bars
    c = list(range(1, 102))                     # 1..101
    assert abs(_ret(c, 100) - (101 / 2 - 1)) < 1e-9
    assert _ret([1, 2], 10) is None
    assert _ret([0, 1], 2) is None              # zero base guarded (a = closes[end-lookback])
    assert _ret([0, 1], 1) == 0.0               # degenerate 1-bar window: a == b
    assert abs(_ret(list(range(1, 52)), 50, skip=0) - (51 / 2 - 1)) < 1e-9
    assert abs(_ret(list(range(1, 52)), 49, skip=1) - (50 / 2 - 1)) < 1e-9


def test_piecewise():
    pts = [(0, 0), (10, 100)]
    assert _piecewise(5, pts) == 50
    assert _piecewise(-5, pts) == 0             # clamp low
    assert _piecewise(99, pts) == 100           # clamp high
    assert _piecewise(None, pts) is None


def test_pct_rank():
    assert pct_rank(2, [1, 2, 3]) is not None
    assert abs(pct_rank(2, [1, 2, 3]) - 1 / 3) < 1e-9
    assert pct_rank(2, [1, 2]) is None          # <3 valid
    assert pct_rank(None, [1, 2, 3]) is None


def test_metrics_uptrend_template():
    m = metrics(make_rec(drift=0.003))
    assert m["px"] > m["sma50"] > m["sma200"]
    assert all(m["tt"])                         # 7/7 on a clean uptrend
    assert m["m121"] is not None and m["m121"] > 0
    assert m["off_hi"] is not None and m["off_hi"] >= -0.25
    assert m["vs200"] > 0


def test_metrics_downtrend_template():
    m = metrics(make_rec(start=100, drift=-0.003))
    assert not any(m["tt"])                     # 0/7 in a clean downtrend
    assert m["m121"] < 0
    assert m["off_hi"] < -0.5                   # falling-knife territory


def test_metrics_piotroski():
    assert metrics(make_rec())["f_score"] == 9   # builder yields all nine checks
    weak = make_rec(info={"revenueGrowth": 0, "shortName": "W"}, healthy=False)
    assert metrics(weak)["f_score"] <= 2


def test_metrics_empty_rec_is_safe():
    m = metrics({})
    assert m.get("f_score") is None and m.get("px") is None


def test_metrics_dilution():
    m = metrics(make_rec(healthy=False))        # shares grow 10%/yr
    assert m["dilution"] is not None and m["dilution"] >= 0.08


def test_pillar_scores_range_and_signals():
    m = metrics(make_rec())
    sc = pillar_scores(m, rs_pct=0.9, m3_pct=0.9)
    for k in ("quality", "growth", "value", "momentum"):
        assert 0 <= sc[k] <= 100
    assert sc["quality"] > 80 and sc["momentum"] > 80

    wm = metrics(make_rec(start=100, drift=-0.004, info={"revenueGrowth": -0.2, "earningsGrowth": -0.5,
                                                          "debtToEquity": 300, "returnOnEquity": -0.3,
                                                          "freeCashflow": -1e9, "marketCap": 1e9,
                                                          "forwardPE": -2, "trailingPE": None,
                                                          "pegRatio": None, "enterpriseToEbitda": None,
                                                          "shortName": "W"}, healthy=False))
    ws = pillar_scores(wm, rs_pct=0.05, m3_pct=0.05)
    assert ws["momentum"] < 25 and ws["growth"] < 40


def test_pillar_scores_defaults_on_empty_metrics():
    sc = pillar_scores({})
    assert sc["quality"] == 52.0                # all-neutral fallbacks
    assert sc["growth"] == 50.0 and sc["value"] == 50.0 and sc["momentum"] == 45.0


def test_metrics_short_history_no_200d_average():
    # 50..199 valid closes: sma50 exists but sma200 is None. The vs200 line
    # guarded on s50 while dividing by s200, so ANY short-history name raised
    # TypeError and killed the whole refresh (fresh-instance 503, Nov 2026).
    for n in (60, 120):
        m = metrics(make_rec(n=n))
        assert m["sma50"] is not None and m["sma200"] is None
        assert m["vs200"] is None               # missing trend -> None, never guessed
        sc = pillar_scores(m)
        assert 0 <= sc["momentum"] <= 100       # neutral fallback path, no crash
