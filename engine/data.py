"""Data layer: fetch fundamentals + prices via yfinance, cache as JSON."""
import json, os, time, math, warnings
warnings.filterwarnings("ignore")

# Writable state lives in the host instance dir — see engine/paths.py.
# (MARKET_ENGINE_HOME env, else cwd. Hosts set it before importing engine.)
from . import paths
ROOT = paths.home()
CACHE = paths.cache_dir()
os.makedirs(CACHE, exist_ok=True)
CACHE_TTL = 6 * 3600  # seconds


def _num(x):
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _stmt_series(df, row_names):
    """From a yfinance statement DataFrame, pull named rows -> {col_ts_iso: [vals]} chronological (oldest first)."""
    out = []
    if df is None or getattr(df, "empty", True):
        return out
    cols = sorted(df.columns)  # oldest -> newest
    for ts in cols:
        rec = {"date": str(ts.date()) if hasattr(ts, "date") else str(ts)}
        for rn in row_names:
            if rn in df.index:
                v = df.at[rn, ts]
                try:
                    rec[rn] = _num(v)
                except Exception:
                    rec[rn] = None
            else:
                rec[rn] = None
        out.append(rec)
    return out


def fetch_one(tkr, force=False):
    path = os.path.join(CACHE, tkr.replace("^", "IDX_") + ".json")
    if not force and os.path.exists(path) and (time.time() - os.path.getmtime(path)) < CACHE_TTL:
        try:
            return json.load(open(path))
        except Exception:
            pass
    import yfinance as yf
    t = yf.Ticker(tkr)
    rec = {"ticker": tkr, "fetched": int(time.time()), "ok": False}
    try:
        info = t.info or {}
    except Exception as e:
        info = {"_info_err": str(e)[:120]}
    keep = ["trailingPE","forwardPE","pegRatio","returnOnEquity","returnOnAssets","debtToEquity",
            "freeCashflow","revenueGrowth","earningsGrowth","enterpriseToEbitda","fiftyTwoWeekHigh",
            "fiftyTwoWeekLow","twoHundredDayAverage","fiftyDayAverage","currentPrice","marketCap",
            "dividendYield","profitMargins","grossMargins","operatingMargins","trailingEps",
            "forwardEps","totalRevenue","enterpriseValue","totalCash","totalDebt","bookValue",
            "sharesOutstanding","heldPercentInstitutions","sector","shortName","beta"]
    rec["info"] = {k: _num(info.get(k)) if k not in ("sector", "shortName") else info.get(k) for k in keep}

    try:  # prices (2y daily)
        h = t.history(period="2y", interval="1d", auto_adjust=True)
        closes = [_num(v) for v in h["Close"].tolist()]
        vols = [_num(v) for v in h.get("Volume", h["Close"]).tolist()]
        dates = [str(d.date()) for d in h.index]
        pairs = [(d, c, v) for d, c, v in zip(dates, closes, vols) if c is not None]
        rec["prices"] = {"dates": [p[0] for p in pairs], "close": [p[1] for p in pairs], "volume": [p[2] for p in pairs]}
        rec["ok"] = len(pairs) > 60
    except Exception as e:
        rec["prices"] = {"dates": [], "close": [], "volume": []}
        rec["_hist_err"] = str(e)[:120]

    if tkr not in ("SPY", "QQQ", "^VIX"):
        try:
            inc = t.income_stmt
            bs = t.balance_sheet
            cf = t.cashflow
            rec["income"] = _stmt_series(inc, ["Total Revenue", "Gross Profit", "Net Income", "Research And Development", "Operating Income"])
            rec["balance"] = _stmt_series(bs, ["Total Assets", "Long Term Debt", "Current Assets", "Current Liabilities",
                                               "Ordinary Shares Number", "Share Issued", "Total Debt"])
            rec["cashflow"] = _stmt_series(cf, ["Operating Cash Flow", "Free Cash Flow", "Capital Expenditure"])
        except Exception as e:
            rec["_stmt_err"] = str(e)[:120]
            rec["income"] = rec.get("income", [])
            rec["balance"] = rec.get("balance", [])
            rec["cashflow"] = rec.get("cashflow", [])

    try:
        json.dump(rec, open(path, "w"))
    except Exception:
        pass
    return rec


def fetch_many(tickers, force=False):
    out = {}
    for tk in tickers:
        try:
            out[tk] = fetch_one(tk, force=force)
        except Exception as e:
            out[tk] = {"ticker": tk, "ok": False, "err": str(e)[:150]}
        time.sleep(0.15)
    return out
