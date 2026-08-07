"""组合风险基础计算与压力适配器测试"""
import pandas as pd

from modules.risk import (
    single_day_loss,
    industry_collapse_loss,
    vol_is_usable,
    monte_carlo_p95_drawdown,
)
from modules.stress import single_day, industry_collapse


def _rows(vols=(20.0, 25.0), weights=(40.0, 40.0), layer="A"):
    return [
        {"code": "600900", "weight": weights[0], "vol": vols[0], "industry": "电力", "_layer": layer},
        {"code": "601398", "weight": weights[1], "vol": vols[1], "industry": "银行", "_layer": layer},
    ]


def test_single_day_loss_matches_stress_adapter():
    rows = _rows()
    weights = {"600900": 40.0, "601398": 40.0}
    direct = single_day_loss(rows, -10.0)
    stress = single_day(["600900", "601398"], weights, drops=[-0.10])[0]
    assert direct == stress["impact"] == -8.0


def test_industry_collapse_matches_stress_adapter():
    rows = _rows()
    weights = {"600900": 40.0, "601398": 40.0}
    direct = industry_collapse_loss(rows)
    stress = industry_collapse(weights)[0]
    assert direct == stress["impact"] == -40.0


def test_placeholder_vol_unusable_for_hk():
    assert not vol_is_usable(20.0, "HK")
    assert vol_is_usable(25.0, "HK")
    assert vol_is_usable(20.0, "A")


def test_monte_carlo_requires_real_vol():
    p95 = monte_carlo_p95_drawdown(_rows())
    assert p95 is not None and p95 > 0
    assert monte_carlo_p95_drawdown(_rows(vols=(20.0, 20.0), layer="HK")) is None
