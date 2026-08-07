"""Kelly引擎单元测试"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.kelly import run as kelly_run


class TestKellyRun:
    def test_basic_calculation(self):
        result = kelly_run(
            price=10.0, pb=1.0, pe=15, roe=12,
            eps=0.5, debt=50, div=0.3, vol=0.2,
            frac=0.25, maxpos=30.0
        )
        assert "p_signal" in result
        assert "b_odds" in result
        assert "kelly_raw" in result
        assert "kelly_opt" in result
        assert "kelly_final" in result
        assert "dividend_yield" in result

    def test_p_range(self):
        result = kelly_run(10.0, 1.0, 15, 12, 0.5, 50, 0.3, 0.2)
        assert 0.05 <= result["p_signal"] <= 0.95

    def test_b_range(self):
        result = kelly_run(10.0, 1.0, 15, 12, 0.5, 50, 0.3, 0.2)
        assert 0.1 <= result["b_odds"] <= 10.0

    def test_kelly_opt_capped_at_maxpos(self):
        result = kelly_run(10.0, 0.5, 10, 20, 1.0, 30, 0.5, 0.1,
                          frac=0.5, maxpos=30.0)
        assert result["kelly_final"] <= 30.0

    def test_high_vol_reduces_kelly(self):
        low_vol = kelly_run(10.0, 1.0, 15, 12, 0.5, 50, 0.3, 0.1)
        high_vol = kelly_run(10.0, 1.0, 15, 12, 0.5, 50, 0.3, 0.5)
        assert high_vol["kelly_final"] <= low_vol["kelly_final"]

    def test_zero_price(self):
        result = kelly_run(0, 1.0, 15, 12, 0.5, 50, 0.3, 0.2)
        assert result["kelly_final"] >= 0

    def test_dividend_yield(self):
        result = kelly_run(10.0, 1.0, 15, 12, 0.5, 50, 0.5, 0.2)
        assert result["dividend_yield"] == 5.0
