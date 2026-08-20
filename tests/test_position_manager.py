"""仓位管理规则单元测试"""
import modules.position_manager as pm

_EMPTY_HOLDINGS = {"positions": {}, "cash": 0.0}


def _row(code="600036", name="招商银行", group="A股持仓", weight=5.0,
          target=12.0, lishing=80.0, dy=4.5, price=38.0, flags=""):
    return {
        "code": code, "name": name, "group": group, "weight": weight,
        "kelly_portfolio_adj": target, "lishing": lishing, "dy": dy,
        "price": price, "data_flags": flags,
    }


def _plan_target(rows, **kw):
    """显式锁定 target 口径（空真实持仓），避免真实数据文件干扰规则测试。"""
    kw.setdefault("actual_holdings", _EMPTY_HOLDINGS)
    return pm.build_position_plan(rows, **kw)


def test_addable_when_rule_gap_ok():
    rows = [_row()]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"})
    e = plan["entries"][0]
    assert e["verdict"] == "可加仓"
    assert e["gap_ratio"] == 7.0
    assert "分红再投入" in e["div_reinvest"]


def test_stale_and_tier_pause():
    rows = [_row(), _row(code="601398", name="工商银行")]
    plan = _plan_target(
        rows, tier_by_code={"600036": "黄金", "601398": "警戒"},
        freshness_codes={"600036"})
    assert plan["entries"][0]["verdict"] == "暂停"
    assert plan["entries"][1]["verdict"] == "暂停"


def test_single_limit_and_zero_target():
    rows = [
        _row(code="600900", name="长江电力", weight=30.0, target=35.0),
        _row(code="601088", name="中国神华", weight=5.0, target=0.0),
    ]
    plan = _plan_target(rows, tier_by_code={"600900": "黄金", "601088": "黄金"},
                        rules={"maxpos": 30.0})
    verdicts = {e["code"]: e["verdict"] for e in plan["entries"]}
    assert verdicts["600900"] == "持有"
    assert verdicts["601088"] == "持有"


def test_red_flag_stops():
    rows = [_row(lishing=0.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"})
    assert plan["entries"][0]["verdict"] == "停止加仓"


def test_summary_and_layers():
    rows = [_row(weight=30.0, target=25.0), _row(code="601398", name="工商银行", weight=20.0, target=32.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金", "601398": "白银"})
    s = plan["summary"]
    assert s["total_current"] == 50.0
    assert s["cash_ratio"] == 50.0
    assert s["addable"] == 1
    assert s["over_single_limit"] == 1
    a_layer = next(l for l in plan["layers"] if l["layer"] == "A")
    assert a_layer["current_ratio"] == 50.0
    assert a_layer["status"] == "正常"


def test_risk_budget_caps_add_when_capital_given():
    """给定 capital 时, add_pct 被单笔风险预算封顶(ladder 建议 > 预算时)。"""
    rows = [_row(weight=5.0, target=20.0, lishing=82.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"}, capital=100000.0)
    e = plan["entries"][0]
    assert e["verdict"] == "可加仓"
    # 10万 × 1.5% / 5% 止损 = 3万 = 30% 总资产封顶
    assert e["risk_cap_pct"] == 30.0
    assert e["risk_budget"]["max_value"] == 30000.0
    # ladder 建议 gap 的一半 = 7.5% > 30%? 否: target20-current5=15, 首层=7.5
    assert e["add_pct"] <= e["risk_cap_pct"]


def test_risk_budget_disabled_without_capital():
    """未提供 capital 时不启用风险预算, 不报错。"""
    rows = [_row(weight=5.0, target=20.0, lishing=82.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"})
    e = plan["entries"][0]
    assert e["verdict"] == "可加仓"
    assert e["risk_budget"] is None
    assert e["risk_cap_pct"] is None


def test_add_count_from_cost_batches(monkeypatch):
    """cost_tiers.json 批次数作为已加仓次数, 按真实层级放量。"""
    import modules.cost_tier as ct
    monkeypatch.setattr(ct, "get_stock",
                        lambda code: {"batches": [{"shares": 100}, {"shares": 100}]})
    rows = [_row(weight=5.0, target=20.0, lishing=82.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"})
    e = plan["entries"][0]
    assert e["add_count"] == 2
    # 已加2次 → 第3层, 放量 = gap*0.2
    assert e["add_step"] == 3


def test_add_count_zero_no_batches(monkeypatch):
    """无批次时视为首层。"""
    import modules.cost_tier as ct
    monkeypatch.setattr(ct, "get_stock", lambda code: {"batches": []})
    rows = [_row(weight=5.0, target=20.0, lishing=82.0)]
    plan = _plan_target(rows, tier_by_code={"600036": "黄金"})
    e = plan["entries"][0]
    assert e["add_count"] == 0
    assert e["add_step"] == 1


def _real_holdings():
    return {
        "positions": {
            "600036": {"shares": 200, "avg_cost": 38.58},
            "601398": {"shares": 200, "avg_cost": 25.61},
        },
        "cash": 0.0,
        "_source": "broker",
    }


def test_actual_ratio_overrides_weight():
    """有真实持仓时 current 用真实市值占比, 而非目标 weight。"""
    rows = [
        _row(code="600036", weight=10.0, target=12.0, price=38.0),
        _row(code="601398", weight=20.0, target=25.0, price=25.61),
    ]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金", "601398": "黄金"},
                                  actual_holdings=_real_holdings())
    assert plan["summary"]["ratio_source"] == "actual"
    by_code = {e["code"]: e for e in plan["entries"]}
    # 600036: 200*38=7600; 601398: 200*25.61=5122; total=12722
    # 600036 占比 = 7600/12722*100 = 59.7
    assert by_code["600036"]["current_source"] == "actual"
    assert by_code["600036"]["current_ratio"] == 59.7
    assert by_code["601398"]["current_ratio"] == 40.3
    # 超限: 默认 maxpos=30, 60%/40% 都超 → 持有而非可加仓
    assert by_code["600036"]["verdict"] == "持有"
    assert "减仓" in by_code["600036"]["reasons"]
    assert by_code["601398"]["verdict"] == "持有"


def test_actual_ratio_falls_back_to_target():
    """无真实持仓时回退目标权重口径。"""
    rows = [_row(weight=5.0, target=12.0)]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金"},
                                  actual_holdings={"positions": {}, "cash": 0.0})
    assert plan["summary"]["ratio_source"] == "target"
    e = plan["entries"][0]
    assert e["current_source"] == "target"
    assert e["current_ratio"] == 5.0
    assert e["verdict"] == "可加仓"


def test_actual_ratio_capital_from_market_value():
    """capital 由真实市值+现金估算, 供风险预算使用。"""
    rows = [_row(code="600036", weight=10.0, target=12.0, price=38.0)]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金"},
                                  actual_holdings=_real_holdings(), capital=12722.0)
    e = plan["entries"][0]
    assert e["verdict"] == "持有"  # 超限, 不触发风险预算逻辑
