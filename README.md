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
    app.py                  Flask dashboard
    static/index.html       dashboard UI
    scripts/refresh.py      fetch → score → snapshot → digest pipeline
    scripts/broker_merge.py broker book → config.json merge (deterministic)
    tests/                  90 pytest tests, all synthetic fixtures
    config.example.json     template for your instance config

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
$EDITOR config.json          # your positions, cash, watchlist, scan universe

python /path/to/market-engine/scripts/refresh.py    # first run fetches ~all tickers, prints a digest
python /path/to/market-engine/app.py                # → http://127.0.0.1:8517
```

`refresh.py` writes `data/state.json` + a ledger snapshot; `app.py` serves
that snapshot and the UI, and its **Refresh** button re-runs the pipeline as
a subprocess (the server never imports engine modules — long-lived servers
caching stale decision code is a real bug this project has bled over).

Environment knobs: `MARKET_ENGINE_HOME` (instance dir), `DASHBOARD_HOST`
(default `127.0.0.1`; set `0.0.0.0` to serve your LAN — no auth, trusted
networks only), `DASHBOARD_PORT` (default 8517).

## Broker sync (bring your own book)

`scripts/broker_merge.py` reconciles a broker dump into `config.json`
deterministically — an agent or script fetches, this file decides what
changes, and an LLM never hand-edits money data:

1. Write `data/broker_raw.json` in your instance:
   ```json
   {"account_number": "<id>", "pulled_at": "<ISO>",
    "portfolio": {"cash": 1234.56, "equity_value": 7890.12},
    "positions": [{"symbol": "XYZ", "quantity": 10, "average_buy_price": 45.0}]}
   ```
2. `python .../scripts/broker_merge.py` — dry-run listing drift (cash,
   share count, cost-basis, NEW/CLOSED positions); `--apply` writes
   `config.json` (with `.bak`), prunes stale tickers from the ETF/spec lists
   and re-runs the refresh. `NOCHANGE` = already in sync.
3. Guards: refuses payloads whose `account_number` ≠ `broker_account` in
   your config (wrong-account protection), incomplete payloads, implausible
   book values.

For **Robinhood** the sanctioned fetch path is the official Trading MCP
(read-only `get_accounts` / `get_portfolio` / `get_equity_positions`); run it
from any agent/automation on a schedule and pipe results through the merge.
`config.json` then stays a mirror of the broker, and trades made at the
broker flow into the engine within your sync interval.

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
python -m pytest tests/ -q        # ~90 tests, <2s, fully offline & synthetic
```

Covers factor math, regime states, every decision band/gate, portfolio
invariants, ledger dedupe + scoring, cache behavior, the offline pipeline
(cost-basis overlay, sparkline integrity, state schema), broker-merge guards
via subprocess, and the Flask API incl. path-traversal defenses. One test
validates the production `state.json` when an instance home exists and skips
otherwise — a bare clone never touches a real book.

## Data caveats

- Prices are Yahoo Finance, ~15–20 min delayed. Not a trading system — decision support.
- Fundamentals are quarterly and point-in-time blind (restatements/backfills are taken as-is).
- Universe-relative ranks (RS percentile) depend on what else you feed it — keep the scan universe stable if you rely on ranks.
- Nothing here is financial advice. The ledger exists so you only trust the engine as far as its evidence supports.

## Security

- `config.json`, `data/`, `ledger/` are gitignored on purpose: they hold positions, cost basis, cash and decision history — keep them that way.
- No credentials, brokerage APIs or personal data anywhere in this repo.
