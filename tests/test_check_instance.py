"""scripts/check_instance.py against good and broken instances (subprocess)."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "scripts", "check_instance.py")

GOOD = {
    "broker_account": "A1", "cash": 1000.0,
    "positions": {"SCR1": 10}, "cost_basis": {"SCR1": 100.0},
    "weights": {"quality": .3, "growth": .2, "value": .2, "momentum": .3},
    "cash_targets": {"bull": .05, "neutral": .08, "bear": .10},
    "max_position_weight": 0.12, "speculative_budget": 0.10,
    "benchmarks": ["SPY", "QQQ", "^VIX"], "watchlist": [], "scan_universe": [],
    "etf_tickers": [], "speculative_tickers": [], "manual_overlay": {},
    "baseline_date": "2026-01-01", "baseline_value": 50000.0,
}


def make(tmp_path, mutate=None, extra=None):
    (tmp_path / "data").mkdir(exist_ok=True)
    cfg = json.loads(json.dumps(GOOD))
    if mutate:
        mutate(cfg)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    for rel, content in (extra or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    return tmp_path


def run(root):
    return subprocess.run([sys.executable, CHECKER], capture_output=True, text=True,
                          cwd=str(root), env=dict(os.environ, MARKET_ENGINE_HOME=str(root)))


def test_good_instance_passes(tmp_path):
    r = run(make(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout and "weights sum" in r.stdout


def test_weights_must_sum_to_one(tmp_path):
    r = run(make(tmp_path, lambda c: c.update(weights={"quality": .5, "growth": .5, "value": .5})))
    assert r.returncode == 1 and "must be 1.0" in r.stdout


def test_lowercase_ticker_fails(tmp_path):
    r = run(make(tmp_path, lambda c: c.update(positions={"scr1": 10})))
    assert r.returncode == 1 and "uppercase" in r.stdout


def test_broker_account_mismatch_fails(tmp_path):
    r = run(make(tmp_path, extra={"data/broker_raw.json": {"account_number": "OTHER",
                                                            "positions": []}}))
    assert r.returncode == 1 and "merge would abort" in r.stdout


def test_ledger_order_checked(tmp_path):
    lines = json.dumps({"date": "2026-02-02"}) + "\n" + json.dumps({"date": "2026-01-01"}) + "\n"
    r = run(make(tmp_path, extra={"ledger/decisions.jsonl": lines}))
    assert r.returncode == 1 and "out of order" in r.stdout


def test_orphan_cost_basis_is_warning_only(tmp_path):
    r = run(make(tmp_path, lambda c: c["cost_basis"].update(GHOST=1.0)))
    assert r.returncode == 0 and "WARN" in r.stdout and "GHOST" in r.stdout
