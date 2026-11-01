"""Ledger fixture: hand-designed outcomes with fixed dates, graded deterministically.

decisions.jsonl holds one snapshot inside the fixture SPY price series with
five decisions; the intended grading is:
  WINB  BUY  then outperformed SPY  -> win
  LOSEB BUY  then lagged SPY        -> loss
  SELLW SELL then lagged SPY        -> win (sell-calls win by lagging)
  TRIMW TRIM then lagged SPY        -> win
  HOLDX HOLD                        -> never graded
"""
import json
import os

import pytest

from conftest import ROOT  # noqa: F401
from engine import ledger, paths

FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ledgers", "mixed_track")


@pytest.fixture
def graded(monkeypatch):
    recs = json.load(open(os.path.join(FIXDIR, "recs.json")))
    monkeypatch.setattr(ledger, "DEC", os.path.join(FIXDIR, "decisions.jsonl"))
    return ledger.track_record(recs)


def test_windows_match_golden(graded):
    gold = json.load(open(os.path.join(FIXDIR, "golden_track.json")))
    for h in ("30", "60", "90"):
        assert graded["windows"][int(h)]["calls"] == gold["windows"][h]["calls"]
        assert graded["windows"][int(h)]["wins"] == gold["windows"][h]["wins"]
        assert abs(graded["windows"][int(h)]["avg_excess_vs_spy"]
                   - gold["windows"][h]["avg_excess_vs_spy"]) < 1e-6


def test_outcome_directions(graded):
    log = {(l["ticker"], l["horizon"]): l for l in graded["log"]}
    assert log[("WINB", 30)]["win"] is True
    assert log[("LOSEB", 30)]["win"] is False
    assert log[("SELLW", 30)]["win"] is True      # sold call: right if it lagged
    assert log[("TRIMW", 30)]["win"] is True
    assert all(l["horizon"] in (30, 60, 90) for l in graded["log"])
    # HOLD actions never enter the grading log
    assert all(l["ticker"] != "HOLDX" for l in graded["log"])
    # exactly 5 horizons x 4 graded calls
    assert len(graded["log"]) == 12
