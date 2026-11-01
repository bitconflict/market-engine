"""HTTP tests for the Flask dashboard (app.py) — routes, guards, state integrity."""
import json
import os

import pytest

import app as appmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    state = {"as_of": "2026-01-01 08:00", "regime": {"label": "bull"},
             "holdings": [{"ticker": "AAA", "action": "HOLD", "composite": 60, "value": 1000.0,
                           "shares": 10, "px": 100.0, "weight": 0.5, "flags": []}],
             "watchlist": [], "candidates": [],
             "portfolio": {"invested": 1000.0, "cash": 1000.0, "total": 2000.0}}
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    monkeypatch.setattr(appmod, "STATE", str(p))
    appmod._refreshing.update({"busy": False, "error": None, "last": None})
    return appmod.app.test_client(), state


def test_index_serves_dashboard(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "html" in r.get_data(as_text=True).lower()


def test_api_state(client):
    c, st = client
    r = c.get("/api/state")
    assert r.status_code == 200
    body = r.get_json()
    assert body["as_of"] == st["as_of"]
    assert "refresh_status" in body and body["refresh_status"]["busy"] is False


def test_api_state_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "STATE", str(tmp_path / "nope.json"))
    r = appmod.app.test_client().get("/api/state")
    assert r.status_code == 503 and "no data" in r.get_json()["error"]


def test_api_state_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    monkeypatch.setattr(appmod, "STATE", str(p))
    assert appmod.app.test_client().get("/api/state").status_code == 503


def test_refresh_is_post_only(client):
    c, _ = client
    # GET must not trigger a refresh; the static catch-all answers before Flask's
    # method check, so 404 is as correct as 405 here.
    assert c.get("/api/refresh").status_code in (404, 405)


def test_refresh_starts_and_guards_busy(client, monkeypatch):
    c, _ = client
    started = {}
    monkeypatch.setattr(appmod, "_do_refresh", lambda: started.setdefault("ran", True))
    r = c.post("/api/refresh")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "started": True}
    import time
    for _ in range(50):
        if started.get("ran"):
            break
        time.sleep(0.02)
    assert started.get("ran")
    appmod._refreshing["busy"] = True
    try:
        r = c.post("/api/refresh")
        assert r.get_json() == {"ok": False, "busy": True}
    finally:
        appmod._refreshing["busy"] = False


@pytest.mark.parametrize("path", [
    "/..%2F..%2Fconfig.json", "/../../config.json", "/%2e%2e/config.json",
    "/../config.json", "/static/../../../config.json",
])
def test_path_traversal_cannot_reach_config(client, path):
    c, _ = client
    r = c.get(path)
    assert r.status_code in (400, 404)
    assert "positions" not in r.get_data(as_text=True)


# ---------- real production state.json integrity (read-only) ----------

real_state = os.path.join(ROOT, "data", "state.json")


@pytest.mark.skipif(not os.path.exists(real_state), reason="no production state yet")
def test_production_state_invariants():
    st = json.load(open(real_state))
    for k in ("as_of", "regime", "holdings", "watchlist", "candidates", "portfolio", "actions"):
        assert k in st
    hs = st["holdings"]
    for h in hs:
        if h.get("px") and h.get("shares") is not None:
            assert abs(h["value"] - h["px"] * h["shares"]) < 0.51
    assert abs(st["portfolio"]["invested"] - sum(h["value"] for h in hs)) < 1.0
    vals = [h["value"] for h in hs]
    assert vals == sorted(vals, reverse=True)
    cands = st["candidates"]
    assert len(cands) <= 12
    assert all((c.get("composite") or 0) >= 45 for c in cands)
    comps = [c["composite"] for c in cands]
    assert comps == sorted(comps, reverse=True)
    held = {h["ticker"] for h in hs} | {w["ticker"] for w in st["watchlist"]}
    assert {c["ticker"] for c in cands}.isdisjoint(held)
    p = st["portfolio"]
    assert abs(p["cash_pct"] - p["cash"] / p["total"]) < 1e-9
