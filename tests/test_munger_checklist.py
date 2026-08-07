"""芒格清单引擎单元测试"""
import pandas as pd

from modules.munger_checklist import build_checklist, _score_trend


def _row(**over):
    r = {
        "code": "600900", "name": "长江电力", "group": "A股持仓", "layer": "A",
        "ratio": 25.0, "price": 25.0, "chg": -2.0, "dy": 3.4, "roe": 15.0,
        "lishing": 80.0, "lishing_flags": "",
        "kelly_final": 12.0, "kelly_portfolio_adj": 20.0, "p_signal": "buy",
    }
    r.update(over)
    return r


def _df():
    return pd.DataFrame([
        {"code": "600900", "name": "长江电力", "group": "A股持仓", "chg": -2.0,
         "lishing": 80.0, "kelly_final": 12.0, "dy": 3.4},
        {"code": "601398", "name": "工商银行", "group": "A股持仓", "chg": -1.0,
         "lishing": 76.0, "kelly_final": 15.0, "dy": 3.8},
    ])


def test_red_line_forces_sell():
    r = build_checklist("600900", _row(lishing=0.0), df=_df())
    assert r["q6"]["action"] == "sell"
    assert "红线" in r["q1"]["conclusion"]
    assert r["q6"]["reason"]


def test_oversell_high_score_adds():
    r = build_checklist("600900", _row(chg=-9.0, lishing=82.0, kelly_portfolio_adj=20.0), df=_df())
    assert r["q6"]["action"] == "add"
    assert r["q6"]["quantity_pct"] is not None


def test_high_score_without_oversell_holds():
    r = build_checklist("600900", _row(chg=-2.0, lishing=82.0), df=_df())
    assert r["q6"]["action"] == "hold"


def test_low_score_reduces():
    r = build_checklist("600900", _row(lishing=55.0), df=_df())
    assert r["q6"]["action"] == "sell"


def test_freshness_issue_freezes():
    r = build_checklist("600900", _row(), df=_df(), freshness_issues=["spot 过期"])
    assert r["q6"]["action"] == "freeze"


def test_counter_evidence_always_present():
    r = build_checklist("600900", _row(), df=_df())
    assert r["counter_evidence"]
    assert "错了" in r["counter_evidence"]


def test_better_candidate_detected():
    r = build_checklist("600900", _row(lishing=60.0), df=_df(),
                        buy_scores={"600900": 60, "601398": 80})
    assert r["q5"]["action"] == "换仓候选"
    assert "601398" in r["q5"]["conclusion"]


def test_score_trend():
    hist = {"600900": [{"date": "2026-07-01", "score": 90.0},
                       {"date": "2026-08-03", "score": 80.0}]}
    latest, d, before, delta = _score_trend(hist, "600900")
    assert latest == 80.0
    assert before == 90.0
    assert delta == -10.0