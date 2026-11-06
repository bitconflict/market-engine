"""Server-side plumbing: profile resolution, uploads, refresh pool, digest.

Pure file IO + subprocess plumbing — no decision logic here (that lives in
the engine and scripts/refresh.py). Kept out of app.py so both stay readable
and unit-testable without Flask.

Layout: the $MARKET_ENGINE_HOME root is the DEFAULT profile (flat, original
1.0 layout). Uploaded portfolios become <home>/profiles/<name>/ with the same
flat layout. Refresh for a profile runs scripts/refresh.py as a subprocess
with MARKET_ENGINE_HOME=<profile dir>, so the engine sees an ordinary single
instance; nothing in engine/ learned about profiles.

Uploads are OFF unless ME_ALLOW_UPLOADS=1 (the server has no auth — trusted
LAN only). Every upload guard is fail-closed: empty books, absurd sizes, and
bad names are refused without writing anything.
"""
import datetime as _dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED = {"data", "ledger", "profiles", "static", "venv", "tests"}
MAX_UPLOAD = 1_000_000  # bytes

# Generic, public mega-cap/ETF universe so a fresh upload gets a working
# screener on first refresh. Public market trivia only — users are expected
# to edit scan_universe in their own config.json.
STARTER_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "JPM", "JNJ", "V", "PG", "UNH",
    "MA", "HD", "MRK", "XOM", "CVX", "COST", "ABBV", "LLY", "CSCO", "PEP",
    "KO", "AVGO", "TMO", "CRM", "AMD", "ORCL", "ACN", "QCOM", "TXN", "IBM",
    "CAT", "UNP", "NOW", "UBER", "NFLX", "DIS", "SPGI", "VRTX", "GE",
    "SCHD", "SCHG", "TLT",
]
STARTER_SPEC = {"UBER", "NFLX", "NOW"}
STARTER_ETF = {"SCHD", "SCHG", "TLT"}


def load_script(name):
    """Import scripts/<name> fresh (no module cache): the server must never
    hold stale engine code between requests — same lesson as the 1.0
    stale-module incident, applied to the read path."""
    path = os.path.join(HERE, "scripts", name)
    spec = importlib.util.spec_from_file_location("_me_" + name[:-3] + str(id(object())), path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script {name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and name not in _RESERVED


def list_profiles(home):
    """Profile names under <home>/profiles that carry a config.json."""
    d = os.path.join(home, "profiles")
    out = []
    if os.path.isdir(d):
        try:
            for name in sorted(os.listdir(d)):
                if valid_name(name) and os.path.isfile(os.path.join(d, name, "config.json")):
                    out.append(name)
        except OSError:
            pass
    return out


# ---------------- refresh pool ----------------

class Pool:
    """Background refresh runners for uploaded profiles.

    The DEFAULT profile keeps its own legacy runner inside app.py (byte-similar
    to 1.0); everything else goes through this pool. At most `max_conc`
    refreshes run at once so a stranger's upload flood cannot hammer yfinance
    from this box; each profile is single-flight; last digest is kept for
    the /api/digest endpoint."""

    def __init__(self, max_conc=2):
        self._sem = threading.Semaphore(max_conc)
        self._lock = threading.Lock()
        self._st = {}

    def status(self, key):
        with self._lock:
            s = self._st.get(key)
            if not s:
                return {"busy": False, "error": None, "last": None}
            return {"busy": s.get("busy", False), "error": s.get("error"), "last": s.get("last")}

    def is_busy(self, key):
        with self._lock:
            s = self._st.get(key)
            return bool(s and s.get("busy"))

    def start(self, key, home):
        """Kick a background refresh; False if one is already running."""
        with self._lock:
            s = self._st.get(key)
            if s and s.get("busy"):
                return False
            self._st[key] = {"busy": True, "error": None, "last": None}
        threading.Thread(target=self._run, args=(key, home), daemon=True).start()
        return True

    def _run(self, key, home):
        try:
            with self._sem:
                env = dict(os.environ, MARKET_ENGINE_HOME=home)
                env.pop("PYTHONPATH", None)  # host env must not shadow deps
                r = subprocess.run(
                    [sys.executable, os.path.join(HERE, "scripts", "refresh.py"), "--force"],
                    capture_output=True, text=True, timeout=900, cwd=home, env=env)
                with self._lock:
                    if r.returncode != 0:
                        self._st[key]["error"] = (r.stderr or r.stdout or "")[-400:]
                    else:
                        self._st[key]["last"] = _dt.datetime.now().isoformat()
                        self._st[key]["digest"] = (r.stdout or "").strip()
        except Exception:
            with self._lock:
                self._st[key]["error"] = traceback.format_exc()[-400:]
        finally:
            with self._lock:
                self._st[key]["busy"] = False

    def digest(self, key):
        with self._lock:
            s = self._st.get(key) or {}
        return s.get("digest")


POOL = Pool()


# ---------------- uploads ----------------

def _default_template():
    return {
        "_comment": "Generated by market-engine upload. Edit freely; the engine "
                    "re-reads this every refresh. scan_universe is a generic "
                    "starter — replace it with the names you actually follow.",
        "weights": {"quality": 0.30, "growth": 0.20, "value": 0.20, "momentum": 0.30},
        "cash_targets": {"bull": 0.05, "neutral": 0.08, "bear": 0.10},
        "max_position_weight": 0.12,
        "speculative_budget": 0.10,
        "benchmarks": ["SPY", "QQQ", "^VIX"],
        "positions": {},
        "cost_basis": {},
        "cash": 0.0,
        "broker_account": "csv-import",
        "baseline_date": None,
        "baseline_value": None,
        "etf_tickers": [],
        "speculative_tickers": [],
        "watchlist": [],
        "scan_universe": list(STARTER_UNIVERSE),
        "manual_overlay": {},
    }


def _detect_kind(filename, blob):
    low = (filename or "").lower()
    if low.endswith(".json"):
        return "config"
    head = blob[:2048].decode("utf-8", "replace").lower()
    if "trans code" in head:
        return "activity"
    if "symbol" in head and "quantity" in head:
        return "positions"
    return None


def import_upload(home, name, *, kind, blob, filename, cash=None, account=None,
                  confirm=False, equity=None):
    """Create <home>/profiles/<name>/ from an uploaded config.json / activity
    CSV / positions CSV. Returns (http_status, payload). Fail-closed: any
    refusal writes nothing. Success writes config.json then schedules a
    background refresh (warming) for the new profile."""
    if not valid_name(name):
        return 400, {"error": f"invalid portfolio name {name!r} — use lowercase letters, digits, '-' or '_' (max 32)"}
    if "/" in name or "\\" in name or ".." in name:
        return 400, {"error": "invalid portfolio name"}
    if len(blob) > MAX_UPLOAD:
        return 413, {"error": "upload larger than 1 MB"}

    pdir = os.path.realpath(os.path.join(home, "profiles", name))
    root = os.path.realpath(home)
    if not pdir.startswith(root + os.sep):
        return 400, {"error": "invalid portfolio path"}
    if os.path.exists(pdir):
        return 409, {"error": f"profile '{name}' already exists — this server creates new profiles only"}

    kind = (kind or "auto").strip().lower()
    if kind == "auto" or not kind:
        kind = _detect_kind(filename, blob)
    if kind not in ("config", "activity", "positions"):
        return 422, {"error": "unrecognized file — expected config.json, a Robinhood Account Activity CSV "
                              "(Instrument/Trans Code/Quantity/Amount columns), or a positions CSV "
                              "(symbol,quantity[,average_buy_price])"}

    notes = []
    cfg = _default_template()

    if kind == "config":
        try:
            up = json.loads(blob.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return 422, {"error": "not valid JSON"}
        if not isinstance(up, dict) or not isinstance(up.get("positions"), dict) or not up["positions"]:
            return 422, {"error": "config.json needs a non-empty 'positions' object (ticker -> shares)"}
        if up.get("cash") is None and cash is None:
            return 422, {"error": "config.json has no 'cash' — provide cash in the config or the form"}
        for k, v in up.items():
            if k in cfg or k in ("weights", "cash_targets", "positions", "cost_basis", "cash",
                                 "broker_account", "etf_tickers", "speculative_tickers",
                                 "watchlist", "scan_universe", "benchmarks", "manual_overlay",
                                 "max_position_weight", "speculative_budget"):
                cfg[k] = v
        if cash is not None:
            cfg["cash"] = cash
    else:
        if cash is None:
            return 422, {"error": "cash is required: broker CSVs carry no cash balance — "
                                  "enter the cash/buying-power figure from your account screen"}
        imp = load_script("import_broker_csv.py")
        fd, tmppath = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            try:
                if kind == "activity":
                    shares, cost, stats = imp.parse_activity(tmppath)
                else:
                    shares, cost, stats = imp.parse_positions(tmppath)
            except SystemExit as e:  # importer signals bad format via sys.exit
                return 422, {"error": str(e) or "unrecognized CSV format"}
        finally:
            os.unlink(tmppath)
        notes = list(stats.get("warnings") or [])

        positions, cost_basis = {}, {}
        for sym in sorted(shares):
            q = round(shares[sym], 4)
            q = int(q) if q == int(q) else q
            if q is None or q <= 0:
                continue
            positions[sym] = q
            abp = round(cost.get(sym, 0.0) / shares[sym], 4) if shares[sym] > 0 else 0
            if abp > 0:
                cost_basis[sym] = abp
        if not positions:
            return 422, {"error": "CSV produced zero open positions — wrong format, the report window "
                                  "missed your buys, or trades net out"}
        cfg["positions"] = positions
        cfg["cost_basis"] = cost_basis
        cfg["cash"] = round(float(cash), 2)
        held = set(positions)
        cfg["etf_tickers"] = [t for t in STARTER_ETF if t in held]
        cfg["speculative_tickers"] = [t for t in STARTER_SPEC if t in held]

    if account:
        cfg["broker_account"] = str(account)[:64]

    # plausibility (same spirit as broker_merge): a book under $1k after a
    # *successful parse* is allowed only with explicit confirm.
    book_est = float(cfg.get("cash") or 0.0) + sum(
        (cfg["positions"].get(t, 0) * p) for t, p in cfg["cost_basis"].items())
    if book_est < 1000 and not confirm:
        return 422, {"error": f"book looks implausibly small (~${book_est:,.0f} at cost). "
                              "If that's genuinely your portfolio, re-upload with confirm=1"}

    today = _dt.date.today().isoformat()
    cfg["baseline_date"] = today
    cfg["baseline_value"] = round(book_est, 2) or None

    os.makedirs(os.path.join(pdir, "data"), exist_ok=True)
    os.makedirs(os.path.join(pdir, "ledger"), exist_ok=True)
    with open(os.path.join(pdir, "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)

    started = POOL.start(name, pdir)
    return 202, {"ok": True, "profile": name,
                 "positions": len(cfg["positions"]),
                 "warming": bool(started),
                 "notes": notes[:8],
                 "hint": f"open /?profile={name} — data appears after the first refresh (~2-4 min)"}
