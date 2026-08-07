"""Kelly 组合级总仓位约束单元测试"""
from modules.kelly import apply_portfolio_cap


def test_no_scale_when_under_cap():
    rows = [
        {"code": "A", "kelly_factor_adj": 20.0},
        {"code": "B", "kelly_factor_adj": 30.0},
    ]
    out, total, scale = apply_portfolio_cap(rows, max_total=80.0)
    assert scale == 1.0
    assert total == 50.0
    assert out[0]["kelly_portfolio_adj"] == 20.0
    assert out[1]["kelly_portfolio_adj"] == 30.0


def test_scales_when_over_cap():
    rows = [
        {"code": "A", "kelly_factor_adj": 60.0},
        {"code": "B", "kelly_factor_adj": 40.0},
    ]
    out, total, scale = apply_portfolio_cap(rows, max_total=80.0)
    assert total == 100.0
    assert scale == 0.8
    assert out[0]["kelly_portfolio_adj"] == 48.0
    assert out[1]["kelly_portfolio_adj"] == 32.0


def test_zero_rows():
    out, total, scale = apply_portfolio_cap([], max_total=80.0)
    assert total == 0.0
    assert scale == 1.0
    assert out == []
