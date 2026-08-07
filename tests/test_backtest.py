"""回测引擎单元测试"""
import datetime

import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.backtest import backtest_scenarios, fetch_histories


@pytest.fixture
def fake_history(monkeypatch):
    """注入确定性离线行情，避免 CI 上依赖东财/腾讯网络。"""
    idx = pd.date_range("2023-01-02", periods=252, freq="B")
    df = pd.DataFrame({"close": pd.Series(
        range(100, 100 + len(idx)), index=idx, dtype=float)})
    df.index.name = "date"
    monkeypatch.setattr("modules.backtest.fetch_history",
                        lambda code, years=5, mkt="A": df)
    return df


class TestBacktestScenarios:
    def test_basic_backtest(self, fake_history):
        port_a = {"600900": {"name": "长江电力", "ratio": 50}}
        port_hk = {}
        fins = {"600900": {"roe": 15}}
        result = backtest_scenarios(port_a, port_hk, fins, years=1)
        assert "bear" in result
        assert "base" in result
        assert "bull" in result

    def test_metrics_structure(self, fake_history):
        port_a = {"600900": {"name": "长江电力", "ratio": 50}}
        port_hk = {}
        fins = {"600900": {"roe": 15}}
        result = backtest_scenarios(port_a, port_hk, fins, years=1)
        metrics = result["base"]["metrics"]
        assert "total_return" in metrics
        assert "annual_return_net" in metrics
        assert "sharpe" in metrics
        assert "max_drawdown" in metrics
        assert "win_rate" in metrics


class TestFetchHistories:
    def test_fetch_returns_dict(self, fake_history):
        result = fetch_histories(["600900"], years=1)
        assert isinstance(result, dict)
