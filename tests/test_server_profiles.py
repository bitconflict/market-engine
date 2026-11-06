"""Multi-profile server tests: isolation, upload flows, digest, gating.

No network and no real refresh subprocesses: POOL.start is stubbed unless a
test only checks that warming was *requested*.
"""
import io
import json
import os

import pytest

import app as appmod
import server_core as sc


def mk_state(total=2000.0):
    return {
        "as_of": "2026-01-01 08:00",
        "regime": {"label": "bull", "spy": 0.02, "qqq": 0.01, "vix": 17.0, "explain": []},
        "holdings": [{"ticker": "AAA", "action": "HOLD", "composite": 60, "value": 1000.0,
                      "shares": 10, "px": 100.0, "weight": 0.5, "flags": []}],
        "watchlist": [], "candidates": [], "actions": [],
        "portfolio": {"invested": 1000.0, "cash": 1000.0, "total": total,
                      "cash_pct": 0.5, "cash_target": 0.05, "speculative_pct": 0.0,
                      "speculative_budget": 0.10, "over_cap": [], "max_position_weight": 0.12},
        "baseline": {"date": "2026-01-01", "value": 2000.0},
        "track": {"windows": {}}, "nav_history": [],
        "speculative_tickers": [], "etf_tickers": [],
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Fake server home: default state + two populated profiles."""
    h = tmp_path / "home"
    (h / "data").mkdir(parents=True)
    (h / "data" / "state.json").write_text(json.dumps(mk_state(9999.0)))
    for name in ("alice", "bob"):
        pdir = h / "profiles" / name
        (pdir / "data").mkdir(parents=True)
        (pdir / "config.json").write_text(json.dumps({"positions": {"ZZTOP": 5}, "cash": 500.0}))
        total = 1234.0 if name == "alice" else 5678.0
        (pdir / "data" / "state.json").write_text(json.dumps(mk_state(total)))
    monkeypatch.setattr(appmod, "HOME", str(h))
    monkeypatch.setattr(appmod, "STATE", str(h / "data" / "state.json"))
    monkeypatch.setattr(sc.POOL, "start", lambda key, hm: True)
    appmod._refreshing.update({"busy": False, "error": None, "last": None})
    return str(h)


@pytest.fixture
def client(home):
    return appmod.app.test_client()


class TestStateIsolation:
    def test_default_state_served(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200
        body = r.get_json()
        assert body["portfolio"]["total"] == 9999.0
        assert body["profile"] == "default"
        assert body["refresh_status"]["busy"] is False

    def test_profile_state_is_personalized(self, client):
        a = client.get("/api/state?profile=alice").get_json()
        b = client.get("/api/state?profile=bob").get_json()
        assert a["portfolio"]["total"] == 1234.0 and a["profile"] == "alice"
        assert b["portfolio"]["total"] == 5678.0 and b["profile"] == "bob"

    def test_profile_refresh_status_isolated(self, client):
        sc.POOL._st["alice"] = {"busy": True, "error": None, "last": None}
        try:
            a = client.get("/api/state?profile=alice").get_json()
            d = client.get("/api/state").get_json()
            assert a["refresh_status"]["busy"] is True
            assert d["refresh_status"]["busy"] is False
        finally:
            sc.POOL._st.pop("alice")

    def test_unknown_profile_404(self, client):
        assert client.get("/api/state?profile=mallory").status_code == 404

    @pytest.mark.parametrize("p", ["../secrets", "../data", "DATA", "a/b",
                                    "../../etc/passwd", "profiles", "."])
    def test_profile_path_games_are_404(self, client, p):
        assert client.get("/api/state", query_string={"profile": p}).status_code == 404

    def test_warming_profile_auto_starts_refresh(self, home, monkeypatch):
        w = os.path.join(home, "profiles", "carol", "data")
        os.makedirs(w)
        open(os.path.join(home, "profiles", "carol", "config.json"), "w").write("{}")
        started = []
        monkeypatch.setattr(sc.POOL, "start",
                            lambda key, hm: started.append(key) or True)
        c = appmod.app.test_client()
        r = c.get("/api/state?profile=carol")
        assert r.status_code == 503
        assert "warming" in r.get_json()["error"]
        assert started == ["carol"]


class TestCatalog:
    def test_profiles_listing(self, client):
        body = client.get("/api/profiles").get_json()
        assert body["default"]["has_state"] is True
        assert [p["name"] for p in body["profiles"]] == ["alice", "bob"]

    def test_digest_renders_from_current_state(self, client):
        r = client.get("/api/digest")
        assert r.status_code == 200
        assert "Strategist" in r.get_json()["digest"]

    def test_digest_per_profile(self, client):
        r = client.get("/api/digest?profile=alice")
        assert r.status_code == 200
        assert "1,234" in r.get_json()["digest"]

    def test_meta_defaults_closed(self, client):
        m = client.get("/api/meta").get_json()
        assert m["uploads_enabled"] is False


POS_CSV = b"symbol,quantity,average_buy_price\nZZTOP,10,150.00\nKRAUT,5,80.00\n"


class TestUpload:
    def _up(self, c, **data):
        f = data.pop("file", None) or (io.BytesIO(POS_CSV), "positions.csv")
        cash = data.pop("cash", "120000")
        payload = dict(data)
        if cash is not None:
            payload["cash"] = cash
        payload["file"] = f
        return c.post("/api/upload", data=payload,
                      content_type="multipart/form-data")

    def test_disabled_by_default(self, client):
        assert self._up(client, profile="zed").status_code == 403

    def test_enabled_creates_personalized_profile(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        r = self._up(c, profile="zed")
        assert r.status_code == 202, r.get_json()
        body = r.get_json()
        assert body["ok"] and body["positions"] == 2 and body["warming"]
        cfg = json.load(open(os.path.join(home, "profiles", "zed", "config.json")))
        assert cfg["positions"] == {"KRAUT": 5, "ZZTOP": 10}
        assert cfg["cost_basis"]["ZZTOP"] == 150.0
        assert cfg["cash"] == 120000
        assert cfg["broker_account"] == "csv-import"
        assert "SCHD" in cfg["scan_universe"]
        # personalization end-to-end: once state exists it renders for this profile
        sd = os.path.join(home, "profiles", "zed", "data")
        open(os.path.join(sd, "state.json"), "w").write(json.dumps(mk_state(1800.0)))
        got = c.get("/api/state?profile=zed").get_json()
        assert got["profile"] == "zed" and got["portfolio"]["total"] == 1800.0

    def test_activity_csv_auto_detected(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        csv = (b'"Date","Instrument","Description","Trans Code","Quantity","Price","Amount"\n'
               b'"2026-01-05","ZZTOP","Buy","Buy","10","150.00","-$1,500.00"\n'
               b'"2026-01-06","ZZTOP","Dividend","CDIV","","","$3.00"\n')
        r = self._up(c, profile="act1", cash="50000",
                     file=(io.BytesIO(csv), "activity.csv"))
        assert r.status_code == 202, r.get_json()
        cfg = json.load(open(os.path.join(home, "profiles", "act1", "config.json")))
        assert cfg["positions"] == {"ZZTOP": 10}
        assert abs(cfg["cost_basis"]["ZZTOP"] - 150.0) < 0.01

    def test_config_json_upload(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        cfg = {"positions": {"ZZTOP": 3}, "cash": 40000.0,
               "watchlist": ["KRAUT"], "scan_universe": ["KRAUT"]}
        r = self._up(c, profile="json1", cash=None,
                     file=(io.BytesIO(json.dumps(cfg).encode()), "config.json"))
        assert r.status_code == 202, r.get_json()
        saved = json.load(open(os.path.join(home, "profiles", "json1", "config.json")))
        assert saved["positions"] == {"ZZTOP": 3} and saved["cash"] == 40000.0
        assert saved["scan_universe"] == ["KRAUT"]

    def test_missing_cash_refused(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        r = self._up(c, profile="nocash", cash=None)
        assert r.status_code == 422 and "cash" in r.get_json()["error"]

    def test_garbage_file_refused_and_not_written(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        r = self._up(c, profile="junk", cash="50000",
                     file=(io.BytesIO(b"random,bytes\n1,2\n"), "junk.csv"))
        assert r.status_code == 422
        assert not os.path.exists(os.path.join(home, "profiles", "junk"))

    def test_empty_book_refused(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        sold = b"symbol,quantity,average_buy_price\nZZTOP,0,150.00\n"
        r = self._up(c, profile="empty", cash="50000", file=(io.BytesIO(sold), "p.csv"))
        assert r.status_code == 422

    def test_existing_name_not_touched(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        before = open(os.path.join(home, "profiles", "alice", "config.json")).read()
        r = self._up(c, profile="alice")
        assert r.status_code == 409
        assert open(os.path.join(home, "profiles", "alice", "config.json")).read() == before

    def test_evil_names_rejected(self, client, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        for name in ("../escape", "..", "ALICE", "a" * 33, "data", "a b"):
            assert self._up(client, profile=name).status_code == 400, name

    def test_oversize_refused(self, client, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        r = self._up(client, profile="big", cash="50000",
                     file=(io.BytesIO(b"x" * (sc.MAX_UPLOAD + 1)), "big.csv"))
        assert r.status_code == 413

    def test_small_book_needs_explicit_confirm(self, home, monkeypatch):
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        tiny = b"symbol,quantity,average_buy_price\nZZTOP,1,150.00\n"
        assert self._up(c, profile="tiny", cash="0",
                        file=(io.BytesIO(tiny), "p.csv")).status_code == 422
        assert self._up(c, profile="tiny2", cash="0", confirm="1",
                        file=(io.BytesIO(tiny), "p.csv")).status_code == 202
