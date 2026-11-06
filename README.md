# market-engine

A rules-based equity decision system you run on your own machine: a scoring
**engine**, a **dashboard**, a **refresh pipeline**, and a deterministic
**broker-sync merge**. It scores stocks on four pillars (quality, growth,
value, momentum), applies hard risk gates, maps scores to concrete actions
(BUY / ADD / HOLD / TRIM / SELL, or BUY / WATCH / PASS / AVOID for
non-holdings), keeps a self-scoring ledger of every call versus SPY, and
serves it all as a local web dashboard.

Everything is local and portfolio-agnostic: **your portfolio never leaves
your machine and never enters git.** This repo holds only code; your book
lives in a separate *instance directory* that the code reads.

**Not financial advice.** This is decision-support research on delayed public
data — it does not trade, does not connect to your broker, and nothing here
is a recommendation for your personal situation.

---

## You need

- **Python 3.9+** (developed and CI-tested on 3.9; 3.10–3.12 fine) — macOS, Linux or Windows.
- **An internet connection** — the engine pulls prices + fundamentals from Yahoo Finance via `yfinance`. No API keys, no broker credentials, nothing to pay for. Quotes are 15–20 min delayed.
- ~150 MB for Python dependencies (yfinance brings pandas/numpy).

## Install (once, ~2 minutes)

```bash
git clone <this repo>
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
.venv/bin/pip install flask yfinance
```

That's the whole install — the engine needs no build step: `app.py` and the
scripts put their own folder on `sys.path`, so you run them straight from
the clone. (Advanced/embedding use only: `pip install -e .` works on pip
≥21.3; the stock Python 3.9 pip on macOS is older and can't do editable
installs — use the command above.) Then `pip install pytest` if you want
to run the test suite.

## First run: give it your portfolio (~5 minutes)

**1 · Pick where your book lives** — any folder; all your data will live
inside it. Nothing goes into the repo checkout.

```bash
mkdir ~/my-book
```

**2 · Start the server pointing at that folder:**

```bash
cd market-engine
python app.py --home ~/my-book          # serves http://127.0.0.1:8517
```

**3 · Open http://127.0.0.1:8517.** With no portfolio yet, the page is an
upload form instead of a dashboard. Feed it one of:

- **Robinhood export (recommended path).** On the Robinhood website:
  *Reports → Account activity report* — choose a window reaching back before
  your oldest still-open buy (default 1-month window is usually too short).
  Download the CSV. The importer replays buys/sells (incl. fractional),
  splits and ACAT transfers into current shares + weighted-average cost
  basis.
- **Any other broker / hand-typed:** a CSV with
  `symbol,quantity,average_buy_price` columns (common aliases like
  `Ticker,Shares,Avg Price` are recognised, `$`/comma/parenthesis formats
  are handled).
- **A market-engine `config.json`** (advanced — carries strategy params too;
  start from `config.example.json` in this repo).

**4 · Also type your current cash balance** — no broker export includes it.
Copy the figure from your account screen. It matters: the engine's cash
targets are a core output.

**5 · Press "Build my dashboard."** Nothing uploads anywhere — the file is
parsed on your own machine into `~/my-book/config.json`, then the first
analysis runs immediately (~2–4 min to fetch data on first run; the page
reloads itself when ready). Your holdings, cost-basis P&L, risk flags and
digest are now personalized to your book; a ledger starts accumulating and
graduates each call against SPY after 30/60/90 days.

Fail-closed guards: empty/garbage books, oversized files (>1 MB), implausibly
small books (confirm checkbox overrides) and path tricks are refused without
writing anything.

## Uploads and the network gate

The upload endpoint is **on while the server is bound to 127.0.0.1** (the
default) because it writes your portfolio data. Bind `--host 0.0.0.0` to
serve your LAN and uploads switch **off** automatically — the server has no
auth. Force either way with `--allow-uploads` / `ME_ALLOW_UPLOADS=1` /
`ME_ALLOW_UPLOADS=0`. Never expose this server beyond a network you trust.

## Day-2 operations

**You traded? Update the book:**
- Dashboard → the "rebuild dashboard from a new export" panel at the bottom
  (upload a fresh activity CSV, tick *replace my book* — old config is kept
  as `config.json.bak`), or
- hand-edit `~/my-book/config.json`, or
- CLI: `import_broker_csv.py` + `broker_merge.py` (see below).

Then press **↻ Refresh data** in the dashboard header (or run
`refresh.py`). Prices re-fetch, actions re-score.

**Command reference**

| Task | Command (from your instance dir, e.g. `cd ~/my-book`) |
|---|---|
| Start dashboard | `python ~/path/to/market-engine/app.py --home ~/my-book` |
| Manual refresh (prints digest) | `MARKET_ENGINE_HOME=$PWD python …/scripts/refresh.py [--force]` |
| Import broker CSV | `python …/scripts/import_broker_csv.py ~/Downloads/activity.csv --cash 8213.44` (`--dry-run` to preview, `--format positions` for the generic schema) |
| Merge into config | `python …/scripts/broker_merge.py` (dry-run) → `… broker_merge.py --apply` |
| Self-diagnostic | `python …/scripts/check_instance.py` (exit 1 = problems) |
| Full test suite | `cd market-engine && python -m pytest tests/ -q` — 137 tests, ~8 s, fully offline & synthetic |

Environment knobs: `MARKET_ENGINE_HOME` (instance dir; `--home` sets it),
`DASHBOARD_HOST` (default `127.0.0.1`; set `0.0.0.0` for LAN — no auth,
trusted networks only), `DASHBOARD_PORT` (default 8517).

**Scheduling** — `refresh.py` prints a Telegram-ready digest to stdout and
exits; point any cron at it:

```
5 8 * * 1-5  cd ~/my-book && MARKET_ENGINE_HOME=$PWD python ~/market-engine/scripts/refresh.py
```

**Reset / move machines:** your whole state is the instance folder. Copy
`~/my-book` anywhere and point a fresh clone at it. Delete it for a factory
reset. The dashboard is a view; the ledger + `config.json` are the substance.

## Architecture: repo vs instance

```
market-engine/            ← this repo (code only, safe to share/clone)
    engine/                 decision core (read-only at runtime)
    app.py                  Flask server: dashboard + digest + upload endpoints
    server_core.py          upload/setup plumbing (fail-closed guards)
    static/index.html       dashboard UI
    scripts/refresh.py        fetch → score → snapshot → digest pipeline
    scripts/broker_merge.py   broker book → config.json merge (deterministic)
    scripts/import_broker_csv.py  Robinhood (or any) CSV export → broker book
    scripts/check_instance.py self-diagnostic: does your instance add up?
    tests/                    137 pytest tests, all synthetic fixtures
    config.example.json       template for your instance config

~/my-book/                ← your instance (MARKET_ENGINE_HOME — your data)
    config.json             positions, cost basis, cash, watchlist, params
    config.json.bak         kept automatically when a rebuild replaces it
    data/cache/             yfinance cache (6h TTL)
    data/state.json         dashboard snapshot (written by refresh)
    data/broker_raw.json    optional raw broker book (see Broker sync)
    ledger/                 decisions.jsonl + nav.json — the track record
```

The instance home is `$MARKET_ENGINE_HOME`, else the current directory. Run
the tools **from your instance directory** (or pass `--home` / export the env
var) and the repo never accumulates anyone's data. `engine/paths.py` is the
single definition of this layout. The server is single-tenant by design:
one process = one machine = one book — no multi-user mode, by choice (a
correct shared server needs auth, quotas and per-tenant sandboxing; wrong
tool for a personal research box).

## Getting your book in via CLI (no browser)

`scripts/import_broker_csv.py` turns a broker CSV export into the broker
book `broker_merge.py` consumes. Nothing leaves your machine; the script
never talks to any API.

```bash
cd ~/my-book
python …/scripts/import_broker_csv.py ~/Downloads/activity_report.csv --cash 8213.44
python …/scripts/broker_merge.py           # review the drift it found
python …/scripts/broker_merge.py --apply   # writes config.json (+ .bak)
```

Cash events in the activity CSV (dividends, transfers, fees) are counted
into the drift report but skipped for positions; the CSV carries no
beginning cash balance, hence `--cash` from your account screen. ACAT
transfers arrive without cost basis — the importer warns; fix `cost_basis`
in config.json if you care about P&L accuracy.

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
2. `python …/scripts/broker_merge.py` — dry-run listing drift (cash,
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
python -m pytest tests/ -q        # 137 tests, ~8s, fully offline & synthetic
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
merge → config) against a synthetic Robinhood-style CSV, and
`tests/test_server_setup.py` covers the upload/onboarding flow and its
network gate. One test validates a production `state.json` when an instance
home exists and skips otherwise — a bare clone never touches a real book.

Regenerate goldens after an intentional rules change:

```bash
python tests/fixtures/_build.py   # rewrite fixture books + goldens
```

## Data caveats

- Prices are Yahoo Finance, ~15–20 min delayed. Not a trading system — decision support.
- Fundamentals are quarterly and point-in-time blind (restatements/backfills are taken as-is).
- Universe-relative ranks (RS percentile) depend on what else you feed it — keep the scan universe stable if you rely on ranks.
- Nothing here is financial advice. The ledger exists so you only trust the engine as far as its evidence supports.

## Security & privacy stance

- `config.json`, `data/`, `ledger/` are gitignored on purpose: they hold positions, cost basis, cash and decision history — keep them that way.
- No credentials, brokerage APIs or personal data anywhere in this repo.
- Server binds `127.0.0.1` unless told otherwise; no auth exists. Uploads (which write your config) auto-disable on any network bind. Do not expose beyond a trusted network.
- Refresh always runs as a subprocess, never in-process — a long-lived server caching stale decision code is a real bug this project has bled over.