"""组合级回撤熔断单元测试"""
import os
import pytest

from modules import drawdown_guard as dg
from modules.paths import data_dir

_TMP = os.path.join(data_dir(), "portfolio_drawdown_test.json")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(dg, "DRAWDOWN_FILE", _TMP)
    if os.path.exists(_TMP):
        os.remove(_TMP)
    yield
    if os.path.exists(_TMP):
        os.remove(_TMP)


def test_snapshot_builds_peak_and_drawdown():
    r1 = dg.record_snapshot(1000000.0, "2026-08-01")
    assert r1["recorded"] is True
    assert r1["peak"] == 1000000.0
    assert r1["drawdown_pct"] == 0.0
    assert r1["frozen"] is False
    # 同日覆盖不重复记录
    dg.record_snapshot(1000000.0, "2026-08-01")
    assert len(dg.current_drawdown()["dates"]) == 1
    # 下跌 12% → 熔断
    r2 = dg.record_snapshot(880000.0, "2026-08-02")
    assert r2["drawdown_pct"] == 12.0
    assert r2["frozen"] is True
    assert dg.is_frozen() is True


def test_recovery_unfreezes():
    dg.record_snapshot(1000000.0, "2026-08-01")
    dg.record_snapshot(880000.0, "2026-08-02")
    assert dg.is_frozen() is True
    # 反弹回补 → 不再熔断
    dg.record_snapshot(960000.0, "2026-08-03")
    assert dg.is_frozen() is False
    # 但峰值保留, 回撤按新峰值算
    dd = dg.current_drawdown()
    assert dd["peak"] == 1000000.0


def test_threshold_from_layers():
    th = dg.threshold()
    assert th >= 10.0


def test_record_invalid_value():
    r = dg.record_snapshot(0)
    assert r["recorded"] is False


def test_current_without_history():
    c = dg.current_drawdown()
    assert c["has_history"] is False
    assert dg.is_frozen() is False


def test_position_plan_frozen_blocks_add(monkeypatch):
    """熔断时停止一切建仓/加仓。"""
    import modules.position_manager as pm
    monkeypatch.setattr(pm, "_resolve_actual_ratios", lambda *a, **k: {})
    rows = [{"code": "600036", "name": "招商银行", "group": "A股持仓", "weight": 10.0,
             "kelly_portfolio_adj": 15.0, "lishing": 82.0, "dy": 4.5, "price": 38.0, "data_flags": ""}]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金"},
                                  actual_holdings={"positions": {}, "cash": 0}, frozen=True)
    e = plan["entries"][0]
    assert e["verdict"] == "暂停"
    assert "熔断" in e["reasons"]