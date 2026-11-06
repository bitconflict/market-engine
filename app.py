"""market-engine dashboard server: engine + dashboard + digests in one unit.

Serves the portfolio instance in $MARKET_ENGINE_HOME (or cwd). The instance
root is the DEFAULT profile; uploaded portfolios live in <home>/profiles/<name>
and are addressed with ?profile=<name> — one server, isolated per-profile data,
state, ledger and refresh runs.

Endpoints:
    GET  /                     dashboard (profile switcher in header)
    GET  /api/state?profile=   personalized state (+refresh_status)
    GET  /api/digest?profile=  strategy digest rendered from current state
    POST /api/refresh?profile= re-run the pipeline (subprocess, never in-process)
    GET  /api/profiles         profile inventory
    GET  /api/meta             server capabilities (uploads_enabled)
    POST /api/upload           create a profile from config.json / broker CSV
                               — only when ME_ALLOW_UPLOADS=1 (no auth: LAN only)

Refresh runs as a subprocess on purpose: a long-lived server caching engine
modules is the classic stale-code footgun (it bit us once — see ARCHITECTURE).
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # bare checkouts without `pip install -e .`
from engine import paths  # noqa: E402  (resolves home from env/cwd)
import server_core as sc  # noqa: E402
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="")

HOME = paths.home()
STATE = paths.state_path()
_lock = threading.Lock()
_refreshing = {"busy": False, "error": None, "last": None}


def load_state():
    """Default-profile state. Reads the module STATE path at call time so
    tests (and single-tenant users) can redirect it."""
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            return None
    return None


def _read_state(path):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return None
    return None


# ---------- profile resolution ----------

def _resolve(profile):
    """-> (key, profile_home, state_path). Raises ValueError on junk names."""
    if not profile or profile == "default":
        return "default", HOME, STATE
    if not sc.valid_name(profile):
        raise ValueError(f"unknown profile {profile!r}")
    pdir = os.path.realpath(os.path.join(HOME, "profiles", profile))
    if not pdir.startswith(os.path.realpath(HOME) + os.sep) or not os.path.isdir(pdir):
        raise ValueError(f"unknown profile {profile!r}")
    return profile, pdir, os.path.join(pdir, "data", "state.json")


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/api/state")
def api_state():
    try:
        key, pdir, spath = _resolve(request.args.get("profile"))
    except ValueError:
        return jsonify({"error": "unknown profile"}), 404
    if key == "default":
        st = load_state()
        status = _refreshing
    else:
        st = _read_state(spath)
        status = sc.POOL.status(key)
        if st is None and not sc.POOL.is_busy(key):
            # first dashboard hit after upload: start warming automatically
            sc.POOL.start(key, pdir)
            return jsonify({"error": "warming up — first analysis for this profile "
                                     "is running, reload in a few minutes"}), 503
    if st is None:
        return jsonify({"error": "no data yet — run scripts/refresh.py from your instance dir"}), 503
    st = dict(st)
    st["refresh_status"] = dict(status)
    st["profile"] = key
    return jsonify(st)


def _do_refresh():
    try:
        with _lock:
            _refreshing["busy"] = True
            _refreshing["error"] = None
            # MUST be a subprocess: in-process `import refresh` once froze
            # stale engine modules in this long-running server and silently
            # rewrote state.json with old code (dropped whole UI sections).
            env = dict(os.environ, MARKET_ENGINE_HOME=HOME)
            r = subprocess.run(
                [sys.executable, os.path.join(HERE, "scripts", "refresh.py"), "--force"],
                capture_output=True, text=True, timeout=900, cwd=HOME, env=env)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout)[-400:])
            _refreshing["last"] = __import__("datetime").datetime.now().isoformat()
    except Exception:
        _refreshing["error"] = traceback.format_exc()[-400:]
    finally:
        with _lock:
            _refreshing["busy"] = False


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    profile = request.args.get("profile")
    try:
        key, pdir, _ = _resolve(profile)
    except ValueError:
        return jsonify({"error": "unknown profile"}), 404
    if key == "default":
        if _refreshing["busy"]:
            return jsonify({"ok": False, "busy": True})
        # lambda (not the function object) so tests can monkeypatch _do_refresh
        threading.Thread(target=lambda: _do_refresh(), daemon=True).start()
        return jsonify({"ok": True, "started": True})
    if not sc.POOL.start(key, pdir):
        return jsonify({"ok": False, "busy": True})
    return jsonify({"ok": True, "started": True})


@app.route("/api/digest")
def api_digest():
    try:
        key, pdir, spath = _resolve(request.args.get("profile"))
    except ValueError:
        return jsonify({"error": "unknown profile"}), 404
    st = load_state() if key == "default" else _read_state(spath)
    if st is not None:
        try:
            # refresh.py re-imported per call: digest must never come from a
            # stale cached module copy.
            mod = sc.load_script("refresh.py")
            return jsonify({"digest": mod.digest(st)})
        except Exception:
            pass
    d = sc.POOL.digest(key)
    if d:
        return jsonify({"digest": d})
    return jsonify({"error": "no digest yet — refresh first"}), 503


@app.route("/api/profiles")
def api_profiles():
    out = {"default": {"name": "default", "has_state": os.path.exists(STATE)}, "profiles": []}
    for name in sc.list_profiles(HOME):
        out["profiles"].append({
            "name": name,
            "has_state": os.path.exists(os.path.join(HOME, "profiles", name, "data", "state.json")),
        })
    return jsonify(out)


@app.route("/api/meta")
def api_meta():
    return jsonify({
        "uploads_enabled": os.environ.get("ME_ALLOW_UPLOADS") == "1",
        "max_upload_bytes": sc.MAX_UPLOAD,
        "version": "2.0",
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    # No auth on this server: uploads are opt-in and trusted-LAN only.
    if os.environ.get("ME_ALLOW_UPLOADS") != "1":
        return jsonify({"error": "uploads are disabled on this server "
                                 "(set ME_ALLOW_UPLOADS=1 to enable — trusted network only)"}), 403
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "multipart 'file' field required (config.json or CSV)"}), 400
    name = (request.form.get("profile") or "").strip()
    kind = (request.form.get("kind") or "auto").strip()
    account = (request.form.get("account") or "").strip() or None
    confirm = request.form.get("confirm") in ("1", "true", "yes", "on")
    try:
        cash = float(request.form["cash"]) if request.form.get("cash") not in (None, "") else None
        equity = float(request.form["equity"]) if request.form.get("equity") not in (None, "") else None
    except ValueError:
        return jsonify({"error": "cash/equity must be numbers"}), 400
    blob = f.read()
    code, payload = sc.import_upload(
        HOME, name, kind=kind, blob=blob, filename=f.filename or "",
        cash=cash, account=account, confirm=confirm, equity=equity)
    return jsonify(payload), code


def cli_main(argv=None):
    """Entry point for `python app.py [serve] [flags]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="market-engine serve",
                                 description="Run the market-engine dashboard server")
    ap.add_argument("--home", default=None, help="instance dir (default: $MARKET_ENGINE_HOME or cwd)")
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8517")))
    a = ap.parse_args(argv)
    global HOME, STATE
    if a.home:
        os.environ["MARKET_ENGINE_HOME"] = os.path.abspath(a.home)
        HOME = paths.home()
        STATE = paths.state_path()
    print(f"market-engine dashboard | instance home: {HOME} | http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, debug=False)


if __name__ == "__main__":
    cli_main()
