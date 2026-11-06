# market-engine

A rules-based equity decision system: a scoring **engine**, a **dashboard**,
a **refresh pipeline**, and a deterministic **broker-sync merge**. It scores
stocks on four pillars, applies hard risk gates, maps scores to concrete
actions (BUY / ADD / HOLD / TRIM / SELL, or BUY / WATCH / PASS / AVOID for
non-holdings), keeps a self-scoring ledger of every call versus SPY, and
serves it all as a local web dashboard.

Everything is local and portfolio-agnostic: **your portfolio never leaves
your machine and never enters git.** This repo holds only code; each user's
book lives in their own *instance directory* that the code reads.

## Architecture: repo vs instance

```
market-engine/            ← this repo (code only, safe to share/clone)
    engine/                 decision core (read-only at runtime)
    app.py                  Flask server: dashboard + digest + upload endpoints
    server_core.py          server plumbing: profiles, upload guards, refresh pool
    static/index.html       dashboard UI
    scripts/refresh.py        fetch → score → snapshot → digest pipeline
    scripts/broker_merge.py   broker book → config.json merge (deterministic)
    scripts/import_broker_csv.py  Robinhood (or any) CSV export → broker book
    scripts/check_instance.py self-diagnostic: does your instance add up?
    tests/                    144 pytest tests, all synthetic fixtures
    config.example.json       template for your instance config

~/my-book/                ← your instance (MARKET_ENGINE_HOME — your data)
    config.json             positions, cost basis, cash, watchlist, params
    data/cache/             yfinance cache (6h TTL)
    data/state.json         dashboard snapshot (written by refresh)
    data/broker_raw.json    optional raw broker book (see Broker sync)
    ledger/                 decisions.jsonl + nav.json — the track record
```

The instance home is `$MARKET_ENGINE_HOME`, else the current directory. Run
the tools **from your instance directory** (or export the env var) and the
repo never accumulates anyone's data. `engine/paths.py` is the single
definition of this layout.

## Quickstart

```bash
git clone <this repo> && cd market-engine
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

mkdir ~/my-book && cd ~/my-book
cp /path/to/market-engine/config.example.json config.json
$EDITOR config.json          # broker_account, cash strategy, scan universe

# Get your book in — pick ONE:
#  (a) start the server below and upload your CSV on the first-run page
#      (no API, no credentials — this is the intended path), or
#  (b) type positions/cash into config.json by hand, or
#  (c) the same CSV path from the CLI (see Broker sync):
python /path/to/market-engine/scripts/import_broker_csv.py ~/Downloads/activity.csv --cash 8213.44
python /path/to/market-engine/scripts/broker_merge.py --apply   # CSV → config.json

python /path/to/market-engine/scripts/check_instance.py  # self-diagnostic
python /path/to/market-engine/scripts/refresh.py    # first run fetches ~all tickers, prints a digest
python /path/to/market-engine/app.py                # → http://127.0.0.1:8517
```

`refresh.py` writes `data/state.json` + a ledger snapshot; `app.py` serves
that snapshot and the UI, and its **Refresh** button re-runs the pipeline as
a subprocess (the server never imports engine modules — long-lived servers
caching stale decision code is a real bug this project has bled over).

Environment knobs: `MARKET_ENGINE_HOME` (instance dir), `DASHBOARD_HOST`
(default `127.0.0.1`; set `0.0.0.0` to serve your LAN — no auth, trusted
networks only), `DASHBOARD_PORT` (default 8517). See Run-it-locally below for
the upload endpoint and its network gate.

## Run it on your own machine (single-tenant)

One clone, one machine, one portfolio — the server is deliberately not a
multi-tenant host. Point it at any instance dir, open it in a browser:

```bash
python app.py --home ~/my-book          # default 127.0.0.1:8517
```

With no book yet, the dashboard opens as an **upload page**: drop in a
`config.json`, a Robinhood *Account activity* CSV, or a
`symbol,quantity,average_buy_price` positions CSV (plus your `cash` figure,
which no export carries). The server parses it into your `config.json` and
runs the first analysis immediately — the dashboard personalizes itself to
your holdings, cost basis and cash within a few minutes. Fail-closed: empty
or unrecognized books, oversized files and path tricks are refused without
writing anything; a rebuild keeps `config.json.bak`.

Uploads are enabled while the server is bound to loopback (the default).
Bind `--host 0.0.0.0` and they switch **off** automatically — this server has
no auth. Re-enable deliberately with `--allow-uploads` or `ME_ALLOW_UPLOADS=1`
only on a network you trust; `ME_ALLOW_UPLOADS=0` always disables.

## Getting your book in (CSV — no API, no MCP, no credentials)

`scripts/import_broker_csv.py` turns a broker CSV export into the broker
book `broker_merge.py` consumes. Nothing leaves your machine; the script
never talks to any API.

**Robinhood — Account activity report** (web: *Reports → Account activity
report*, pick a window covering your oldest open buy):

```bash
cd ~/my-book
python .../scripts/import_broker_csv.py ~/Downloads/activity_report.csv --cash 8213.44
python .../scripts/broker_merge.py           # review the drift it found
python .../scripts/broker_merge.py --apply   # writes config.json (+ .bak)
```

It replays position events (Buy/Sell incl. fractional, splits, ACAT
transfers) into current shares and weighted-average cost basis, counts cash
events (dividends, transfers, fees) and skips them. The activity CSV carries
no beginning cash balance, hence `--cash` from your account screen. ACAT
transfers arrive without cost basis — the script warns; fix `cost_basis` in
config.json if you care about P&L accuracy. `--dry-run` previews.

**Any other broker (or hand-typed):** `--format positions` with
`symbol,quantity,average_buy_price`. Header names are matched
case-insensitively with common aliases; dollar formatting (`$`, commas,
parenthesised negatives) is handled.

## Broker sync (automated, e.g. Robinhood Trading MCP)

`scripts/broker_merge.py` reconciles any broker dump into `config.json`
deterministically — an agent or script fetches, this file decides what
changes, and an LLM never hand-edits money data:

1. Write `data/broker_raw.json` in your instance:
   ```json
   {"account_number": "<id>", "pulled_at": "<ISO>",
    "portfolio": {"cash": 1234.56, "equity_value": 7890.12},
    "positions": [{"symbol": "XYZ", "quantity": 10, "average_buy_price": 45.0}]}
   ```
   (The CSV importer just writes this file — the two paths converge.)
2. `python .../scripts/broker_merge.py` — dry-run listing drift (cash,
   share count, cost-basis, NEW/CLOSED positions); `--apply` writes
   `config.json` (with `.bak`), prunes stale tickers from the ETF/spec lists
   and re-runs the refresh. `NOCHANGE` = already in sync.
3. Guards: refuses payloads whose `account_number` ≠ `broker_account` in
   your config (wrong-account protection), incomplete payloads, implausible
   book values.

For **Robinhood** the sanctioned automated fetch path is the official
Trading MCP (read-only `get_accounts` / `get_portfolio` /
`get_equity_positions`); run it from any agent/automation on a schedule and
pipe results through the merge. `config.json` then stays a live mirror of
the broker. If you'd rather not connect anything, a weekly CSV re-import
through the same merge works just as well.

## Scheduling

`refresh.py` prints a Telegram-ready digest to stdout (empty book drift =
still prints) — point any cron at it:

```
5 8 * * 1-5  cd ~/my-book && MARKET_ENGINE_HOME=$HOME/my-book ~/.venvs/market-engine/bin/python ~/Repo/market-engine/scripts/refresh.py
```

## Engine modules

| Module | Role |
|---|---|
| `engine/paths.py` | Instance layout (env-or-cwd home for all writable state). |
| `engine/data.py` | Fetch layer. Fundamentals + 2y daily prices via yfinance, JSON cache (6h TTL). Fetches whatever ticker list it is given. |
| `engine/factors.py` | Factor layer. Raw metrics (trend template, 12-1 momentum, Piotroski F-score, ROE, FCF yield, valuation…) normalized into four pillar scores (0–100): **quality, growth, value, momentum**. |
| `engine/rules.py` | Decision layer. Regime from SPY/QQQ vs 200-day + VIX; composite = weighted pillars; band → base action; **trend gate** (8-point template) and **flag gates** (F-score ≤2, cash-burn, dilution >8%, fwd P/E >150, no forward earnings, >50% off high) only *downgrade*; position caps; staged-entry notes. Every decision carries a rule-by-rule `explain` trace. |
| `engine/ledger.py` | Track record. Daily decision snapshots + NAV, scored against SPY at 30/60/90-day horizons (BUY/ADD win by beating SPY, SELL/TRIM/AVOID by lagging it). |

### Decision flow

1. **Regime** — SPY & QQQ vs 200-day (VIX>25 demotes bull one notch). Sets cash target; bear pauses new buys.
2. **Composite** = quality×w + growth×w + value×w + momentum×w (default 30/20/20/30).
3. **Bands** — ≥70 ADD, 50–69 HOLD, 42–49 borderline, 32–41 TRIM, <32 SELL.
4. **Gates (downgrade-only)** — trend template ≤2/8 forces TRIM/SELL on weak scores; red flags cap actions (1 flag ⇒ no adds under 60; 2+ flags under 45 ⇒ SELL).
5. **Held vs not** — non-holdings map to BUY / WATCH / PASS / AVOID; TRIM/SELL grades never open a position.
6. **Ledger** — every daily call is snapshotted and later scored vs SPY; replicate only what the track record supports.

## Config schema (`config.json`, stays in your instance)

Strategy parameters: `weights` (must sum to 1), `cash_targets` (per regime),
`max_position_weight`, `speculative_budget`, `benchmarks`, `baseline_date`,
`baseline_value` (what the digest measures you against).

Portfolio inputs (yours, local only): `positions` (ticker → shares),
`cost_basis`, `cash`, `broker_account` (broker-sync guard), `watchlist`,
`scan_universe` (screener pool, scored with the same rules), `etf_tickers`
(never scored — rebalance-only anchor), `speculative_tickers`,
`manual_overlay` (ticker → note shown on its card).

## Embedding the engine in your own app

```bash
pip install -e /path/to/market-engine        # editable; single copy on disk
export MARKET_ENGINE_HOME=/path/to/your/app  # config.json, data/, ledger/ live here
```

Or skip the env var entirely and pass everything explicitly —
`load_config("/any/path.json")` and `portfolio_state(recs, cfg, cash)` never
touch disk for portfolio state. The dashboard is just one host; the engine is
a plain function of `(records, config, cash)`.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q        # 117 tests, ~10s, fully offline & synthetic
```

Beyond unit coverage of factor math, bands/gates, ledger scoring, the merge
guards and the Flask API, the suite includes **fixture books with golden
outputs** (`tests/fixtures/`): three pre-made behavioural instances — bull,
bear, gate-matrix — plus a mixed win/loss ledger, each with expected engine
outputs frozen in `golden.json`. `tests/test_fixture_books.py` asserts the
rules engine reproduces those goldens (composite ±0.1, exact actions) *and*
semantic invariants (gates only downgrade, bear pauses buys, ETFs never
scored, over-cap ⇒ TRIM). Goldens change only when rules deliberately
change — the git diff of a regenerated golden **is** the rules changelog.
The CSV importer is tested end-to-end (activity replay → broker book →
merge → config) against a synthetic Robinhood-style CSV. One test validates
the production `state.json` when an instance home exists and skips
otherwise — a bare clone never touches a real book.

Regenerate goldens after an intentional rules change:

```bash
python tests/fixtures/_build.py   # rewrite fixture books + goldens
```

## Data caveats

- Prices are Yahoo Finance, ~15–20 min delayed. Not a trading system — decision support.
- Fundamentals are quarterly and point-in-time blind (restatements/backfills are taken as-is).
- Universe-relative ranks (RS percentile) depend on what else you feed it — keep the scan universe stable if you rely on ranks.
- Nothing here is financial advice. The ledger exists so you only trust the engine as far as its evidence supports.

## Security

- `config.json`, `data/`, `ledger/` are gitignored on purpose: they hold positions, cost basis, cash and decision history — keep them that way.
- No credentials, brokerage APIs or personal data anywhere in this repo.
