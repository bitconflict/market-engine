#!/usr/bin/env python3
"""Broker sync merge: reconcile data/broker_raw.json into config.json.

Deterministic: an agent/script fetches the book from ANY broker and writes
data/broker_raw.json; this file decides what changes in config.json — the
LLM never hand-edits money data. Payload contract:

    {"account_number": "<id>",
     "pulled_at": "<ISO timestamp>",
     "portfolio":  {"cash": <num>, "equity_value": <num>, ...},
     "positions":  [{"symbol": "XYZ", "quantity": <num>,
                     "average_buy_price": <num, optional>}, ...]}

For Robinhood, the official Trading MCP (get_accounts / get_portfolio /
get_equity_positions) is the sanctioned fetch source, driven read-only.

Writes only with --apply (creates config.json.bak first). Prints drift lines,
or NOCHANGE when the config already matches the broker. Refuses raw files not
from the account configured as `broker_account` in config.json.
"""
import json, os, shutil, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # bare checkouts without `pip install -e .`
from engine import paths  # noqa: E402

CFG = paths.config_path()
RAW = paths.broker_raw_path()
APPLY = "--apply" in sys.argv

if not os.path.exists(CFG):
    print(f"ABORT: no config.json at {CFG} — run from your instance dir or set MARKET_ENGINE_HOME.")
    sys.exit(2)
cfg = json.load(open(CFG))
raw = json.load(open(RAW))

# --- guards -------------------------------------------------------------
MAIN = str(cfg.get("broker_account") or "")
acct = str(raw.get("account_number", ""))
if not MAIN:
    print("ABORT: config.json has no 'broker_account' — set it to your broker "
          "account id before merging (account guard).")
    sys.exit(2)
if acct != MAIN:
    print(f"ABORT: broker_raw account {acct!r} != configured main account "
          f"{MAIN!r} — refusing to merge (wrong-account guard).")
    sys.exit(2)
port = raw.get("portfolio") or {}
poss = raw.get("positions") or []
if not port.get("cash") or not poss:
    print("ABORT: broker_raw incomplete (no cash or empty positions).")
    sys.exit(2)

def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

drift = []

# --- cash ---------------------------------------------------------------
cash = round(float(port["cash"]), 2)
if abs(cash - float(cfg.get("cash") or 0)) >= 1.0:
    drift.append(f"cash ${cfg.get('cash'):,.0f} -> ${cash:,.0f}")
    new_cash = cash
else:
    new_cash = cfg.get("cash")

# --- positions + cost basis ---------------------------------------------
pos_shares, pos_cost = {}, {}
for p in poss:
    t = p.get("symbol")
    q = fnum(p.get("quantity")) or 0.0
    if not t or q <= 0:
        continue
    pos_shares[t] = round(q, 4) if q != int(q) else int(q)
    abp = fnum(p.get("average_buy_price"))
    if abp and abp > 0:  # may be omitted while reconciling
        pos_cost[t] = round(abp, 4)

old_shares = cfg.get("positions", {})
old_cost = cfg.get("cost_basis", {})

for t, q in sorted(pos_shares.items()):
    if t not in old_shares:
        drift.append(f"NEW position: {t} x{q} (cost ${pos_cost.get(t, 0):,.2f}) — added, defaults: non-spec; check overlay notes")
    elif old_shares[t] != q:
        drift.append(f"{t} shares {old_shares[t]} -> {q}")

for t in sorted(old_shares):
    if t not in pos_shares:
        drift.append(f"CLOSED: {t} (was {old_shares[t]} sh) — removed from positions")

for t, c in sorted(pos_cost.items()):
    tc = old_cost.get(t)
    if tc is None and t in pos_shares:
        if t in old_shares:  # known position gaining a basis
            drift.append(f"{t} cost basis set: ${c:,.2f}")
    elif tc is not None and abs(tc - c) > 0.01:
        drift.append(f"{t} cost basis ${tc:,.2f} -> ${c:,.2f}")

# --- book value sanity ----------------------------------------------------
eq = fnum(port.get("equity_value"))
book = cash + (eq or 0)
if book < 1000:
    print("ABORT: implausible book value from broker payload.")
    sys.exit(2)

# --- apply --------------------------------------------------------------
if not drift:
    print("NOCHANGE")
    sys.exit(0)

if not APPLY:
    print("DRIFT (dry-run, no writes):")
    for d in drift:
        print("  " + d)
    sys.exit(1)

shutil.copy2(CFG, CFG + ".bak")
cfg["cash"] = new_cash
cfg["positions"] = pos_shares
merged_cost = dict(old_cost)
merged_cost.update(pos_cost)
for t in list(merged_cost):
    if t not in pos_shares:
        del merged_cost[t]
cfg["cost_basis"] = merged_cost
cfg["etf_tickers"] = [t for t in cfg.get("etf_tickers", []) if t in pos_shares]
cfg["speculative_tickers"] = [t for t in cfg.get("speculative_tickers", []) if t in pos_shares]
cfg["broker_synced_at"] = raw.get("pulled_at", "")
json.dump(cfg, open(CFG, "w"), indent=2)

# refresh prices/state so the dashboard reflects the new book
try:
    env = dict(os.environ, MARKET_ENGINE_HOME=paths.home())
    subprocess.run([sys.executable, os.path.join(HERE, "refresh.py"), "--force"],
                   cwd=paths.home(), env=env, timeout=300, capture_output=True)
    refreshed = "state refreshed"
except Exception as e:
    refreshed = f"refresh failed: {str(e)[:80]}"

print("SYNCED:")
for d in drift:
    print("  " + d)
print(f"  config.json.bak saved · {refreshed}")
