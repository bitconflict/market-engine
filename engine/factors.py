"""Factor computation: trend template, momentum, Piotroski F-score, pillar scores."""
import math


def _valid(x):
    return isinstance(x, (int, float)) and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


def sma(vals, n):
    v = [x for x in vals if _valid(x)]
    if len(v) < n:
        return None
    return sum(v[-n:]) / float(n)


def _ret(closes, lookback, skip=0):
    """return over lookback sessions ending `skip` sessions before the latest."""
    c = [x for x in closes if _valid(x)]
    end = len(c) - skip
    if len(c) < lookback + skip + 1 or end <= 0:
        return None
    a, b = c[end - lookback], c[end - 1]
    if not _valid(a) or a == 0:
        return None
    return b / a - 1.0


def _piecewise(x, pts):
    """pts: [(x,y),...] ascending; linear interp, clamp at ends."""
    if not _valid(x):
        return None
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (x - x0) * (y1 - y0) / (x1 - x0)
    return float(pts[-1][1])


def pct_rank(val, arr):
    xs = [a for a in arr if _valid(a)]
    if not _valid(val) or len(xs) < 3:
        return None
    below = sum(1 for a in xs if a < val)
    return below / float(len(xs))


def metrics(rec, bench_m121=None):
    """Raw metrics for one ticker (prices-only ok)."""
    closes = [c for c in (rec.get("prices") or {}).get("close", []) if _valid(c)]
    m = {}
    if closes:
        px = closes[-1]
        s50, s150, s200 = sma(closes, 50), sma(closes, 150), sma(closes, 200)
        s200_prev = sma(closes[:-21], 200) if len(closes) > 221 else None
        lo52, hi52 = min(closes[-252:]), max(closes[-252:])
        m121 = _ret(closes, 252, skip=21) or _ret(closes, 252)
        m.update(px=px, sma50=s50, sma150=s150, sma200=s200, lo52=lo52, hi52=hi52,
                 m121=m121, m3=_ret(closes, 63), m6=_ret(closes, 126),
                 off_hi=(px / hi52 - 1.0) if hi52 else None,
                 above_lo=(px / lo52 - 1.0) if lo52 else None,
                 vs200=(px / s200 - 1.0) if s200 else None)
        tt = [
            bool(_valid(s150) and _valid(s200) and px > s150 and px > s200),
            bool(_valid(s150) and _valid(s200) and s150 > s200),
            bool(_valid(s200) and _valid(s200_prev) and s200 > s200_prev),
            bool(_valid(s50) and _valid(s150) and _valid(s200) and s50 > s150 and s50 > s200),
            bool(_valid(s50) and px > s50),
            bool(_valid(m.get("above_lo")) and m["above_lo"] >= 0.30),
            bool(_valid(m.get("off_hi")) and m["off_hi"] >= -0.25),
        ]
        m["tt"] = tt  # 7 price/MA conditions; 8th (RS>=70) added in score pass
    info = rec.get("info") or {}
    m.update(rev_growth=info.get("revenueGrowth"), eps_growth=info.get("earningsGrowth"),
             trail_pe=info.get("trailingPE"), fwd_pe=info.get("forwardPE"),
             peg=info.get("pegRatio"), ev_ebitda=info.get("enterpriseToEbitda"),
             roe=info.get("returnOnEquity"), roa=info.get("returnOnAssets"),
             de=info.get("debtToEquity"), fcf=info.get("freeCashflow"),
             mcap=info.get("marketCap"), margin=info.get("profitMargins"),
             gross=info.get("grossMargins"), fwd_eps=info.get("forwardEps"),
             trail_eps=info.get("trailingEps"), sector=info.get("sector"),
             name=info.get("shortName"), inst=info.get("heldPercentInstitutions"))
    m["fcf_yield"] = (m["fcf"] / m["mcap"]) if _valid(m.get("fcf")) and _valid(m.get("mcap")) and m["mcap"] else None
    eps_g_f = None
    if _valid(m.get("fwd_eps")) and _valid(m.get("trail_eps")) and m["trail_eps"] and m["trail_eps"] > 0:
        eps_g_f = m["fwd_eps"] / m["trail_eps"] - 1.0
    m["eps_g_fwd"] = eps_g_f

    # Piotroski F-score from annual statements
    inc, bs, cf = rec.get("income") or [], rec.get("balance") or [], rec.get("cashflow") or []
    f, fparts = None, None
    try:
        if len(inc) >= 2 and len(bs) >= 2 and len(cf) >= 2:
            i1, i0 = inc[-1], inc[-2]
            b1, b0 = bs[-1], bs[-2]
            c1, c0 = cf[-1], cf[-2]

            def g(d, k):
                v = d.get(k)
                return v if _valid(v) else None

            ta1, ta0 = g(b1, "Total Assets"), g(b0, "Total Assets")
            if ta1 and ta0:
                ni1, ni0 = g(i1, "Net Income"), g(i0, "Net Income")
                cfo1 = g(c1, "Operating Cash Flow")
                roa1 = ni1 / ta1 if _valid(ni1) else None
                roa0 = ni0 / ta0 if _valid(ni0) else None
                ld1, ld0 = g(b1, "Long Term Debt"), g(b0, "Long Term Debt")
                ld1 = ld1 if _valid(ld1) else g(b1, "Total Debt")
                ld0 = ld0 if _valid(ld0) else g(b0, "Total Debt")
                ca1, cl1 = g(b1, "Current Assets"), g(b1, "Current Liabilities")
                ca0, cl0 = g(b0, "Current Assets"), g(b0, "Current Liabilities")
                cr1 = ca1 / cl1 if _valid(ca1) and _valid(cl1) and cl1 else None
                cr0 = ca0 / cl0 if _valid(ca0) and _valid(cl0) and cl0 else None
                sh1 = g(b1, "Share Issued") or g(b1, "Ordinary Shares Number")
                sh0 = g(b0, "Share Issued") or g(b0, "Ordinary Shares Number")
                gm1 = g(i1, "Gross Profit") / g(i1, "Total Revenue") if _valid(g(i1, "Gross Profit")) and _valid(g(i1, "Total Revenue")) and g(i1, "Total Revenue") else None
                gm0 = g(i0, "Gross Profit") / g(i0, "Total Revenue") if _valid(g(i0, "Gross Profit")) and _valid(g(i0, "Total Revenue")) and g(i0, "Total Revenue") else None
                at1 = g(i1, "Total Revenue") / ta1 if _valid(g(i1, "Total Revenue")) else None
                at0 = g(i0, "Total Revenue") / ta0 if _valid(g(i0, "Total Revenue")) else None
                parts = [
                    _valid(roa1) and roa1 > 0,
                    _valid(cfo1) and cfo1 > 0,
                    _valid(roa1) and _valid(roa0) and roa1 > roa0,
                    _valid(cfo1) and _valid(ni1) and cfo1 > ni1,
                    _valid(ld1) and _valid(ld0) and (ld1 / ta1) < (ld0 / ta0),
                    _valid(cr1) and _valid(cr0) and cr1 > cr0,
                    not (_valid(sh1) and _valid(sh0) and sh1 > sh0),
                    _valid(gm1) and _valid(gm0) and gm1 > gm0,
                    _valid(at1) and _valid(at0) and at1 > at0,
                ]
                fparts = [1 if p else 0 for p in parts]
                f = sum(fparts)
                m["dilution"] = (sh1 / sh0 - 1.0) if _valid(sh1) and _valid(sh0) and sh0 else None
                # 3y revenue CAGR
                revs = [d.get("Total Revenue") for d in inc if _valid(d.get("Total Revenue"))]
                if len(revs) >= 3 and revs[0] > 0 and revs[-1] > 0:
                    yrs = len(revs) - 1
                    m["rev_cagr"] = (revs[-1] / revs[0]) ** (1.0 / yrs) - 1.0
    except Exception:
        f = None
    m["f_score"] = f
    m["f_parts"] = fparts
    return m


def pillar_scores(m, rs_pct=None, m3_pct=None, m121_pct=None):
    q = g = v = mo = None
    # ---- Quality
    parts = []
    f = m.get("f_score")
    parts.append((f / 9.0) * 40 if _valid(f) else 22.0)
    roe = m.get("roe")
    parts.append(max(0.0, min(roe, 0.25)) / 0.25 * 20 if _valid(roe) else 10.0)
    fy_q = m.get("fcf_yield")
    parts.append(max(0.0, min(fy_q / 0.06, 1.0)) * 20 if _valid(fy_q) else 10.0)
    de = m.get("de")
    parts.append((20.0 if de < 50 else 14.0 if de < 100 else 8.0 if de < 180 else 3.0) if _valid(de) else 10.0)
    q = sum(parts)

    # ---- Growth
    rg = m.get("rev_growth")
    p_rg = _piecewise(rg, [(-0.20, 0), (0.0, 30), (0.10, 55), (0.25, 85), (0.40, 100)]) if _valid(rg) else 50.0
    eg = m.get("eps_growth")
    p_eg = _piecewise(eg, [(-0.30, 0), (0.0, 30), (0.10, 55), (0.25, 85), (0.40, 100)]) if _valid(eg) else 50.0
    ef = m.get("eps_g_fwd")
    p_ef = _piecewise(ef, [(-0.10, 0), (0.0, 50), (0.20, 100)]) if _valid(ef) else 50.0
    rc = m.get("rev_cagr")
    p_rc = _piecewise(rc, [(-0.10, 10), (0.0, 45), (0.08, 70), (0.15, 100)]) if _valid(rc) else 50.0
    g = 0.35 * p_rg + 0.30 * p_eg + 0.15 * p_ef + 0.20 * p_rc

    # ---- Value (higher = cheaper)
    fp = m.get("fwd_pe") or m.get("trail_pe")
    p_pe = _piecewise(fp, [(8, 100), (12, 90), (18, 75), (25, 55), (35, 35), (50, 20), (80, 8), (150, 2)]) if _valid(fp) else 50.0
    pg = m.get("peg")
    p_pg = _piecewise(pg, [(0.7, 100), (1.0, 88), (1.5, 70), (2.0, 45), (3.0, 20), (4.0, 5)]) if _valid(pg) and pg > 0 else 50.0
    ev = m.get("ev_ebitda")
    p_ev = _piecewise(ev, [(5, 95), (8, 88), (12, 75), (18, 55), (25, 35), (35, 20), (60, 8)]) if _valid(ev) and ev > 0 else 50.0
    fy = m.get("fcf_yield")
    p_fy = _piecewise(fy, [(-0.02, 5), (0.0, 12), (0.01, 30), (0.02, 50), (0.04, 75), (0.06, 90)]) if _valid(fy) else 50.0
    v = 0.35 * p_pe + 0.25 * p_pg + 0.20 * p_ev + 0.20 * p_fy

    # ---- Momentum
    p_rs = (rs_pct * 100) if _valid(rs_pct) else 45.0
    p_200 = _piecewise(m.get("vs200"), [(-0.20, 5), (0.0, 40), (0.10, 70), (0.25, 100)]) if _valid(m.get("vs200")) else 45.0
    p_hi = _piecewise(m.get("off_hi"), [(-0.80, 5), (-0.50, 20), (-0.30, 45), (-0.15, 70), (-0.05, 90), (0.0, 100)]) if _valid(m.get("off_hi")) else 45.0
    p_m3 = (m3_pct * 100) if _valid(m3_pct) else 45.0
    mo = 0.55 * p_rs + 0.15 * p_200 + 0.15 * p_hi + 0.15 * p_m3

    return {"quality": round(q, 1), "growth": round(g, 1), "value": round(v, 1), "momentum": round(mo, 1)}
