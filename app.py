"""market-engine dashboard: serves the portfolio instance in $MARKET_ENGINE_HOME
(or the current working directory) at http://$DASHBOARD_HOST:$DASHBOARD_PORT.

Reads data/state.json written by scripts/refresh.py; POST /api/refresh runs
the refresh pipeline as a subprocess (never in-process: a long-lived server
caching engine modules is the classic stale-code footgun).
"""
import json, os, subprocess, sys, threading, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # bare checkouts without `pip install -e .`
from engine import paths  # noqa: E402  (resolves home from env/cwd)
from flask import Flask, jsonify, send_from_directory

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="")
HOME = paths.home()
STATE = paths.state_path()
_lock = threading.Lock()
_refreshing = {"busy": False, "error": None, "last": None}


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE))
        except Exception:
            return None
    return None


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/api/state")
def api_state():
    st = load_state()
    if st is None:
        return jsonify({"error": "no data yet — run scripts/refresh.py from your instance dir"}), 503
    st["refresh_status"] = _refreshing
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
    if _refreshing["busy"]:
        return jsonify({"ok": False, "busy": True})
    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"ok": True, "started": True})


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")   # set 0.0.0.0 to serve your LAN
    port = int(os.environ.get("DASHBOARD_PORT", "8517"))
    print(f"market-engine dashboard | instance home: {HOME} | http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
