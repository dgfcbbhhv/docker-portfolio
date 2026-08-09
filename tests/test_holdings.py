"""真实持仓接入层测试。"""
import pytest

import modules.holdings as h


def test_get_actual_holdings_empty_when_no_positions(monkeypatch):
    monkeypatch.setattr("modules.actual_holdings.get_actual_holdings", lambda *a, **k: {})
    monkeypatch.setattr("modules.paper_trader._get_paper_portfolio",
                        lambda: {"capital": 1e6, "cash": 1e6, "positions": {}})
    assert h.get_actual_holdings() == {}


def test_get_actual_holdings_parses_positions(monkeypatch):
    monkeypatch.setattr("modules.actual_holdings.get_actual_holdings", lambda *a, **k: {})
    monkeypatch.setattr("modules.paper_trader._get_paper_portfolio",
                        lambda: {"capital": 1e6, "cash": 2e5,
                                 "positions": {"600036": {"shares": 100, "avg_cost": 30},
                                               "000001": {"shares": 0, "avg_cost": 11}}})
    out = h.get_actual_holdings()
    assert "600036" in out["positions"]
    assert "000001" not in out["positions"]  # 0 股忽略
    assert out["cash"] == 2e5


def test_market_value_and_ratio():
    pos = {"A": {"shares": 100, "avg_cost": 10},
           "B": {"shares": 50, "avg_cost": 20}}
    total, per = h.market_value(pos, lambda c: {"A": 12.0, "B": 24.0}[c])
    assert total == 100 * 12.0 + 50 * 24.0
    assert per["A"] == 1200.0
    # 无现金：占比 = 市值/总市值
    ratio = h.market_value_ratio("A", pos, 0, per, total)
    assert ratio is not None
    assert abs(ratio - 1200.0 / total * 100.0) < 0.01


def test_market_value_ratio_with_cash():
    pos = {"A": {"shares": 100, "avg_cost": 10}}
    total, per = h.market_value(pos, lambda c: 10.0)
    ratio = h.market_value_ratio("A", pos, 5000, per, total)
    assert abs(ratio - round(1000.0 / 6000.0 * 100.0, 1)) < 0.01


def test_market_value_ratio_unknown_code_is_none():
    total, per = h.market_value({"A": {"shares": 100, "avg_cost": 10}}, lambda c: 10.0)
    assert h.market_value_ratio("Z", {"A": {"shares": 100}}, 0, per, total) is None