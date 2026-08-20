"""持仓止损提醒单元测试"""
from modules.stop_loss_monitor import check_stop_loss, stop_loss_from_df


def _holdings():
    return {
        "positions": {
            "600036": {"shares": 200, "avg_cost": 40.0},   # 现价38 → -5% → 触发
            "601398": {"shares": 200, "avg_cost": 25.0},   # 现价24.5 → -2% → 正常
            "600900": {"shares": 100, "avg_cost": 20.0},   # 无现价 → 成本 → 正常
        },
        "cash": 0.0,
    }


def test_trigger_when_drawdown_exceeds():
    price_map = {"600036": 38.0, "601398": 24.5}
    r = check_stop_loss(holdings=_holdings(), price_map=price_map, stop_loss_pct=5.0)
    by_code = {x["code"]: x for x in r["rows"]}
    assert by_code["600036"]["verdict"] == "止损提醒"
    assert by_code["600036"]["drawdown_pct"] == 5.0
    assert by_code["601398"]["verdict"] == "正常"
    assert len(r["triggers"]) == 1
    assert "600036" in r["triggers"][0]


def test_no_price_no_trigger():
    r = check_stop_loss(holdings=_holdings(), price_map={"600036": 38.0})
    by_code = {x["code"]: x for x in r["rows"]}
    # 600900 无现价 → 用成本价, drawdown=0, 不触发
    assert by_code["600900"]["verdict"] == "正常"
    assert by_code["600900"]["price"] == 20.0


def test_default_uses_config_pct():
    r = check_stop_loss(holdings=_holdings(), price_map={"600036": 38.0, "601398": 23.5})
    # 601398: (25-23.5)/25=6% 若默认5%则触发
    assert r["stop_loss_pct"] > 0


def test_stop_loss_from_df():
    import pandas as pd
    df = pd.DataFrame([{"code": "600036", "price": 38.0},
                       {"code": "601398", "price": 24.5}])
    r = stop_loss_from_df(df, holdings=_holdings())
    by_code = {x["code"]: x for x in r["rows"]}
    assert by_code["600036"]["verdict"] == "止损提醒"


def test_empty_holdings_ok():
    r = check_stop_loss(holdings={"positions": {}, "cash": 0.0})
    assert r["ok"] is True
    assert r["rows"] == []