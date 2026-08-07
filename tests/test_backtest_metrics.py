"""回测口径修正单元测试"""
import numpy as np
import pandas as pd
import pytest

from modules.backtest import backtest_portfolio


def _fake_history(code, years=5, mkt="A"):
    dates = pd.bdate_range(end=pd.Timestamp("2024-12-31"), periods=500)
    close = np.linspace(100.0, 121.0, len(dates))
    return pd.DataFrame({"close": close}, index=dates)


def test_annualization_uses_actual_span(monkeypatch):
    monkeypatch.setattr("modules.backtest.fetch_history", _fake_history)
    result = backtest_portfolio(["600900"], {"600900": 1.0},
                                years=5, cost=0.001)
    metrics = result["metrics"]

    nav = result["nav"]
    years_actual = int((nav.index[-1] - nav.index[0]).days) / 365.25
    growth = nav.iloc[-1] / nav.iloc[0]
    expected_annual = (growth ** (1 / years_actual)) - 1
    assert metrics["annual_return"] == round(expected_annual * 100, 2)

    assert metrics["turnover"] == 24.0
    assert metrics["annual_cost"] == 2.4
    assert metrics["annual_return_net"] == round(
        (expected_annual - 0.024) * 100, 2)


def test_metrics_keys_present(monkeypatch):
    monkeypatch.setattr("modules.backtest.fetch_history", _fake_history)
    result = backtest_portfolio(["600900"], {"600900": 1.0}, years=1)
    for key in ("total_return", "annual_return", "sharpe", "max_drawdown",
                "annual_vol", "win_rate", "turnover", "annual_cost",
                "annual_return_net"):
        assert key in result["metrics"]
