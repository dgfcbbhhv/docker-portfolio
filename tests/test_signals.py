"""买卖信号引擎 v3 单元测试"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.signals import evaluate_buy, evaluate_sell, format_fix_summary


class TestEvaluateBuy:
    def test_strong_buy(self):
        r = evaluate_buy(
            lishing_score=85, pe_pctl=20, pb_pctl=25,
            div_yield=4.5, price_above_ma20=True, ma20_above_ma60=True,
            kelly_final=8.0, p_signal="buy", vol_adj_factor=1.2,
            red_line_triggered=False, threshold=70,
        )
        assert r["signal"] == "strong_buy"
        assert r["score"] >= 80

    def test_buy(self):
        r = evaluate_buy(
            lishing_score=75, pe_pctl=25, pb_pctl=35,
            div_yield=3.5, price_above_ma20=True, ma20_above_ma60=False,
            kelly_final=3.0, p_signal="watch", vol_adj_factor=0.9,
            red_line_triggered=False, threshold=70,
        )
        assert r["signal"] == "buy"
        assert 65 <= r["score"] < 80

    def test_cautious(self):
        r = evaluate_buy(
            lishing_score=75, pe_pctl=25, pb_pctl=35,
            div_yield=3.5, price_above_ma20=False, ma20_above_ma60=False,
            kelly_final=-1.0, p_signal="watch", vol_adj_factor=0.9,
            red_line_triggered=False, threshold=70,
        )
        assert r["signal"] == "cautious"
        assert 45 <= r["score"] < 65

    def test_avoid_low_score(self):
        r = evaluate_buy(
            lishing_score=40, pe_pctl=60, pb_pctl=65,
            div_yield=1.0, price_above_ma20=False, ma20_above_ma60=False,
            kelly_final=-8.0, p_signal="sell", vol_adj_factor=0.5,
            red_line_triggered=False, threshold=70,
        )
        assert r["signal"] == "avoid"
        assert r["score"] < 45

    def test_red_line_triggers_avoid(self):
        r = evaluate_buy(red_line_triggered=True)
        assert r["signal"] == "avoid"
        assert r["score"] == 0

    def test_reason_not_empty(self):
        r = evaluate_buy(
            lishing_score=80, pe_pctl=20, pb_pctl=25,
            div_yield=4.0, price_above_ma20=True, ma20_above_ma60=True,
            kelly_final=5.0, p_signal="buy", vol_adj_factor=1.0,
            red_line_triggered=False,
        )
        assert r["reason"]
        assert len(r["factors"]) == 6


class TestEvaluateSell:
    def test_strong_hold(self):
        r = evaluate_sell(
            current_score=85, buy_score=80, high_250d=85,
            div_yield=4.0, kelly_final=6.0, p_signal="buy",
            price_above_ma20=True, ma20_above_ma60=True,
            red_line_triggered=False,
        )
        assert r["signal"] == "strong_hold"
        assert r["score"] >= 75

    def test_hold(self):
        r = evaluate_sell(
            current_score=75, buy_score=80, high_250d=82,
            div_yield=2.5, kelly_final=-2.0, p_signal="watch",
            price_above_ma20=True, ma20_above_ma60=False,
            red_line_triggered=False,
        )
        assert r["signal"] == "hold"
        assert 55 <= r["score"] < 75

    def test_reduce(self):
        r = evaluate_sell(
            current_score=65, buy_score=80, high_250d=82,
            div_yield=2.0, kelly_final=-2.0, p_signal="watch",
            price_above_ma20=False, ma20_above_ma60=False,
            red_line_triggered=False,
        )
        assert r["signal"] == "reduce"
        assert 35 <= r["score"] < 55

    def test_sell(self):
        r = evaluate_sell(
            current_score=40, buy_score=80, high_250d=82,
            div_yield=1.0, kelly_final=-10.0, p_signal="sell",
            price_above_ma20=False, ma20_above_ma60=False,
            red_line_triggered=False,
        )
        assert r["signal"] == "sell"
        assert r["score"] < 35

    def test_red_line(self):
        r = evaluate_sell(red_line_triggered=True)
        assert r["signal"] == "sell"
        assert r["score"] == 0

    def test_reason_not_empty(self):
        r = evaluate_sell(
            current_score=80, buy_score=80,
            div_yield=4.0, kelly_final=5.0, p_signal="buy",
            price_above_ma20=True, ma20_above_ma60=True,
            red_line_triggered=False,
        )
        assert r["reason"]
        assert len(r["factors"]) == 5


class TestFormatFixSummary:
    def test_with_fixes(self):
        factors = [
            {"name": "估值(PE/PB分位)", "score": 5, "max": 20, "detail": "PE分位=55%"},
            {"name": "李氏评分", "score": 25, "max": 25, "detail": "80分"},
        ]
        result = format_fix_summary(factors)
        assert result is not None
        assert "估值" in result

    def test_all_pass(self):
        factors = [
            {"name": "李氏评分", "score": 25, "max": 25, "detail": "80分"},
        ]
        result = format_fix_summary(factors)
        assert result is None
