"""Unit tests: engine.rules.regime."""
from engine.rules import regime

from conftest import bench_recs


def test_bull():
    r = regime(bench_recs(bull=True, vix=18))
    assert r["label"] == "bull"
    assert r["spy"] > 0 and r["qqq"] > 0


def test_bear():
    assert regime(bench_recs(bull=False))["label"] == "bear"


def test_neutral_split():
    b = bench_recs(bull=True)
    b["QQQ"] = bench_recs(bull=False)["QQQ"]    # SPY up, QQQ down
    assert regime(b)["label"] == "neutral"


def test_vix_demotes_bull():
    r = regime(bench_recs(bull=True, vix=30))
    assert r["label"] == "neutral"
    assert any("VIX" in n for n in r["notes"])


def test_vix_does_not_promote_bear():
    assert regime(bench_recs(bull=False, vix=30))["label"] == "bear"


def test_missing_benchmarks_defaults_bear():
    r = regime({})
    assert r["label"] == "bear"                 # documented behavior: unknown = below trend
    assert len(r["explain"]) == 3
