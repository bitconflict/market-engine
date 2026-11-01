#!/usr/bin/env python3
"""Instance conformance check: does this MARKET_ENGINE_HOME add up?

Validates config.json against the engine's expectations and cross-checks the
data that surrounds it (broker_raw account guard, ledger continuity, state
file freshness). Run it after hand-editing config or when something feels
off. Exit 0 = usable (warnings allowed), 1 = something will misbehave.

    python scripts/check_instance.py            # $MARKET_ENGINE_HOME or cwd
"""
import datetime, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from engine import paths  # noqa: E402

issues = []


def ok(msg):
    print("PASS  " + msg)


def warn(msg):
    issues.append(("WARN", msg))
    print("WARN  " + msg)


def fail(msg):
    issues.append(("FAIL", msg))
    print("FAIL  " + msg)


def main():
    home = paths.home()
    print(f"instance: {home}\n")
    cfg_path = paths.config_path()
    if not os.path.exists(cfg_path):
        fail(f"no config.json at {cfg_path}")
        sys.exit(1)
    try:
        cfg = json.load(open(cfg_path))
    except Exception as e:
        fail(f"config.json is not valid JSON: {e}")
        sys.exit(1)

    # --- weights / strategy params ------------------------------------------
    w = cfg.get("weights") or {}
    if not w:
        fail("weights missing")
    else:
        s = sum(w.values())
        (ok if abs(s - 1.0) < 1e-6 else fail)(f"weights sum = {s:.4f}" + ("" if abs(s - 1) < 1e-6 else " (must be 1.0)"))
    ct = cfg.get("cash_targets") or {}
    for k in ("bull", "neutral", "bear"):
        if not isinstance(ct.get(k), (int, float)):
            fail(f"cash_targets.{k} missing/not numeric")
    if not cfg.get("benchmarks"):
        warn("benchmarks unset — engine defaults still fetch SPY/QQQ/^VIX? set explicitly: ['SPY','QQQ','^VIX']")

    # --- the book ------------------------------------------------------------
    pos = cfg.get("positions") or {}
    if not pos:
        fail("positions empty — nothing to run")
    bad = {t: q for t, q in pos.items() if not isinstance(q, (int, float)) or q <= 0}
    if bad:
        fail(f"non-positive share counts: {bad}")
    lower = [t for t in pos if t != t.upper()]
    if lower:
        fail(f"tickers must be uppercase: {lower}")
    if isinstance(cfg.get("cash"), (int, float)) and cfg["cash"] >= 0:
        ok(f"cash ${cfg['cash']:,.2f}")
    else:
        fail("cash missing or negative (broker sync needs a numeric cash)")

    held = set(pos)
    for key in ("cost_basis", "manual_overlay"):
        orphan = [t for t in (cfg.get(key) or {}) if t not in held]
        if orphan:
            warn(f"{key} has tickers not in positions (stale?): {orphan}")
    no_basis = sorted(set(pos) - set(cfg.get("cost_basis") or {}))
    if no_basis:
        warn(f"positions without cost basis (P&L shows '—'): {no_basis}")
    for key in ("etf_tickers", "speculative_tickers"):
        outside = [t for t in (cfg.get(key) or []) if t not in held]
        if outside:
            warn(f"{key} entries not held: {outside}")
    for key in ("watchlist", "scan_universe"):
        lst = cfg.get(key)
        if lst is not None and (not isinstance(lst, list) or any(t != t.upper() for t in lst)):
            fail(f"{key} must be a list of uppercase tickers")
    overlap = set(cfg.get("scan_universe") or []) & (held | set(cfg.get("watchlist") or []))
    if overlap:
        warn(f"scan_universe overlaps book/watchlist (they are excluded from candidates anyway): {sorted(overlap)}")
    if not cfg.get("broker_account"):
        warn("broker_account unset — broker_merge/CSV import guards need it "
             "(set it to your account id string)")
    if cfg.get("baseline_value") is not None and cfg.get("baseline_date"):
        ok(f"baseline {cfg['baseline_date']} ${cfg['baseline_value']:,.0f}")
    else:
        warn("baseline_date/_value unset — dashboard 'vs baseline' disabled")

    # --- surrounding data ------------------------------------------------------
    raw_p = paths.broker_raw_path()
    if os.path.exists(raw_p):
        try:
            raw = json.load(open(raw_p))
            if raw.get("account_number") != cfg.get("broker_account"):
                fail(f"data/broker_raw.json account {raw.get('account_number')!r} != "
                     f"config broker_account {cfg.get('broker_account')!r} — merge would abort")
            else:
                ok(f"broker_raw account guard matches ({raw.get('account_number')})")
        except Exception as e:
            warn(f"broker_raw.json unreadable (harmless until next sync): {e}")
    else:
        warn("no data/broker_raw.json yet (fine before first sync/import)")

    dec = paths.decisions_path()
    if os.path.exists(dec):
        dates, bad_ln = [], 0
        for i, l in enumerate(open(dec), 1):
            if not l.strip():
                continue
            try:
                dates.append(json.loads(l)["date"])
            except Exception:
                bad_ln += 1
        if bad_ln:
            fail(f"decisions.jsonl has {bad_ln} malformed lines")
        elif dates == sorted(dates):
            ok(f"ledger: {len(dates)} snapshots, {dates[0]} -> {dates[-1]}, dates ordered")
        else:
            fail("decisions.jsonl dates out of order")
        gap = max(((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
                   for a, b in zip(dates, dates[1:])), default=0)
        if gap > 7:
            warn(f"largest ledger gap: {gap} days (refresh not running, or market break)")
    else:
        warn("no ledger yet — first refresh creates it; track record starts today")

    st_p = paths.state_path()
    if os.path.exists(st_p):
        age_h = (datetime.datetime.now().timestamp() - os.path.getmtime(st_p)) / 3600
        (ok if age_h < 24 else warn)(f"state.json age {age_h:.1f}h"
                                     + ("" if age_h < 24 else " — dashboard is serving stale data, run refresh"))
    else:
        warn("no data/state.json yet — dashboard will 503 until first refresh")

    n_fail = sum(1 for k, _ in issues if k == "FAIL")
    n_warn = len(issues) - n_fail
    print(f"\n{'FAIL' if n_fail else 'PASS'}: {n_fail} failures, {n_warn} warnings")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
