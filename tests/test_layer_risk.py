import pandas as pd

from modules.layer_risk import (
    build_layer_risk_report,
    industry_collapse_loss,
    monte_carlo_p95_drawdown,
    single_day_loss,
)


def _rows(vols=(0.2, 0.25), weights=(40.0, 40.0), layer="A"):
    return [
        {"code": "600900", "name": "长江电力", "weight": weights[0], "vol": vols[0] * 100, "industry": "电力", "_layer": layer},
        {"code": "601398", "name": "工商银行", "weight": weights[1], "vol": vols[1] * 100, "industry": "银行", "_layer": layer},
    ]


def test_single_day_loss_uses_layer_weights():
    rows = _rows()
    assert single_day_loss(rows, -10.0) == -8.0
    assert single_day_loss(rows, -20.0) == -16.0


def test_industry_collapse_loss_takes_worst_industry():
    rows = _rows()
    assert industry_collapse_loss(rows) == -40.0


def test_monte_carlo_requires_real_vol():
    rows = _rows()
    p95 = monte_carlo_p95_drawdown(rows)
    assert p95 is not None and p95 > 0


def test_hk_placeholder_vol_is_insufficient():
    df = pd.DataFrame([
        {"layer": "HK", "code": "00002", "name": "中电控股", "weight": 40.0, "vol": 20.0},
        {"layer": "HK", "code": "01398", "name": "工商银行(H)", "weight": 40.0, "vol": 20.0},
    ])
    layers = {"HK": {"sequence": 2, "invested_cap": 80, "max_drawdown": 10}}
    report = build_layer_risk_report(df, layers)
    assert report[0]["verdict"] == "数据不足"


def test_report_flags_over_limit_with_high_vol():
    df = pd.DataFrame([
        {"layer": "A", "code": "600900", "name": "长江电力", "weight": 40.0, "vol": 100.0},
        {"layer": "A", "code": "601398", "name": "工商银行", "weight": 40.0, "vol": 100.0},
    ])
    layers = {"A": {"sequence": 1, "invested_cap": 80, "max_drawdown": 10}}
    report = build_layer_risk_report(df, layers)
    assert report[0]["verdict"] == "超线"
