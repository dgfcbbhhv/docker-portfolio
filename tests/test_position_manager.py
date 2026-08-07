"""仓位管理规则单元测试"""
import modules.position_manager as pm


def _row(code="600036", name="招商银行", group="A股持仓", weight=5.0,
          target=12.0, lishing=80.0, dy=4.5, price=38.0, flags=""):
    return {
        "code": code, "name": name, "group": group, "weight": weight,
        "kelly_portfolio_adj": target, "lishing": lishing, "dy": dy,
        "price": price, "data_flags": flags,
    }


def test_addable_when_rule_gap_ok():
    rows = [_row()]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金"})
    e = plan["entries"][0]
    assert e["verdict"] == "可加仓"
    assert e["gap_ratio"] == 7.0
    assert "分红再投入" in e["div_reinvest"]


def test_stale_and_tier_pause():
    rows = [_row(), _row(code="601398", name="工商银行")]
    plan = pm.build_position_plan(
        rows, tier_by_code={"600036": "黄金", "601398": "警戒"},
        freshness_codes={"600036"})
    assert plan["entries"][0]["verdict"] == "暂停"
    assert plan["entries"][1]["verdict"] == "暂停"


def test_single_limit_and_zero_target():
    rows = [
        _row(code="600900", name="长江电力", weight=30.0, target=35.0),
        _row(code="601088", name="中国神华", weight=5.0, target=0.0),
    ]
    plan = pm.build_position_plan(rows, tier_by_code={"600900": "黄金", "601088": "黄金"},
                                  rules={"maxpos": 30.0})
    verdicts = {e["code"]: e["verdict"] for e in plan["entries"]}
    assert verdicts["600900"] == "持有"
    assert verdicts["601088"] == "持有"


def test_red_flag_stops():
    rows = [_row(lishing=0.0)]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金"})
    assert plan["entries"][0]["verdict"] == "停止加仓"


def test_summary_and_layers():
    rows = [_row(weight=30.0, target=25.0), _row(code="601398", name="工商银行", weight=20.0, target=32.0)]
    plan = pm.build_position_plan(rows, tier_by_code={"600036": "黄金", "601398": "白银"})
    s = plan["summary"]
    assert s["total_current"] == 50.0
    assert s["cash_ratio"] == 50.0
    assert s["addable"] == 1
    assert s["over_single_limit"] == 1
    a_layer = next(l for l in plan["layers"] if l["layer"] == "A")
    assert a_layer["current_ratio"] == 50.0
    assert a_layer["status"] == "正常"
