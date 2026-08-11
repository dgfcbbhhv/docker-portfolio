"""推送建议 → 模拟盘自动执行桥测试。"""
import os

import pytest

os.environ.setdefault("CONFIG_PATH", "config.json")


def _fake_picks():
    return {
        "date": "2026-08-10",
        "picks": [
            {"code": "600000", "name": "浦发银行", "category": "可建仓", "held": False,
             "market": "A", "price": 10.0, "kelly_final": 15.0},
            {"code": "000001", "name": "平安银行", "category": "可加仓", "held": True,
             "market": "A", "price": 12.0, "kelly_final": 0.0},
            {"code": "00700", "name": "腾讯控股", "category": "可加仓", "held": False,
             "market": "HK", "price": 400.0, "kelly_final": 5.0},
            {"code": "AAPL", "name": "苹果", "category": "可建仓", "held": False,
             "market": "US", "price": 200.0, "kelly_final": 5.0},
        ],
    }


@pytest.fixture(autouse=True)
def _isolate_paper(tmp_path, monkeypatch):
    import modules.paper_common as pc
    import modules.paper_executor as pe
    import modules.paper_picks as pp
    target = str(tmp_path / "portfolio.json")
    monkeypatch.setattr(pc, "PAPER_DIR", str(tmp_path))
    monkeypatch.setattr(pc, "PORTFOLIO_FILE", target)
    monkeypatch.setattr(pe, "PORTFOLIO_FILE", target)
    monkeypatch.setattr(pp, "PORTFOLIO_FILE", target)


def test_bridge_creates_a_orders_skips_hk_us():
    from modules.paper_picks import apply_picks_to_paper
    res = apply_picks_to_paper(_fake_picks())
    assert res["disabled"] is False
    created = res["created"]
    assert "600000" in created
    assert "000001" in created
    assert "00700" not in created
    assert "AAPL" not in created

    from modules.paper_executor import _get_paper_portfolio
    pf = _get_paper_portfolio()
    orders = {o["code"]: o for o in pf["pending_orders"] if o.get("source") == "daily_picks"}
    assert orders["600000"]["action"] == "buy"
    assert orders["600000"]["shares"] >= 100
    assert orders["000001"]["action"] == "buy"


def test_bridge_idempotent():
    from modules.paper_picks import apply_picks_to_paper
    apply_picks_to_paper(_fake_picks())
    apply_picks_to_paper(_fake_picks())
    from modules.paper_executor import _get_paper_portfolio
    pf = _get_paper_portfolio()
    codes = [o["code"] for o in pf["pending_orders"] if o.get("source") == "daily_picks"]
    assert len(codes) == len(set(codes))


def test_bridge_can_disable(monkeypatch):
    import modules.paper_picks as pp
    monkeypatch.setattr(pp, "PAPER_TRADE", {"auto_follow_picks": False})
    res = pp.apply_picks_to_paper(_fake_picks())
    assert res.get("disabled") is True


def test_bridge_reduce_sells_position():
    from modules.paper_picks import apply_picks_to_paper
    picks = _fake_picks()
    picks["picks"].append({"code": "601398", "name": "工商银行", "category": "减仓",
                          "held": True, "market": "A", "price": 5.0, "kelly_final": 0.0})
    from modules.paper_executor import _get_paper_portfolio
    pf = _get_paper_portfolio()
    pf["positions"]["601398"] = {"shares": 1000, "avg_cost": 5.0}
    from modules.paper_common import PORTFOLIO_FILE, _save_json
    _save_json(PORTFOLIO_FILE, pf)

    res = apply_picks_to_paper(picks)
    assert "601398" in res["created"]
    pf = _get_paper_portfolio()
    order = next(o for o in pf["pending_orders"]
                 if o.get("code") == "601398" and o.get("source") == "daily_picks")
    assert order["action"] == "sell"
    assert order["shares"] == 500


def test_signal_accepts_float_pctls(monkeypatch):
    """fetch_price_percentiles 返回 float 时不应崩溃（回归测试）。"""
    from modules.paper_trader import _compute_signal_for_stock

    monkeypatch.setattr("modules.data.market.fetch_sma", lambda *a, **kw: 10.0)
    r = _compute_signal_for_stock("600036", {"eps": 1.0, "bps": 8.0}, 10.0, 0.2, 60.0, mkt="A")
    assert r["code"] == "600036"
    assert r["buy_score"] is not None

    r_us = _compute_signal_for_stock("AAPL", {}, 200.0, 0.3, 55.0, mkt="US", us_extra={})
    assert r_us["code"] == "AAPL"