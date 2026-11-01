"""End-to-end tests for scripts/import_broker_csv.py:

Robinhood activity CSV -> broker_raw.json -> broker_merge --apply -> config.json,
run as real subprocesses in throwaway instances (same shape as the real
user flow). Weighted-average cost math is asserted with hand-computed values.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSVDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "csv")
IMPORTER = os.path.join(ROOT, "scripts", "import_broker_csv.py")
MERGER = os.path.join(ROOT, "scripts", "broker_merge.py")

ACTIVITY = os.path.join(CSVDIR, "activity_robinhood.csv")
POSITIONS = os.path.join(CSVDIR, "positions_simple.csv")


@pytest.fixture
def inst(tmp_path):
    (tmp_path / "data").mkdir()
    cfg = {"broker_account": "TEST123", "cash": 0.0, "positions": {}, "cost_basis": {},
           "weights": {"quality": .3, "growth": .2, "value": .2, "momentum": .3},
           "cash_targets": {"bull": .05, "neutral": .08, "bear": .10},
           "max_position_weight": 0.12, "speculative_budget": 0.10,
           "benchmarks": ["SPY", "QQQ", "^VIX"], "watchlist": [], "scan_universe": [],
           "etf_tickers": [], "speculative_tickers": [], "manual_overlay": {}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return tmp_path


def run(root, *args):
    return subprocess.run([sys.executable, IMPORTER, *args], capture_output=True,
                          text=True, cwd=str(root),
                          env=dict(os.environ, MARKET_ENGINE_HOME=str(root)))


def merge(root, apply=False):
    cmd = [sys.executable, MERGER] + (["--apply"] if apply else [])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(root),
                          env=dict(os.environ, MARKET_ENGINE_HOME=str(root)))


# ---------- activity replay ----------

def test_activity_replay_positions_and_basis(inst):
    r = run(inst, ACTIVITY, "--account", "TEST123", "--cash", "800.55", "--equity", "2000")
    assert r.returncode == 0, r.stderr
    raw = json.load(open(inst / "data" / "broker_raw.json"))
    pos = {p["symbol"]: p for p in raw["positions"]}
    assert raw["account_number"] == "TEST123"
    assert raw["portfolio"]["cash"] == 800.55
    # AAPL: buy 10 @1500 total, sell 4 -> 6 sh, cost 1500-avg*4=900 -> $150/sh
    assert pos["AAPL"]["quantity"] == 6 and pos["AAPL"]["average_buy_price"] == 150.0
    # fractional DRIP buy: 123.45 / 1.2345 = $100/sh
    assert pos["ORCL"]["quantity"] == 1.2345 and pos["ORCL"]["average_buy_price"] == 100.0
    # split delta lands as shares, transfers as shares — both without basis
    assert pos["CAT"]["quantity"] == 3 and "average_buy_price" not in pos["CAT"]
    assert pos["DIS"]["quantity"] == 25 and "average_buy_price" not in pos["DIS"]
    # fully sold position is gone
    assert "CRWD" not in pos
    assert "5 trades replayed" in r.stdout and "1 splits" in r.stdout
    assert "2 cash rows skipped" in r.stdout
    assert "ACAT" in r.stdout  # transfer warning surfaced


def test_activity_chain_into_config(inst):
    assert run(inst, ACTIVITY, "--account", "TEST123", "--cash", "800.55",
               "--equity", "2000").returncode == 0
    m = merge(inst, apply=True)
    assert m.returncode == 0 and "SYNCED" in m.stdout, m.stdout
    cfg = json.load(open(inst / "config.json"))
    assert cfg["cash"] == 800.55
    assert cfg["positions"] == {"AAPL": 6, "ORCL": 1.2345, "CAT": 3, "DIS": 25}
    assert cfg["cost_basis"] == {"AAPL": 150.0, "ORCL": 100.0}


def test_account_defaults_from_config(inst):
    r = run(inst, ACTIVITY, "--cash", "1000")
    assert r.returncode == 0, r.stderr
    raw = json.load(open(inst / "data" / "broker_raw.json"))
    assert raw["account_number"] == "TEST123"


# ---------- positions format ----------

def test_positions_format(inst):
    r = run(inst, POSITIONS, "--format", "positions", "--account", "TEST123", "--cash", "0")
    assert r.returncode == 0, r.stderr
    raw = json.load(open(inst / "data" / "broker_raw.json"))
    pos = {p["symbol"]: p for p in raw["positions"]}
    assert pos["AAPL"] == {"symbol": "AAPL", "quantity": 10, "average_buy_price": 150.0}
    assert pos["CASHY"]["quantity"] == 50 and "average_buy_price" not in pos["CASHY"]


# ---------- failure modes ----------

def test_cash_required(inst):
    r = run(inst, ACTIVITY, "--account", "TEST123")
    assert r.returncode != 0 and "--cash required" in r.stderr + r.stdout


def test_header_validation(inst):
    bad = inst / "junk.csv"
    bad.write_text("date,description\n2024-01-01,whatever\n")
    r = run(inst, str(bad), "--account", "TEST123", "--cash", "5000")
    assert r.returncode != 0 and "Instrument" in r.stderr + r.stdout


def test_oversell_warns_and_refuses(inst):
    csv = inst / "over.csv"
    csv.write_text('"Activity Date","Instrument","Trans Code","Quantity","Amount"\n'
                   '"2024-01-01","XYZ","Buy","2","($100.00)"\n'
                   '"2024-02-01","XYZ","Sell","5","$300.00"\n')
    r = run(inst, str(csv), "--account", "TEST123", "--cash", "5000")
    # net shares <= 0 -> zero open positions -> refuse, write nothing (fail closed)
    assert r.returncode != 0
    assert "exceeds held shares" in r.stdout
    assert not (inst / "data" / "broker_raw.json").exists()


def test_zero_positions_refuses(inst):
    csv = inst / "empty.csv"
    csv.write_text('"Activity Date","Instrument","Trans Code","Quantity","Amount"\n'
                   '"2024-03-15","","ACH","","$5,000.00"\n')
    r = run(inst, str(csv), "--account", "TEST123", "--cash", "5000")
    assert r.returncode != 0 and "zero open positions" in r.stderr + r.stdout
    assert not (inst / "data" / "broker_raw.json").exists()
