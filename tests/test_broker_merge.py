"""Subprocess tests for scripts/broker_merge.py — guards, dry-run, apply.

Each test copies the script into a throwaway instance dir (config.json +
data/broker_raw.json) and points MARKET_ENGINE_HOME at it. The real book is
never read or written.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "broker_merge.py")

BASE_CFG = {"broker_account": "TESTACCT", "cash": 50000.0,
            "positions": {"AAA": 10, "CCC": 7},
            "cost_basis": {"AAA": 50.0, "CCC": 30.0},
            "etf_tickers": ["AAA", "ZZZ"], "speculative_tickers": ["CCC", "ZZZ"]}


def raw_payload(cash=50000.0, positions=None, acct="TESTACCT", equity=10000.0):
    if positions is None:
        positions = [{"symbol": "AAA", "quantity": 10, "average_buy_price": 50.0},
                     {"symbol": "CCC", "quantity": 7, "average_buy_price": 30.0}]
    return {"account_number": acct, "pulled_at": "2026-01-01T09:00:00",
            "portfolio": {"cash": cash, "equity_value": equity}, "positions": positions}


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    shutil.copy2(SCRIPT, tmp_path / "scripts" / "broker_merge.py")
    cfg_p, raw_p = tmp_path / "config.json", tmp_path / "data" / "broker_raw.json"
    def setup(cfg=None, raw=None):
        cfg_p.write_text(json.dumps(cfg if cfg is not None else BASE_CFG))
        raw_p.write_text(json.dumps(raw if raw is not None else raw_payload()))
        return tmp_path
    return setup


def run(root, *args):
    return subprocess.run([sys.executable, str(root / "scripts" / "broker_merge.py"), *args],
                          capture_output=True, text=True, cwd=str(root),
                          env=dict(os.environ, MARKET_ENGINE_HOME=str(root)))


def test_wrong_account_refused(proj):
    r = run(proj(raw=raw_payload(acct="OTHER123")))
    assert r.returncode == 2 and "ABORT" in r.stdout and "main account" in r.stdout


def test_incomplete_payload_refused(proj):
    r = run(proj(raw=raw_payload(positions=[])))
    assert r.returncode == 2 and "incomplete" in r.stdout
    r = run(proj(raw=raw_payload(cash=None)))
    assert r.returncode == 2


def test_implausible_book_refused(proj):
    r = run(proj(raw=raw_payload(cash=10.0, equity=0.0)))
    assert r.returncode == 2 and "implausible" in r.stdout


def test_nochange(proj):
    r = run(proj())
    assert r.returncode == 0 and r.stdout.strip() == "NOCHANGE"


def test_tiny_cash_drift_ignored(proj):
    r = run(proj(raw=raw_payload(cash=50000.40)))
    assert r.returncode == 0 and "NOCHANGE" in r.stdout          # < $1 rounding noise


def test_dry_run_lists_drift_no_writes(proj):
    raw = raw_payload(cash=57000.0, positions=[
        {"symbol": "AAA", "quantity": 12, "average_buy_price": 51.0},   # qty + basis drift
        {"symbol": "BBB", "quantity": 5, "average_buy_price": 20.0},    # new position
    ])                                                            # CCC closed out
    r = proj(raw=raw)
    out = run(r)
    assert out.returncode == 1
    assert "DRIFT (dry-run, no writes)" in out.stdout
    assert "cash $50,000 -> $57,000" in out.stdout
    assert "AAA shares 10 -> 12" in out.stdout
    assert "NEW position: BBB" in out.stdout
    assert "CLOSED: CCC" in out.stdout
    assert "AAA cost basis $50.00 -> $51.00" in out.stdout
    assert json.loads((r / "config.json").read_text()) == BASE_CFG   # untouched
    assert not (r / "config.json.bak").exists()


def test_apply_writes_merges_and_baks(proj):
    r = proj(raw=raw_payload(cash=57000.0, positions=[
        {"symbol": "AAA", "quantity": 12, "average_buy_price": 51.0},
        {"symbol": "BBB", "quantity": 5, "average_buy_price": 20.0}]))  # CCC gone
    res = run(r, "--apply")
    assert res.returncode == 0 and "SYNCED" in res.stdout
    cfg = json.loads((r / "config.json").read_text())
    assert cfg["cash"] == 57000.0
    assert cfg["positions"] == {"AAA": 12, "BBB": 5}
    assert cfg["cost_basis"] == {"AAA": 51.0, "BBB": 20.0}        # CCC basis pruned
    assert cfg["etf_tickers"] == ["AAA"]                          # ZZZ pruned
    assert cfg["speculative_tickers"] == []
    assert cfg["broker_synced_at"] == "2026-01-01T09:00:00"
    assert (r / "config.json.bak").exists()                       # pre-write backup


def test_fractional_shares_preserved(proj):
    r = proj(cfg={**BASE_CFG, "positions": {"AAA": 10.5, "CCC": 7}})
    out = run(r)
    assert out.returncode == 1 and "AAA shares 10.5 -> 10" in out.stdout
    # and raw fractional matches config exactly -> NOCHANGE
    r = proj(cfg={**BASE_CFG, "positions": {"AAA": 10.1234, "CCC": 7}},
             raw=raw_payload(positions=[
                       {"symbol": "AAA", "quantity": 10.1234, "average_buy_price": 50.0},
                       {"symbol": "CCC", "quantity": 7, "average_buy_price": 30.0}]))
    assert run(r).returncode == 0
