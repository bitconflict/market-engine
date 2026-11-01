#!/usr/bin/env python3
"""Import a broker export into data/broker_raw.json — no API, no MCP.

The zero-credentials on-ramp: export a CSV from your broker, run this, then
run scripts/broker_merge.py (dry-run) and --apply. From there the engine,
dashboard and ledger all see your real book.

Formats
-------
Robinhood "Account Activity" CSV  (default, --format activity)
    Download: Account → (web) Reports → Account activity report → generate,
    or Settings → Privacy → Download your data. Expected header:
        "Activity Date","Process Date","Settle Date","Instrument",
        "Description","Trans Code","Quantity","Price","Amount"
    Replays position-affecting events (Buy/Sell/stock splits/ACAT transfers)
    into current shares + weighted-average cost basis. Cash events (ACH,
    dividends, fees, …) are counted and skipped — Robinhood's activity CSV
    carries no beginning balance, so pass --cash from the number shown at the
    top of your account.
    Trans codes: Buy | Sell | SPL (split delta) | ACATI/ACATO (transfer,
    arrives without cost basis — warned) | everything else = cash, skipped.

Simple positions CSV  (--format positions)
        symbol,quantity,average_buy_price
        AAPL,10,150.00
        ORCL,1.2345,100.10
    (works for any broker or a hand-typed book; average_buy_price optional)

Cash is always explicit:  --cash 12345.67   (buying power / core cash from
the broker UI). Optional --equity equities-total only feeds the merge's
implausible-book sanity check.

Examples
--------
    python scripts/import_broker_csv.py ~/Downloads/activity.csv --cash 8213.44
    python scripts/import_broker_csv.py positions.csv --format positions --cash 0
    python scripts/import_broker_csv.py activity.csv --account MYID --cash 500 --dry-run

Then:
    python scripts/broker_merge.py            # dry-run: review the drift
    python scripts/broker_merge.py --apply    # writes config.json (+ .bak)
"""
import argparse, csv, datetime, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from engine import paths  # noqa: E402

# Robinhood trans codes that change share counts
_TRADE = {"Buy": 1, "Sell": -1}
_CASH_ONLY = {"CDIV", "ACH", "WITH", "INT", "TAX", "AFEE", "DFEE", "GOLD", "SPR",
              "REC", "MISC", "GMPC", "RTP", "BCXL", "SLIP", "MTCH", "DIV", "CASH",
              "JNLM", "JNLP", "SCPA", "UNL", "LNRF", "RFTR"}


def money(s):
    """'$1,234.56' / '($2,233.58)' / '-' -> float (parentheses = negative)."""
    s = (s or "").strip().replace("$", "").replace(",", "")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def qty(s):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def norm_qty(q):
    if q is None:
        return None
    r = round(q, 4)
    return int(r) if r == int(r) else r


def parse_activity(path):
    """Replay a Robinhood activity CSV -> (positions, stats)."""
    shares, cost = {}, {}
    stats = {"trades": 0, "cash_skipped": 0, "splits": 0, "transfers": [], "warnings": []}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            sys.exit(f"error: {os.path.basename(path)} is empty")
        idx = {h.strip().lower(): i for i, h in enumerate(header)}
        col_instr = next((idx[k] for k in ("instrument", "symbol", "ticker") if k in idx), None)
        col_tc = next((idx[k] for k in ("trans code", "transaction code", "type") if k in idx), None)
        col_q = idx.get("quantity")
        col_a = idx.get("amount")
        if col_instr is None or col_tc is None or col_q is None or col_a is None:
            sys.exit("error: not a recognizable Account Activity CSV — need Instrument, "
                     "Trans Code, Quantity, Amount columns (got: " + ", ".join(header) + ")")
        for row in reader:
            if not row or not any(c.strip() for c in row):
                continue
            tc = row[col_tc].strip() if col_tc < len(row) else ""
            if not tc:
                continue  # footer/disclaimer rows
            sym = row[col_instr].strip().upper() if col_instr < len(row) else ""
            q, amt = qty(row[col_q]), money(row[col_a])
            if tc in _TRADE:
                if not sym:
                    stats["warnings"].append(f"{tc} row without symbol skipped")
                    continue
                if q is None:
                    stats["warnings"].append(f"{tc} {sym or '?'}: no quantity — skipped")
                    continue
                direction = _TRADE[tc]
                q = abs(q) * direction
                q0 = shares.get(sym, 0.0)
                q1 = q0 + q
                if q < 0:
                    if q1 < -1e-9:
                        stats["warnings"].append(
                            f"SELL {sym} exceeds held shares (report window starts after a buy?)")
                    avg = (cost.get(sym, 0.0) / q0) if q0 > 0 else 0.0
                    cost[sym] = max(cost.get(sym, 0.0) + avg * q, 0.0)  # remove avg cost of sold sh
                else:
                    # buys cost |amount| (cash-outflow rows are negative)
                    spend = abs(amt) if amt is not None else None
                    if spend is None:
                        price = money(row[idx["price"]]) if "price" in idx else None
                        if price:
                            spend = abs(q) * price
                        else:
                            spend = 0.0
                            stats["warnings"].append(f"BUY {sym}: no Amount/Price — cost basis will be wrong")
                    cost[sym] = cost.get(sym, 0.0) + spend
                shares[sym] = q1
                stats["trades"] += 1
            elif tc == "SPL":
                if not sym or q is None:
                    stats["warnings"].append("SPL row missing symbol/quantity — positions may be stale")
                    continue
                shares[sym] = shares.get(sym, 0.0) + q  # split delta, no cost change
                stats["splits"] += 1
            elif tc in ("ACATI", "ACATO"):
                if not sym or q is None:
                    continue
                shares[sym] = shares.get(sym, 0.0) + (abs(q) if tc == "ACATI" else -abs(q))
                stats["transfers"].append(f"{tc} {sym}")
                stats["warnings"].append(
                    f"ACAT {tc} {sym}: shares moved with NO cost basis — fix cost_basis in config.json if you care about P&L accuracy")
            elif tc in _CASH_ONLY:
                stats["cash_skipped"] += 1
            else:
                stats["cash_skipped"] += 1  # unknown code: assume cash event
    return shares, cost, stats


def parse_positions(path):
    shares, cost = {}, {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"error: {os.path.basename(path)} has no header row")
        cols = {c.strip().lower().replace(" ", "_").replace("-", "_"): c for c in reader.fieldnames}
        if "symbol" not in cols or "quantity" not in cols:
            sys.exit("error: positions CSV needs at least symbol,quantity "
                     "(optional average_buy_price / avg_cost / basis)")
        basis_col = next((cols[k] for k in ("average_buy_price", "avg_cost", "avg_buy_price",
                                            "basis", "cost_basis", "average_cost") if k in cols), None)
        for i, row in enumerate(reader, 2):
            sym = (row.get(cols["symbol"]) or "").strip().upper()
            q = qty(row.get(cols["quantity"]))
            if not sym:
                continue
            if q is None or q <= 0:
                continue  # zero rows = closed positions; correct to omit
            shares[sym] = q
            b = money(row.get(basis_col)) if basis_col else None
            if b and b > 0:
                cost[sym] = b * q
    return shares, cost, {"warnings": [], "transfers": []}


def main():
    ap = argparse.ArgumentParser(description="Import a broker CSV into data/broker_raw.json",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Then: scripts/broker_merge.py (dry-run) -> --apply")
    ap.add_argument("csv_file")
    ap.add_argument("--format", choices=("activity", "positions"), default="activity")
    ap.add_argument("--account", default=None,
                    help="account id; must match broker_account in config.json (guard). "
                         "Default: config's broker_account if present")
    ap.add_argument("--cash", type=float, default=None,
                    help="current cash/buying power, from the broker UI")
    ap.add_argument("--equity", type=float, default=0.0,
                    help="optional equities total (sanity-check only)")
    ap.add_argument("--out", default=None, help="output path (default: <instance>/data/broker_raw.json)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written; touch nothing")
    a = ap.parse_args()

    if not os.path.exists(a.csv_file):
        sys.exit(f"error: no such file: {a.csv_file}")
    acct = a.account
    if not acct:
        try:
            acct = json.load(open(paths.config_path())).get("broker_account") or None
        except Exception:
            acct = None
    if not acct:
        sys.exit("error: --account required (or set broker_account in config.json first)")
    if a.cash is None:
        sys.exit("error: --cash required: broker CSVs carry no beginning balance, so cash "
                 "comes from your account screen (e.g. --cash 8213.44). Use --cash 0 if genuinely none.")

    if a.format == "activity":
        shares, cost, stats = parse_activity(a.csv_file)
    else:
        shares, cost, stats = parse_positions(a.csv_file)

    positions = []
    for sym in sorted(shares):
        q = norm_qty(shares[sym])
        if q is None or q <= 0:
            continue
        p = {"symbol": sym, "quantity": q}
        abp = round(cost.get(sym, 0.0) / shares[sym], 4) if shares[sym] > 0 else 0
        if abp > 0:
            p["average_buy_price"] = abp
        positions.append(p)

    for w in stats.get("warnings", []):
        print("warning:", w)
    if stats.get("transfers"):
        print("transfers:", ", ".join(stats["transfers"]))

    if not positions:
        sys.exit("error: CSV produced zero open positions — wrong format, the report "
                 "window missed your buys, or trades net out? (nothing written)")
    if a.cash + a.equity < 1000:
        print("warning: cash + equity under $1,000 — broker_merge will refuse this as "
              "implausible unless your book really is that small")
    summary = (f"{len(positions)} open positions | {stats.get('trades', 0)} trades replayed"
               f" | {stats.get('splits', 0)} splits | {stats.get('cash_skipped', 0)} cash rows skipped")
    print(summary + f" | cash ${a.cash:,.2f}")

    raw = {"account_number": str(acct),
           "pulled_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "source": f"csv:{os.path.basename(a.csv_file)}:{a.format}",
           "portfolio": {"cash": round(a.cash, 2), "equity_value": round(a.equity, 2)},
           "positions": positions}

    if a.dry_run:
        print("(dry-run) would write:", json.dumps(raw, indent=2)[:1200])
        return
    out = a.out or paths.broker_raw_path()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(raw, open(out, "w"), indent=1)
    print(f"wrote {out}")
    print("next: python scripts/broker_merge.py          # review drift (dry-run)")
    print("      python scripts/broker_merge.py --apply  # update config.json")


if __name__ == "__main__":
    main()
