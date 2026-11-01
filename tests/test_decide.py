"""Unit tests: engine.rules.decide — bands, gates, watch mapping, notes."""
from engine.rules import decide

from conftest import decide_m, flat_sc, make_cfg

CFG = make_cfg()


def d(score, held=True, weight=0.05, regime_label="bull", **m_over):
    return decide("T", decide_m(**m_over), flat_sc(score), CFG, held, weight, regime_label)


# ---------- composite bands (held) ----------

def test_band_add():
    assert d(80)["action"] == "ADD"

def test_band_hold():
    assert d(60)["action"] == "HOLD"

def test_band_borderline_intact_trend_holds():
    assert d(45, tt_passes=4)["action"] == "HOLD"

def test_band_borderline_broken_trend_trims():
    assert d(45, tt_passes=2)["action"] == "TRIM"

def test_band_weak():
    assert d(35, tt_passes=4)["action"] == "TRIM"

def test_band_broken():
    assert d(25, tt_passes=4)["action"] == "SELL"


# ---------- trend gate ----------

def test_trend_gate_sells_broken_and_weak():
    # composite 37 (<38) with trend 0/8 -> trend gate forces SELL
    assert d(37, tt_passes=0)["action"] == "SELL"

def test_trend_gate_trims_midscore_broken_trend():
    assert d(44, tt_passes=1)["action"] == "TRIM"


# ---------- flag gates ----------

def test_one_flag_caps_hold_to_trim_below_60():
    assert d(50, f_score=1)["action"] == "TRIM"

def test_one_flag_allows_hold_above_60():
    assert d(65, f_score=1)["action"] == "HOLD"

def test_one_flag_blocks_add():
    r = d(75, f_score=1)
    assert r["action"] == "HOLD"               # ADD downgraded, not TRIMmed

def test_two_flags_sell_weak():
    assert d(36, f_score=1, fwd_pe=-2)["action"] == "SELL"

def test_dilution_flag_detected():
    r = d(60, dilution=0.15)
    assert any("dilution" in f for f in r["flags"])

def test_falling_knife_flag():
    assert any("falling knife" in f for f in d(50, off_hi=-0.6)["flags"])


# ---------- watch (not-held) mapping ----------

def test_watch_buy():
    assert d(80, held=False)["action"] == "BUY"

def test_watch_wait_in_bear():
    assert d(80, held=False, regime_label="bear")["action"] == "WAIT"

def test_watch_watch_band():
    assert d(65, held=False)["action"] == "WATCH"

def test_watch_pass_band():
    assert d(55, held=False)["action"] == "PASS"

def test_watch_avoid_never_opens():
    assert d(30, held=False)["action"] == "AVOID"


# ---------- notes ----------

def test_note_cramer_trim_on_big_winner():
    r = d(60, weight=0.10, m6=0.25)
    assert any("Cramer" in n for n in r["notes"])

def test_note_over_cap():
    r = d(60, weight=0.20)
    assert any("over cap" in n for n in r["notes"])

def test_note_staged_add_sizing():
    r = d(80)                                    # ADD on a held name
    assert any("tranches" in n for n in r["notes"])

def test_note_no_add_sizing_in_bear():
    r = d(80, regime_label="bear")               # bear: ADD->? held name stays ADD band but sizing 0%
    assert any("0% of account" in n for n in r["notes"])


# ---------- explain/flip ----------

def test_explain_trace_present():
    r = d(80)
    assert r["explain"][0]["step"] == "Composite score"
    assert any(g["step"] == "Base action" for g in r["explain"])
    assert r["flip"]

def test_flag_notes_paired_with_flags():
    r = d(36, f_score=1, fwd_pe=-2)
    assert len(r["flag_notes"]) == len(r["flags"])
    assert all(r["flag_notes"])                 # every flag has human text

def test_confidence_needs_fscore():
    assert d(80, f_score=None)["confidence"] != "high"
    assert d(80, f_score=7)["confidence"] == "high"
