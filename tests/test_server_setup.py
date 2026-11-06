"""Single-tenant server tests: first-run upload -> personalized instance.

No network and no real refresh subprocesses: appmod._do_refresh is patched to
a no-op, so these tests prove routing, gating and file-writing only. The
real subprocess path is covered by scripts/check_instance.py + manual E2E.
"""
import io
import json
import os

import pytest

import app as appmod
import server_core as sc

POS_CSV = b"symbol,quantity,average_buy_price\nZZTOP,10,150.00\nKRAUT,5,80.00\n"


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
    """A fresh instance dir with no book; refresh patched out; env hermetic."""
    h = tmp_path / "home"
    (h / "data").mkdir(parents=True)
    monkeypatch.setattr(appmod, "HOME", str(h))
    monkeypatch.setattr(appmod, "STATE", str(h / "data" / "state.json"))
    monkeypatch.setattr(appmod, "_do_refresh", lambda: None)
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("ME_ALLOW_UPLOADS", raising=False)
    appmod._refreshing.update({"busy": False, "error": None, "last": None})
    return str(h)


@pytest.fixture
def client(home):
    return appmod.app.test_client()


def up(c, cash="120000", file=None, **fields):
    f = file or (io.BytesIO(POS_CSV), "positions.csv")
    payload = dict(fields)
    if cash is not None:
        payload["cash"] = cash
    payload["file"] = f
    return c.post("/api/upload", data=payload, content_type="multipart/form-data")


class TestMeta:
    def test_first_run_flags(self, client):
        m = client.get("/api/meta").get_json()
        assert m["has_book"] is False and m["has_data"] is False
        assert m["uploads_enabled"] is True  # loopback default
        assert m["version"] == "2.1"

    def test_book_without_state(self, home, client):
        open(os.path.join(home, "config.json"), "w").write("{}")
        m = client.get("/api/meta").get_json()
        assert m["has_book"] is True and m["has_data"] is False

    def test_state_and_no_book(self, home, client):
        c = client  # no config.json written
        open(os.path.join(home, "data", "state.json"), "w").write(json.dumps(mk_state()))
        m = client.get("/api/meta").get_json()
        assert m["has_book"] is False and m["has_data"] is True
        assert c.get("/api/state").status_code == 200


class TestGating:
    def test_disabled_on_network_bind(self, home, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        c = appmod.app.test_client()
        assert c.get("/api/meta").get_json()["uploads_enabled"] is False
        assert up(c).status_code == 403

    def test_kill_switch_beats_loopback(self, home, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "0")
        c = appmod.app.test_client()
        assert c.get("/api/meta").get_json()["uploads_enabled"] is False
        assert up(c).status_code == 403

    def test_forced_open_on_remote_bind(self, home, monkeypatch):
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.setenv("ME_ALLOW_UPLOADS", "1")
        c = appmod.app.test_client()
        assert up(c).status_code == 202

    def test_busy_refresh_blocks_upload(self, home, monkeypatch):
        appmod._refreshing["busy"] = True
        try:
            r = up(appmod.app.test_client())
            assert r.status_code == 409 and "refresh" in r.get_json()["error"]
        finally:
            appmod._refreshing["busy"] = False


class TestSetupUpload:
    def test_positions_csv_creates_book(self, home, client):
        r = up(client)
        assert r.status_code == 202, r.get_json()
        body = r.get_json()
        assert body["ok"] and body["positions"] == 2 and not body["replaced"]
        cfg = json.load(open(os.path.join(home, "config.json")))
        assert cfg["positions"] == {"KRAUT": 5, "ZZTOP": 10}
        assert cfg["cost_basis"]["ZZTOP"] == 150.0
        assert cfg["cash"] == 120000
        assert cfg["broker_account"] == "csv-import"
        assert "AAPL" in cfg["scan_universe"]  # starter screener universe present
        assert os.path.isdir(os.path.join(home, "ledger"))

    def test_activity_csv_auto_detected(self, home, client):
        csv = (b'"Date","Instrument","Description","Trans Code","Quantity","Price","Amount"\n'
               b'"2026-01-05","ZZTOP","Buy","Buy","10","150.00","-$1,500.00"\n'
               b'"2026-01-06","ZZTOP","Dividend","CDIV","","","$3.00"\n')
        r = up(client, cash="50000", file=(io.BytesIO(csv), "activity.csv"))
        assert r.status_code == 202, r.get_json()
        cfg = json.load(open(os.path.join(home, "config.json")))
        assert cfg["positions"] == {"ZZTOP": 10}
        assert abs(cfg["cost_basis"]["ZZTOP"] - 150.0) < 0.01

    def test_config_json_upload_passes_keys(self, home, client):
        cfg = {"positions": {"ZZTOP": 3}, "cash": 40000.0,
               "watchlist": ["KRAUT"], "scan_universe": ["KRAUT"]}
        r = up(client, cash=None, file=(io.BytesIO(json.dumps(cfg).encode()), "config.json"))
        assert r.status_code == 202, r.get_json()
        saved = json.load(open(os.path.join(home, "config.json")))
        assert saved["positions"] == {"ZZTOP": 3} and saved["cash"] == 40000.0
        assert saved["scan_universe"] == ["KRAUT"]

    def test_reupload_needs_replace_and_keeps_backup(self, home, client):
        seed = json.dumps({"positions": {"OLD": 5}, "cash": 9999.0})
        open(os.path.join(home, "config.json"), "w").write(seed)
        assert up(client).status_code == 409
        # refused upload wrote nothing
        assert open(os.path.join(home, "config.json")).read() == seed
        r = up(client, replace="1")
        assert r.status_code == 202 and r.get_json()["replaced"]
        assert open(os.path.join(home, "config.json.bak")).read() == seed
        assert json.load(open(os.path.join(home, "config.json")))["positions"] != {"OLD": 5}

    def test_personalized_after_setup(self, home, client):
        """End-to-end personalization: book in -> numbers out for THIS book."""
        assert up(client).status_code == 202
        # no state yet -> 503 with a building message (warming, not broken)
        r = client.get("/api/state")
        assert r.status_code == 503 and "building" in r.get_json()["error"]
        # imagine the background refresh completed:
        open(os.path.join(home, "data", "state.json"), "w").write(json.dumps(mk_state(1800.0)))
        got = client.get("/api/state").get_json()
        assert got["portfolio"]["total"] == 1800.0
        d = client.get("/api/digest").get_json()["digest"]
        assert "Strategist" in d and "1,800" in d

    def test_state_without_book_directs_to_upload(self, home, client):
        r = client.get("/api/state")
        assert r.status_code == 503 and "no portfolio loaded" in r.get_json()["error"]


class TestFailClosed:
    def test_missing_cash_refused(self, client):
        r = up(client, cash=None)
        assert r.status_code == 422 and "cash" in r.get_json()["error"]

    def test_garbage_refused_nothing_written(self, home, client):
        r = up(client, cash="50000", file=(io.BytesIO(b"random,bytes\n1,2\n"), "junk.csv"))
        assert r.status_code == 422
        assert not os.path.exists(os.path.join(home, "config.json"))

    def test_empty_book_refused(self, home, client):
        sold = b"symbol,quantity,average_buy_price\nZZTOP,0,150.00\n"
        r = up(client, cash="50000", file=(io.BytesIO(sold), "p.csv"))
        assert r.status_code == 422
        assert not os.path.exists(os.path.join(home, "config.json"))

    def test_oversize_refused(self, client):
        r = up(client, cash="50000",
               file=(io.BytesIO(b"x" * (sc.MAX_UPLOAD + 1)), "big.csv"))
        assert r.status_code == 413

    def test_small_book_needs_explicit_confirm(self, client):
        tiny = b"symbol,quantity,average_buy_price\nZZTOP,1,150.00\n"
        assert up(client, cash="0", file=(io.BytesIO(tiny), "p.csv")).status_code == 422

    def test_bad_cash_rejected(self, client):
        assert up(client, cash="lots").status_code == 400

    def test_filename_traversal_stays_inside_instance(self, home, tmp_path, client):
        r = up(client, file=(io.BytesIO(POS_CSV), "../../../evil.csv"))
        assert r.status_code == 202
        assert os.path.exists(os.path.join(home, "config.json"))
        assert not (tmp_path / "evil.csv").exists()
        assert not (tmp_path / "config.json").exists()