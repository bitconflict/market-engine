"""market-engine dashboard server: engine + dashboard + digest, single book.

Serves the one portfolio instance in $MARKET_ENGINE_HOME (or cwd / --home).
This is a local tool: one machine, one book, no auth, no multi-tenancy.
Clone-and-run shape: open http://127.0.0.1:8517 with no portfolio loaded and
you get a first-run upload page; upload your broker export and the dashboard
becomes personalized to it within a few minutes.

Endpoints:
    GET  /             dashboard (or first-run upload page until a book exists)
    GET  /api/state    instance state + refresh status
    GET  /api/digest   strategy digest rendered from current state
    POST /api/refresh  re-run the pipeline (subprocess, never in-process)
    GET  /api/meta     capabilities: has_book / has_data / uploads_enabled
    POST /api/upload   build or rebuild config.json from config.json / CSV —
                       on while bound to loopback; a network bind disables it
                       unless ME_ALLOW_UPLOADS=1 (this server has no auth)

Refresh runs as a subprocess on purpose: a long-lived server caching engine
modules is the classic stale-code footgun (it bit us once — see ARCHITECTURE
§9). Keep it that way.
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

LOOPBACK = ("", "127.0.0.1", "localhost", "::1")


def load_state():
    """Reads the module STATE path at call time so tests (and --home) can
    redirect the instance."""
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            return None
    return None


def uploads_enabled():
    """Uploads write your instance's config.json, so the gate is where the
    server is reachable: loopback (the default) = on; a network bind = off
    unless explicitly forced. ME_ALLOW_UPLOADS=0 always wins."""
    flag = (os.environ.get("ME_ALLOW_UPLOADS") or "").strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    return _bound_host() in LOOPBACK


def _bound_host():
    return (os.environ.get("DASHBOARD_HOST") or "127.0.0.1").strip().lower()


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/api/state")
def api_state():
    st = load_state()
    if st is None:
        if os.path.exists(os.path.join(HOME, "config.json")):
            return jsonify({"error": "no data yet — analysis is building for your "
                                     "book, reload in ~1-3 min"}), 503
        return jsonify({"error": "no data yet — no portfolio loaded: upload your "
                                 "export on the start page, or drop a config.json in "
                                 "the instance dir and press Refresh"}), 503
    st = dict(st)
    st["refresh_status"] = dict(_refreshing)
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
            env.pop("PYTHONPATH", None)  # host env must not shadow deps
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


def _refresh_busy():
    return bool(_refreshing.get("busy"))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if _refresh_busy():
        return jsonify({"ok": False, "busy": True})
    # lambda (not the function object) so tests can monkeypatch _do_refresh
    threading.Thread(target=lambda: _do_refresh(), daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/digest")
def api_digest():
    st = load_state()
    if st is not None:
        try:
            # refresh.py re-imported per call: digest must never come from a
            # stale cached module copy.
            mod = sc.load_script("refresh.py")
            return jsonify({"digest": mod.digest(st)})
        except Exception:
            pass
    return jsonify({"error": "no digest yet — refresh first"}), 503


@app.route("/api/meta")
def api_meta():
    return jsonify({
        "uploads_enabled": uploads_enabled(),
        "max_upload_bytes": sc.MAX_UPLOAD,
        "has_book": os.path.exists(os.path.join(HOME, "config.json")),
        "has_data": load_state() is not None,
        "bound_host": _bound_host(),
        "version": "2.1",
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if not uploads_enabled():
        return jsonify({"error": "uploads are disabled: this server is bound to "
                                 f"{_bound_host()!r} and has no auth. Restart on "
                                 "127.0.0.1, or set ME_ALLOW_UPLOADS=1 on a network "
                                 "you trust completely"}), 403
    if _refresh_busy():
        return jsonify({"error": "a refresh is already running — try again in a minute"}), 409
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "multipart 'file' field required (config.json or CSV)"}), 400
    kind = (request.form.get("kind") or "auto").strip()
    account = (request.form.get("account") or "").strip() or None
    confirm = request.form.get("confirm") in ("1", "true", "yes", "on")
    replace = request.form.get("replace") in ("1", "true", "yes", "on")
    try:
        cash = float(request.form["cash"]) if request.form.get("cash") not in (None, "") else None
    except ValueError:
        return jsonify({"error": "cash must be a number"}), 400
    blob = f.read()
    code, payload = sc.setup_book(
        HOME, kind=kind, blob=blob, filename=f.filename or "",
        cash=cash, account=account, confirm=confirm, replace=replace)
    if code == 202:
        # Start the first (or rebuild) analysis right away — same subprocess
        # runner as the Refresh button, so the server never imports the engine.
        threading.Thread(target=lambda: _do_refresh(), daemon=True).start()
    return jsonify(payload), code


def cli_main(argv=None):
    """Entry point for `python app.py [serve] [flags]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "serve":
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="market-engine serve",
                                 description="Run the market-engine dashboard server")
    ap.add_argument("--home", default=None,
                    help="instance dir holding config.json (default: $MARKET_ENGINE_HOME or cwd)")
    ap.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8517")))
    ap.add_argument("--allow-uploads", action="store_true",
                    help="force upload endpoint ON even on a network bind (no auth — trusted network only)")
    a = ap.parse_args(argv)
    global HOME, STATE
    if a.home:
        os.environ["MARKET_ENGINE_HOME"] = os.path.abspath(a.home)
        HOME = paths.home()
        STATE = paths.state_path()
    os.environ["DASHBOARD_HOST"] = a.host  # single source of truth for the upload gate
    if a.allow_uploads:
        os.environ["ME_ALLOW_UPLOADS"] = "1"
    up = "on" if uploads_enabled() else "off"
    print(f"market-engine dashboard | instance home: {HOME} | http://{a.host}:{a.port} | uploads: {up}")
    app.run(host=a.host, port=a.port, debug=False)


if __name__ == "__main__":
    cli_main()
