"""Unit tests: engine.ledger — daily snapshots and vs-SPY scoring."""
import json

from engine import ledger

from conftest import dates, make_rec


def _state():
    return {"regime": {"label": "bull"},
            "holdings": [{"ticker": "AAA", "action": "TRIM", "composite": 40, "px": 10.0,
                          "shares": 5, "value": 50.0},
                         {"ticker": "CCC", "action": "HOLD", "composite": 55, "px": 4.0,
                          "shares": 10, "value": 40.0}],
            "watchlist": [{"ticker": "BBB", "action": "BUY", "composite": 75, "px": 20.0,
                           "shares": 0, "value": 0.0},
                          {"ticker": "DDD", "action": None}],
            "portfolio": {"total": 100.0, "invested": 50.0, "cash": 50.0}}


def test_snapshot_writes_and_dedupes_same_day(tmp_engine):
    ledger.snapshot(_state())
    ledger.snapshot(_state())                  # same calendar day -> still one row
    rows = [json.loads(l) for l in open(tmp_engine["DEC"])]
    assert len(rows) == 1
    decs = rows[0]["decisions"]
    assert decs["AAA"]["action"] == "TRIM" and decs["BBB"]["action"] == "BUY"
    assert "CCC" in decs                        # HOLDs are recorded (they are decisions too)
    assert "DDD" not in decs                    # action=None entries are skipped
    nav = json.load(open(tmp_engine["NAV"]))
    assert len(nav) == 1 and nav[0]["total"] == 100.0


def test_snapshot_includes_screener_candidates(tmp_engine):
    """The 2026-11-09 bug class: screener calls existed in state but never
    reached the ledger, so track_record() could never score them."""
    st = _state()
    st["candidates"] = [{"ticker": "EEE", "action": "BUY", "composite": 71, "px": 100.0},
                        {"ticker": "FFF", "action": "AVOID", "composite": 20, "px": 10.0},
                        {"ticker": "BBB", "action": "WATCH", "composite": 50, "px": 20.0}]
    ledger.snapshot(st)
    decs = [json.loads(l) for l in open(tmp_engine["DEC"])][-1]["decisions"]
    assert decs["EEE"]["action"] == "BUY" and decs["FFF"]["action"] == "AVOID"
    assert decs["BBB"]["action"] == "BUY"       # listed twice -> first (watchlist) wins,
    assert len([k for k in decs if decs[k]["action"]]) == 5   # never overwritten


def test_track_record_scores_vs_spy(tmp_engine):
    dts = dates(400)
    d0 = dts[10]
    n = len(dts)
    def closes(path):                          # multiplicative path over the window
        out, v = [], 1.0
        for i in range(n):
            if i >= 10:
                v *= (1 + path)
            out.append(round(v, 6))
        return out
    spy = {"ticker": "SPY", "ok": True, "info": {}, "income": [], "balance": [], "cashflow": [],
           "prices": {"dates": dts, "close": closes(0.0005), "volume": [1] * n}}
    def tkr(sym, path):
        return {"ticker": sym, "ok": True, "info": {}, "income": [], "balance": [], "cashflow": [],
                "prices": {"dates": dts, "close": closes(path), "volume": [1] * n}}
    rows = [{"date": d0, "regime": "bull", "decisions": {
        "WINNER": {"action": "BUY"}, "LOSER": {"action": "BUY"}, "SXR": {"action": "SELL"}}}]
    with open(tmp_engine["DEC"], "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    recs = {"SPY": spy, "WINNER": tkr("WINNER", 0.006), "LOSER": tkr("LOSER", -0.003),
            "SXR": tkr("SXR", 0.0)}
    tr = ledger.track_record(recs)
    w30 = tr["windows"][30]
    assert w30["calls"] == 3 and w30["wins"] == 2      # BUY winner + SELL laggard win
    assert w30["hit_rate"] == round(2 / 3, 3)
    assert tr["windows"][60]["calls"] == 3             # same calls mature at 60d too
    assert tr["windows"][90]["calls"] == 3
    assert len(tr["log"]) == 9
    loser = [e for e in tr["log"] if e["ticker"] == "LOSER" and e["horizon"] == 30][0]
    assert loser["win"] is False and loser["excess"] < 0


def test_track_record_without_ledger(tmp_engine):
    tr = ledger.track_record({"SPY": {"prices": {}}})
    assert tr == {"windows": {}, "log": []}
