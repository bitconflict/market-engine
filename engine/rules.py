"""Rules layer: market regime, decision engine, portfolio overlay."""
import json
from .factors import metrics, pillar_scores, pct_rank, _valid


def load_config(path=None):
    """Load engine config. Default resolution: $MARKET_ENGINE_HOME/config.json,
    else ./config.json at the repo root (gitignored — it is expected to carry
    portfolio data). Pass an explicit path to run fully parameterized, e.g.
    load_config("/path/to/portfolio.json")."""
    import os
    if path is None:
        root = os.environ.get("MARKET_ENGINE_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "config.json")
    return json.load(open(path))


TT_LABELS = [
    "price above both 150- and 200-day averages",
    "150-day average above 200-day average",
    "200-day average rising (Stage-2 base)",
    "50-day average above both longer averages",
    "price above 50-day average",
    "trading at least 30% above its 52-week low",
    "within 25% of its 52-week high",
    "12-1 momentum at or above 70th percentile of the scored universe (RS proxy)",
]

FLAG_NOTES = {
    "F-score <= 2 (financial health failing)":
        "Piotroski\u2019s 9-point health check at 2 or fewer: profits, cash flow, leverage or margins are deteriorating. Cheap-and-weak is the classic value trap.",
    "revenue declining AND cash-burning":
        "Sales shrinking while free cash flow stays negative \u2014 the company is being consumed while shrinking. A damaged *company*, not just a damaged stock.",
    "forward P/E > 150 \u2014 priced on hope":
        "You would pay more than 150x next year\u2019s expected earnings \u2014 the story must keep working for years to justify the price. Value discipline refuses to open or add here, no matter how good the growth looks.",
    "no forward earnings (loss-making)":
        "Analysts expect a loss next year \u2014 there is no earnings to value the stock on, only a story.",
    "more than 50% below its high (falling knife)":
        "Down over half from the 52-week high. Broken leaders usually broke for a reason; wait for a base (Minervini Stage 2) before buying.",
}

FLAG_FLIPS = {
    "F-score <= 2 (financial health failing)": "F-score rebuilds to 3+ (profits and cash flow stabilize)",
    "revenue declining AND cash-burning": "revenue inflects positive AND free cash flow turns positive",
    "forward P/E > 150 \u2014 priced on hope": "forward P/E drops back below 150 \u2014 earnings grow into the price or the price corrects",
    "no forward earnings (loss-making)": "forward earnings turn positive",
    "more than 50% below its high (falling knife)": "price recovers above the 50%-below-high line and re-establishes a base",
}


def flag_note(text):
    if text.startswith("dilution"):
        return "Share count up more than 8% in a year (Cramer: beware the diluters) \u2014 growth is being bought with your ownership slice."
    return FLAG_NOTES.get(text, "")


def flag_flip(text):
    if text.startswith("dilution"):
        return "share dilution slows below 8% YoY"
    return FLAG_FLIPS.get(text, "flag clears")


def regime(bench):
    """bench: dict with SPY/QQQ/^VIX recs -> regime dict."""
    out = {"label": "neutral", "spy": None, "qqq": None, "vix": None, "notes": [], "explain": []}
    for k, lbl in (("SPY", "spy"), ("QQQ", "qqq")):
        m = metrics(bench.get(k) or {})
        out[lbl] = m.get("vs200")
    vix_rec = bench.get("^VIX") or {}
    vc = [c for c in (vix_rec.get("prices") or {}).get("close", []) if _valid(c)]
    out["vix"] = vc[-1] if vc else None
    above = sum(1 for v in (out["spy"], out["qqq"]) if _valid(v) and v > 0)
    out["label"] = "bull" if above == 2 else ("bear" if above == 0 else "neutral")
    pre_vix = out["label"]
    if _valid(out["vix"]) and out["vix"] > 25:
        out["notes"].append("VIX > 25: defensive tone")
        if out["label"] == "bull":
            out["label"] = "neutral"
    def fmt(v):
        return f"{v*100:+.1f}%" if _valid(v) else "n/a"
    vtxt = f"VIX at {out['vix']:.0f}." if out["vix"] else "VIX n/a."
    out["explain"] = [
        {"step": "Trend inputs", "detail": f"SPY {fmt(out['spy'])} vs its 200-day average; QQQ {fmt(out['qqq'])}. {vtxt}"},
        {"step": "Rule", "detail": "Both indexes above their 200-day average = BULL. Split = NEUTRAL. Both below = BEAR. VIX above 25 demotes a BULL one notch (fear overrides trend). The regime never changes a stock\u2019s score \u2014 it only sets the cash target (5/8/10%) and pauses new buys in BEAR."},
        {"step": "Result", "detail": f"{above} of 2 indexes above trend" + (" \u2014 VIX>25 demoted bull" if pre_vix == "bull" and out["label"] != "bull" else "") + f" \u2192 {out['label'].upper()}."},
    ]
    return out


def _rs_and_scores(recs, cfg):
    """Compute metrics for all tickers, then universe-relative percentiles + pillar scores."""
    ms = {t: metrics(r) for t, r in recs.items()}
    trade = [t for t in ms if t not in ("SPY", "QQQ", "^VIX")]
    m121 = [ms[t].get("m121") for t in trade]
    m3 = [ms[t].get("m3") for t in trade]
    scored = {}
    for t in trade:
        m = ms[t]
        rs_p = pct_rank(m.get("m121"), m121)
        m3_p = pct_rank(m.get("m3"), m3)
        tt = list(m.get("tt") or [False] * 7)
        rs_ok = rs_p is not None and rs_p >= 0.70
        tt.append(bool(rs_ok))
        m["tt8"] = tt
        m["tt_passes"] = sum(1 for x in tt if x)
        m["rs_pct"] = rs_p
        scored[t] = (m, pillar_scores(m, rs_pct=rs_p, m3_pct=m3_p))
    return scored


def decide(t, m, sc, cfg, is_holding, weight, regime_label, cost_hint=None):
    """Return decision dict for one ticker, with a rule-by-rule explain trace."""
    w = cfg["weights"]
    composite = round(sum(w[k] * sc[k] for k in w), 1)
    reasons, flags, explain = [], [], []
    tt_passes = m.get("tt_passes", 0)
    f = m.get("f_score")

    top = sorted(sc.items(), key=lambda kv: -kv[1])
    bot = sorted(sc.items(), key=lambda kv: kv[1])
    reasons.append(f"{top[0][0]} {top[0][1]:.0f}/100, {top[1][0]} {top[1][1]:.0f}/100 strong")
    reasons.append(f"weakest: {bot[0][0]} {bot[0][1]:.0f}, {bot[1][0]} {bot[1][1]:.0f}")
    if _valid(f):
        reasons.append(f"Piotroski F-score {f}/9")
    if m.get("rev_growth") is not None:
        reasons.append(f"revenue growth {m['rev_growth']*100:.0f}%")
    if m.get("m121") is not None:
        reasons.append(f"12-1 momentum {m['m121']*100:+.0f}%")
    reasons.append(f"trend template {tt_passes}/8")

    explain.append({"step": "Composite score",
        "detail": (f"{composite:.1f}/100 = quality {sc['quality']:.0f}\u00d7{w['quality']:.0%} + growth {sc['growth']:.0f}\u00d7{w['growth']:.0%} "
                   f"+ value {sc['value']:.0f}\u00d7{w['value']:.0%} + momentum {sc['momentum']:.0f}\u00d7{w['momentum']:.0%}. "
                   "Quality = Piotroski F-score, ROE, free-cash-flow yield, leverage. Growth = revenue/earnings growth and acceleration. "
                   "Value = forward P/E, PEG, EV/EBITDA, FCF yield (soft: expensive is penalized, never auto-sold). "
                   "Momentum = 12-1 return rank vs the scored universe, trend, distance from high.")})

    if _valid(f) and f <= 2:
        flags.append("F-score <= 2 (financial health failing)")
    if _valid(m.get("rev_growth")) and m["rev_growth"] < 0 and _valid(m.get("fcf")) and m["fcf"] <= 0:
        flags.append("revenue declining AND cash-burning")
    if _valid(m.get("dilution")) and m["dilution"] > 0.08:
        flags.append(f"dilution {m['dilution']*100:.0f}% YoY (Cramer: beware the diluters)")
    if _valid(m.get("fwd_pe")) and m["fwd_pe"] > 150:
        flags.append("forward P/E > 150 \u2014 priced on hope")
    if _valid(m.get("fwd_pe")) and m["fwd_pe"] < 0:
        flags.append("no forward earnings (loss-making)")
    if _valid(m.get("off_hi")) and m["off_hi"] < -0.5:
        flags.append("more than 50% below its high (falling knife)")
    hard_flags = len(flags)
    if flags:
        explain.append({"step": "Red flags", "detail": f"{hard_flags} flag(s): " + "; ".join(flags) + ". Each flag caps how bullish the engine can be."})

    if composite >= 70:
        action = "ADD" if tt_passes >= 6 else "HOLD"
        band = f"ADD band (composite \u226570); trend {tt_passes}/8 {'clears' if tt_passes >= 6 else 'fails'} the \u22656/8 requirement"
    elif composite >= 50:
        action = "HOLD"
        band = "HOLD band (50\u201369)"
    elif composite >= 42:
        action = "TRIM" if tt_passes <= 2 else "HOLD"
        band = f"borderline band (42\u201349); trend {tt_passes}/8 " + ("\u22642 is broken \u2192 TRIM" if tt_passes <= 2 else "intact \u2192 HOLD")
    elif composite >= 32:
        action = "TRIM"
        band = "weak band (32\u201341)"
    else:
        action = "SELL"
        band = "broken band (<32)"
    explain.append({"step": "Base action", "detail": f"Composite {composite:.1f} sits in the {band} \u2192 {action}."})

    if tt_passes <= 2 and composite < 45:
        action = "TRIM" if composite >= 38 else "SELL"
        explain.append({"step": "Trend gate", "detail": f"Minervini trend {tt_passes}/8 with composite <45 \u2192 {action}. No confirmed uptrend means no benefit of the doubt."})
    if hard_flags >= 2 and composite < 45:
        action = "SELL"
        explain.append({"step": "Flag gate", "detail": "2+ flags with composite <45 \u2192 SELL."})
    elif hard_flags >= 1 and composite < 38:
        action = "SELL"
        explain.append({"step": "Flag gate", "detail": "Flag(s) with composite <38 \u2192 SELL."})
    elif hard_flags == 1 and action in ("ADD", "HOLD") and composite < 60:
        action = "TRIM"
        explain.append({"step": "Flag gate", "detail": "One flag caps the action at TRIM for composites under 60 \u2014 a flagged name cannot be added to, and is barely held."})
    elif hard_flags == 1 and action == "ADD":
        action = "HOLD"
        explain.append({"step": "Flag gate", "detail": "One flag blocks the ADD \u2192 HOLD."})

    if not is_holding:
        if action in ("TRIM", "SELL"):
            pre = action
            action = "AVOID"
        elif action == "ADD" and regime_label == "bear":
            pre = action
            action = "WAIT"
            reasons.append("new buys paused in bearish regime")
        elif action == "ADD":
            pre = action
            action = "BUY"
        elif action == "HOLD" and composite < 62:
            pre = action
            action = "PASS"
        elif action == "HOLD":
            pre = action
            action = "WATCH"
        else:
            pre = action
        explain.append({"step": "Watchlist mapping",
            "detail": f"Not currently held \u2014 engine action {pre} maps to {action}. BUY needs composite \u226570, trend \u22656/8 and zero flags; WATCH needs \u226562; TRIM/SELL map to AVOID: the tool will not open a position that its own rules would not keep."})

    notes = []
    if is_holding:
        if _valid(m.get("m6")) and m["m6"] >= 0.20 and weight is not None and weight >= 0.08:
            notes.append("up >20% in 6m and >=8% weight \u2014 trim 5-10% into strength (Cramer Rule 1)")
        if weight is not None and weight > cfg["max_position_weight"]:
            notes.append(f"weight {weight*100:.1f}% over cap {cfg['max_position_weight']*100:.0f}%")
        if action == "ADD":
            max_add = 0.06 if regime_label != "bear" else 0.0
            notes.append(f"suggested new money <= {max_add*100:.0f}% of account, staged in 3 tranches on dips")

    flip = []
    if flags and action in ("AVOID", "TRIM", "SELL", "PASS", "HOLD", "WATCH"):
        flip.append("To improve this rating: " + "; ".join(flag_flip(x) for x in flags) + ".")
    if action == "AVOID":
        flip.append("Then: composite \u226550 with flags cleared = WATCH; composite \u226570, trend \u22656/8 and zero flags = BUY.")
    elif action == "PASS":
        flip.append(f"Composite {composite:.1f} needs \u226562 (flags clear) to become WATCH.")
    elif action == "SELL":
        flip.append("Composite back above the band boundary (and flags cleared) softens this to HOLD.")
    elif action in ("BUY", "ADD"):
        flip.append("Entry stays staged: max 6% of account, three tranches on dips. A BEAR regime flips new buys to WAIT.")
    elif is_holding and action == "HOLD":
        flip.append("HOLD means: thesis intact, no action needed \u2014 re-rated automatically at every refresh.")
    flip.append("Data caveat: prices 15\u201320 min delayed (Yahoo); fundamentals are quarterly.")

    conf = "high" if abs(composite - 50) >= 18 and m.get("f_score") is not None else "medium"
    if m.get("f_score") is None and m.get("m121") is None:
        conf = "low"

    return {"ticker": t, "action": action, "composite": composite, "pillars": sc,
            "flags": flags, "flag_notes": [flag_note(x) for x in flags],
            "reasons": reasons, "notes": notes, "explain": explain, "flip": flip,
            "tt8": m.get("tt8"), "tt_labels": TT_LABELS,
            "confidence": conf, "trend_passes": tt_passes, "f_score": f, "rs_pct": m.get("rs_pct"),
            "px": m.get("px"), "off_hi": m.get("off_hi"), "m6": m.get("m6"),
            "m121": m.get("m121"), "fwd_pe": m.get("fwd_pe"), "rev_growth": m.get("rev_growth"),
            "name": m.get("name"), "sector": m.get("sector")}


def portfolio_state(recs, cfg, cash=0.0):
    """Full engine pass. Returns dict for API + digest."""
    scored = _rs_and_scores({t: r for t, r in recs.items()}, cfg)
    bench_recs = {k: recs.get(k) or {} for k in ("SPY", "QQQ", "^VIX")}
    rg = regime(bench_recs)

    positions = cfg["positions"]

    def _px(t):
        r = recs.get(t) or {}
        p = (r.get("info") or {}).get("currentPrice")
        if not (p and p == p):
            cl = (r.get("prices") or {}).get("close") or []
            p = cl[-1] if cl else None
        return p

    px = {t: _px(t) for t in recs}
    vals = {t: positions[t] * (px.get(t) or 0) for t in positions}
    invested = sum(vals.values())
    total = invested + cash
    holdings, watch = [], []
    etfs = set(cfg.get("etf_tickers", []))
    for t in positions:
        wgt = vals[t] / total if total else 0
        if t in etfs:
            holdings.append({"ticker": t, "action": "HOLD", "composite": None, "pillars": None,
                             "flags": [], "reasons": ["index/ETF sleeve — Cramer's anchor; rebalance, don't score"],
                             "notes": [], "px": px.get(t), "weight": wgt, "value": vals[t],
                             "shares": positions[t], "confidence": "high", "trend_passes": None,
                             "flag_notes": [], "tt_labels": TT_LABELS,
                             "explain": [{"step": "Index sleeve", "detail": "ETFs are the portfolio anchor (the ~half-index core in the 3-part plan / Bogle cost discipline). Never scored or sold on signals \u2014 rebalance, do not trade."}],
                             "flip": ["The index sleeve is rebalanced, not re-rated. If it drifts far from target weight, rebalance toward it."]})
            continue
        if t in scored:
            m, sc = scored[t]
            d = decide(t, m, sc, cfg, True, wgt, rg["label"])
        else:
            d = {"ticker": t, "action": "HOLD", "composite": None, "pillars": {}, "flags": [],
                 "reasons": ["no data"], "notes": [], "px": px.get(t), "confidence": "low"}
        d.update({"weight": wgt, "value": vals[t], "shares": positions[t]})
        d["overlay_note"] = (cfg.get("manual_overlay") or {}).get(t)
        holdings.append(d)
    for t in cfg.get("watchlist", []):
        if t in positions or t not in scored:
            continue
        m, sc = scored[t]
        d = decide(t, m, sc, cfg, False, 0.0, rg["label"])
        d.update({"weight": 0.0, "value": 0.0, "shares": 0})
        d["list"] = "watch"
        d["overlay_note"] = (cfg.get("manual_overlay") or {}).get(t)
        watch.append(d)

    # screener: curated universe outside holdings+watchlist, scored by the same rules
    candidates = []
    known = set(positions) | set(cfg.get("watchlist", []))
    for t in cfg.get("scan_universe", []):
        if t in known or t not in scored:
            continue
        m, sc = scored[t]
        d = decide(t, m, sc, cfg, False, 0.0, rg["label"])
        d.update({"weight": 0.0, "value": 0.0, "shares": 0})
        d["list"] = "screener"
        candidates.append(d)
    candidates.sort(key=lambda d: -(d.get("composite") or 0))
    candidates = [c for c in candidates if (c.get("composite") or 0) >= 45][:12]

    spec_budget = cfg.get("speculative_budget", 0.10)
    spec_pct = sum(h["weight"] for h in holdings if h["ticker"] in set(cfg.get("speculative_tickers", [])))
    cash_target = cfg["cash_targets"].get(rg["label"], 0.08)
    actions_order = {"SELL": 0, "TRIM": 1, "AVOID": 2, "PASS": 3, "HOLD": 4, "WATCH": 5, "WAIT": 6, "BUY": 7, "ADD": 8}
    actions = sorted([h for h in holdings if h.get("action") in ("SELL", "TRIM")] +
                     [w for w in watch if w.get("action") in ("BUY", "ADD")],
                     key=lambda d: actions_order.get(d["action"], 9))
    holdings.sort(key=lambda d: -(d.get("value") or 0))
    watch.sort(key=lambda d: -(d.get("composite") or 0))

    return {"as_of": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip(),
            "regime": rg, "holdings": holdings, "watchlist": watch, "candidates": candidates,
            "speculative_tickers": list(cfg.get("speculative_tickers", [])),
            "etf_tickers": list(etfs),
            "portfolio": {"invested": invested, "cash": cash, "total": total,
                          "cash_pct": cash / total if total else 0.0, "cash_target": cash_target,
                          "speculative_pct": spec_pct, "speculative_budget": spec_budget,
                          "over_cap": [h["ticker"] for h in holdings
                                       if (h.get("weight") or 0) > cfg["max_position_weight"] and h["ticker"] not in etfs]},
            "actions": actions}
