"""Shared fixtures: synthetic market data builders + engine-state isolation.

Every test that could write (ledger snapshots, price cache, state.json) is
redirected to tmp dirs — the suite never touches the real book, ledger, or
dashboard state.
"""
import datetime as _dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from engine import data as edata, ledger as eled  # noqa: E402


# ---------- synthetic data builders ----------

def dates(n, start="2024-01-01"):
    d0 = _dt.date.fromisoformat(start)
    return [(d0 + _dt.timedelta(days=i)).isoformat() for i in range(n)]


STRONG_INFO = {
    "revenueGrowth": 0.25, "earningsGrowth": 0.30, "trailingPE": 22.0, "forwardPE": 20.0,
    "pegRatio": 1.2, "enterpriseToEbitda": 14.0, "returnOnEquity": 0.20, "returnOnAssets": 0.09,
    "debtToEquity": 40.0, "freeCashflow": 6.0e9, "marketCap": 1.0e11, "profitMargins": 0.15,
    "grossMargins": 0.60, "forwardEps": 5.0, "trailingEps": 4.5, "sector": "Tech",
    "shortName": "Strong Co", "heldPercentInstitutions": 0.8, "currentPrice": None,
}
WEAK_INFO = {
    "revenueGrowth": -0.15, "earningsGrowth": -0.40, "trailingPE": None, "forwardPE": -3.0,
    "pegRatio": None, "enterpriseToEbitda": None, "returnOnEquity": -0.2, "returnOnAssets": -0.1,
    "debtToEquity": 220.0, "freeCashflow": -2.0e9, "marketCap": 3.0e9, "profitMargins": -0.3,
    "grossMargins": 0.2, "forwardEps": -1.2, "trailingEps": -2.0, "sector": "Retail",
    "shortName": "Weak Co", "heldPercentInstitutions": 0.3, "currentPrice": None,
}


def statements(healthy=True):
    """2+ years of annual statements; healthy=True -> Piotroski 9."""
    s = 1 if healthy else -1
    inc = [{"date": f"20{i}Y", "Total Revenue": 100.0 + (10 if healthy else -10) * i,
            "Gross Profit": (60.0 if healthy else 20.0) + (12 * i if healthy else -5 * i),
            "Net Income": (10.0 if healthy else -10.0) * (1 + (0.2 * i if healthy else 0)),
            "Research And Development": 10.0, "Operating Income": 15.0} for i in range(3)]
    bs = [{"date": f"20{i}Y", "Total Assets": 200.0, "Long Term Debt": 60.0 - (5 * i if healthy else -5 * i),
           "Current Assets": 80.0 + (5 * i if healthy else -5 * i), "Current Liabilities": 40.0,
           "Ordinary Shares Number": 100.0 if healthy else 100.0 * (1.10 ** i),
           "Share Issued": 100.0 if healthy else 100.0 * (1.10 ** i), "Total Debt": 60.0} for i in range(3)]
    cf = [{"date": f"20{i}Y", "Operating Cash Flow": (15.0 if healthy else -15.0),
           "Free Cash Flow": (10.0 if healthy else -10.0), "Capital Expenditure": -5.0} for i in range(3)]
    return inc, bs, cf


def make_rec(tkr="TEST", n=300, start=10.0, drift=0.003, info=None, healthy=True,
             dts=None, current_price=None):
    closes = [round(start * ((1 + drift) ** i), 4) for i in range(n)]
    dts = dts or dates(n)
    info = dict(STRONG_INFO if info is None else info)
    if current_price is not None:
        info["currentPrice"] = current_price
    else:
        info.pop("currentPrice", None)
    inc, bs, cf = statements(healthy)
    return {"ticker": tkr, "ok": True,
            "prices": {"dates": dts, "close": closes, "volume": [1e6] * n},
            "info": info, "income": inc, "balance": bs, "cashflow": cf,
            "_closes": closes}


def bench_recs(bull=True, vix=18.0):
    d = 0.002 if bull else -0.002
    vrec = {"ticker": "^VIX", "ok": True,
            "prices": {"dates": dates(30), "close": [vix] * 30, "volume": [1] * 30}, "info": {}}
    return {"SPY": make_rec("SPY", start=100, drift=d, info=WEAK_INFO, healthy=True),
            "QQQ": make_rec("QQQ", start=100, drift=d, info=WEAK_INFO, healthy=True),
            "^VIX": vrec}


def make_cfg(**over):
    cfg = {"weights": {"quality": 0.3, "growth": 0.2, "value": 0.2, "momentum": 0.3},
           "cash_targets": {"bull": 0.05, "neutral": 0.08, "bear": 0.10},
           "max_position_weight": 0.12, "speculative_budget": 0.10,
           "positions": {}, "cost_basis": {}, "cash": 100000.0,
           "watchlist": [], "scan_universe": [], "etf_tickers": [],
           "speculative_tickers": [], "benchmarks": ["SPY", "QQQ", "^VIX"],
           "manual_overlay": {}}
    cfg.update(over)
    return cfg


def decide_m(**over):
    """Minimal metrics dict for rules.decide() with neutral defaults."""
    m = {"tt_passes": 7, "f_score": None, "rev_growth": None, "m121": None, "m6": None,
         "dilution": None, "fwd_pe": None, "off_hi": None, "px": 100.0,
         "name": "T", "sector": "S", "tt8": [True] * 7 + [True], "rs_pct": 0.8}
    m.update(over)
    return m


def flat_sc(x):
    return {"quality": x, "growth": x, "value": x, "momentum": x}


# ---------- isolation ----------

@pytest.fixture
def tmp_engine(tmp_path, monkeypatch):
    """Redirect engine module-level paths (price cache, ledger files) to tmp."""
    cache = tmp_path / "cache"
    cache.mkdir()
    led = tmp_path / "ledger"
    led.mkdir()
    monkeypatch.setattr(edata, "CACHE", str(cache))
    monkeypatch.setattr(eled, "LED", str(led))
    monkeypatch.setattr(eled, "DEC", str(led / "decisions.jsonl"))
    monkeypatch.setattr(eled, "NAV", str(led / "nav.json"))
    return {"cache": str(cache), "led": str(led),
            "DEC": str(led / "decisions.jsonl"), "NAV": str(led / "nav.json")}


@pytest.fixture
def fixture_state(tmp_path):
    """Minimal state.json stand-in for Flask API tests."""
    st = {"as_of": "2026-01-01 08:00", "regime": {"label": "bull"},
          "holdings": [{"ticker": "AAA", "action": "HOLD", "composite": 60, "value": 1000.0,
                        "shares": 10, "px": 100.0, "weight": 0.5, "flags": []}],
          "watchlist": [], "candidates": [],
          "portfolio": {"invested": 1000.0, "cash": 1000.0, "total": 2000.0}}
    p = tmp_path / "state.json"
    p.write_text(json.dumps(st))
    return str(p), st
