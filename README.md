# market-engine

A rules-based equity decision engine: it scores stocks on four pillars, applies
hard risk gates, maps scores to concrete actions (BUY / ADD / HOLD / TRIM / SELL,
or BUY / WATCH / PASS / AVOID for non-holdings), and keeps a self-scoring ledger
of every call it makes versus SPY.

The engine is **portfolio-agnostic**: holdings, cash and constraints enter only
as a config parameter (`portfolio_state(recs, cfg, cash)`). Nothing in this repo
knows about anyone's actual portfolio, and nothing portfolio-specific should
ever be committed.

## Architecture

| Module | Role |
|---|---|
| `engine/data.py` | Fetch layer. Pulls fundamentals + 2y daily prices via yfinance, caches as JSON (`./data/cache`, 6h TTL). Fetches whatever ticker list it is given. |
| `engine/factors.py` | Factor layer. Computes raw metrics (trend template, 12-1 momentum, Piotroski F-score, ROE, FCF yield, valuation ratios…) and normalizes them into four pillar scores (0–100): **quality, growth, value, momentum**. |
| `engine/rules.py` | Decision layer. Market regime from SPY/QQQ vs 200-day + VIX; composite = weighted pillars; band → base action; **trend gate** (Minervini 8-point template) and **flag gates** (F-score ≤2, cash-burn, dilution >8%, fwd P/E >150, no forward earnings, >50% off high) can only *downgrade*; position caps; staged-entry notes. Every decision carries a rule-by-rule `explain` trace. |
| `engine/ledger.py` | Track record. Appends daily decision snapshots + NAV, and scores past calls against SPY at 30/60/90-day horizons (BUY/ADD win by beating SPY, SELL/TRIM/AVOID by lagging it). |

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # edit: your positions, watchlist, scan universe
```

```python
from engine.data import fetch_many
from engine.rules import load_config, portfolio_state

cfg = load_config()                       # or load_config("/any/path.json")
tickers = (cfg["benchmarks"] + list(cfg["positions"])
           + cfg["watchlist"] + cfg["scan_universe"])
recs = fetch_many(tickers)
state = portfolio_state(recs, cfg, cash=cfg.get("cash", 0.0))

print(state["regime"]["label"])           # bull | neutral | bear
for a in state["actions"]:                # actionable calls, SELL/TRIM first
    print(a["ticker"], a["action"], a["composite"])
state["candidates"]                       # screener picks from scan_universe
```

Fully parameterized (no local file): pass a dict-shaped config and records
straight in — `portfolio_state(recs, cfg, cash)` never touches disk for
portfolio state.

## Config schema (`config.json`, gitignored)

Strategy parameters: `weights` (must sum to 1), `cash_targets` (per regime),
`max_position_weight`, `speculative_budget`, `benchmarks`.

Portfolio inputs (yours, local only): `positions` (ticker → shares),
`cost_basis`, `cash`, `watchlist`, `scan_universe` (screener pool, scored with
the same rules), `etf_tickers` (never scored — rebalance-only anchor),
`speculative_tickers`, `manual_overlay` (ticker → note shown on its card).

## Decision flow

1. **Regime** — SPY & QQQ vs 200-day (VIX>25 demotes bull one notch). Sets cash target; bear pauses new buys.
2. **Composite** = quality×w + growth×w + value×w + momentum×w (default 30/20/20/30).
3. **Bands** — ≥70 ADD, 50–69 HOLD, 42–49 borderline, 32–41 TRIM, <32 SELL.
4. **Gates (downgrade-only)** — trend template ≤2/8 forces TRIM/SELL on weak scores; red flags cap actions (1 flag ⇒ no adds under 60; 2+ flags under 45 ⇒ SELL).
5. **Held vs not** — non-holdings map to BUY / WATCH / PASS / AVOID; TRIM/SELL grades never open a position.
6. **Ledger** — every daily call is snapshotted and later scored vs SPY; replicate only what the track record supports.

## Data caveats

- Prices are Yahoo Finance, ~15–20 min delayed. Not a trading system — decision support.
- Fundamentals are quarterly and point-in-time blind (restatements/backfills are taken as-is).
- Universe-relative ranks (RS percentile) depend on what else you feed it — keep the scan universe stable if you rely on ranks.

## Embedding this engine (host apps)

Install it into your environment and keep all runtime state in **your** directory:

```bash
pip install -e /path/to/market-engine        # editable; single copy on disk
export MARKET_ENGINE_HOME=/path/to/your/app  # config.json, data/, ledger/ live here
```

`MARKET_ENGINE_HOME` decides where `config.json` is read from, where the price
cache goes, and where the ledger writes — the engine package itself stays
read-only. Or skip the env var entirely and pass everything explicitly:
`load_config("/any/path.json")` and `portfolio_state(recs, cfg, cash)`.

## Security

- `config.json`, `data/`, `ledger/` are gitignored on purpose: they hold positions, cost basis, cash and decision history.
- No credentials, brokerage APIs or personal data anywhere in this repo.
