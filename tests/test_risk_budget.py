"""单笔风险预算单元测试"""
from modules.risk_budget import risk_budget_per_add, effective_add_value
from modules.config import RISK_BUDGET


def test_defaults_from_config():
    b = risk_budget_per_add(100000)
    assert b["risk_pct"] == RISK_BUDGET["risk_pct"]
    assert b["stop_loss_pct"] == RISK_BUDGET["stop_loss_pct"]
    # 10万资产, 1.5%风险, 5%止损 → 最多投 3万
    assert b["max_value"] == 30000.0


def test_explicit_params():
    # 10万资产, 2%风险, 10%止损 → 2万
    b = risk_budget_per_add(100000, risk_pct=2.0, stop_loss_pct=10.0)
    assert b["max_value"] == 20000.0
    assert b["max_pct_of_assets"] == 20.0


def test_zero_assets():
    b = risk_budget_per_add(0)
    assert b["max_value"] == 0.0
    assert "跳过" in b["note"]


def test_bad_stop():
    b = risk_budget_per_add(100000, stop_loss_pct=0.0)
    assert b["max_value"] == 0.0
    assert "非法" in b["note"]


def test_effective_takes_min():
    # ladder 建议 5万 > 风险预算 3万 → 取 3万
    r = effective_add_value(50000, 100000)
    assert r["value"] == 30000.0
    assert r["capped_by_risk"] is True


def test_effective_not_capped_when_below():
    # ladder 建议 1万 < 预算 3万 → 取 1万, 不封顶
    r = effective_add_value(10000, 100000)
    assert r["value"] == 10000.0
    assert r["capped_by_risk"] is False


def test_effective_none_ladder():
    r = effective_add_value(None, 100000)
    assert r["value"] == 30000.0
    assert r["capped_by_risk"] is True