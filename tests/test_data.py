"""Unit tests: engine.data helpers (no network — cache paths and pure funcs only)."""
import json
import math
import sys

import pandas as pd
import pytest

from engine import data


def test_num():
    assert data._num("1.5") == 1.5 and data._num(2) == 2
    assert data._num(None) is None and data._num("x") is None
    assert data._num(float("nan")) is None and data._num(float("inf")) is None


def test_stmt_series_chronological_with_missing_rows():
    df = pd.DataFrame(index=["Total Revenue"],
                      columns=pd.to_datetime(["2024-12-31", "2023-12-31"]),
                      data=[[200.0, 100.0]])
    out = data._stmt_series(df, ["Total Revenue", "Net Income"])
    assert [r["date"] for r in out] == ["2023-12-31", "2024-12-31"]   # oldest first
    assert [r["Total Revenue"] for r in out] == [100.0, 200.0]
    assert all(r["Net Income"] is None for r in out)            # missing row -> None


def test_stmt_series_empty():
    assert data._stmt_series(None, ["x"]) == []
    assert data._stmt_series(pd.DataFrame(), ["x"]) == []


def test_fetch_one_serves_from_cache_without_network(tmp_engine, monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)          # any import attempt explodes
    cached = {"ticker": "CCH", "ok": True, "fetched": 1, "info": {}, "prices": {}}
    with open(f'{tmp_engine["cache"]}/CCH.json', "w") as f:
        json.dump(cached, f)
    assert data.fetch_one("CCH") == cached                       # fresh cache hit, no import
    knife = f'{tmp_engine["cache"]}/IDX_VIX.json'
    with open(knife, "w") as f:
        json.dump({"ticker": "^VIX", "ok": True}, f)
    assert data.fetch_one("^VIX")["ok"] is True                  # ^ sanitized in filename


def test_fetch_one_stale_cache_falls_through_to_network(tmp_engine, monkeypatch):
    import os, time as _t
    p = f'{tmp_engine["cache"]}/OLD.json'
    with open(p, "w") as f:
        json.dump({"ticker": "OLD"}, f)
    old = _t.time() - data.CACHE_TTL - 10
    os.utime(p, (old, old))
    with pytest.raises(Exception):                                # yfinance poisoned -> raises
        monkeypatch.setitem(sys.modules, "yfinance", None)
        data.fetch_one("OLD")


def test_fetch_many_collects_errors(tmp_engine, monkeypatch):
    def fake(tk, force=False):
        if tk == "BAD":
            raise RuntimeError("boom")
        return {"ticker": tk, "ok": True}
    monkeypatch.setattr(data, "fetch_one", fake)
    out = data.fetch_many(["A", "BAD", "C"])
    assert out["A"]["ok"] and out["C"]["ok"]
    assert out["BAD"]["ok"] is False and "boom" in out["BAD"]["err"]
