"""模拟盘真实成交假设与信号计算单元测试"""
import datetime

import modules.paper_trader as pt
import pytest


def _snap(code, price=10.0, buy_score=80, buy_signal="strong_buy",
          sell_score=100, sell_signal="hold", kelly=10.0):
    return {
        "code": code, "price": price,
        "buy_score": buy_score, "buy_signal": buy_signal,
        "sell_score": sell_score, "sell_signal": sell_signal,
        "kelly_final": kelly, "is_mock": False, "data_source": "eastmoney",
    }


def _pf(cash=1_000_000.0):
    return {
        "capital": 1_000_000.0,
        "cash": cash,
        "positions": {},
        "trades": [],
        "pending_orders": [],
        "skipped_orders": [],
    }


def test_a_limit_ratio():
    assert pt._a_limit_ratio("600036") == 0.10
    assert pt._a_limit_ratio("300001") == 0.20
    assert pt._a_limit_ratio("688001") == 0.20
    assert pt._a_limit_ratio("508098") == 0.30


def test_target_buy_shares_rounds_to_lot():
    pf = _pf()
    assert pt._target_buy_shares(_snap("600036", price=10.0, kelly=10.0), pf) == 10000


def test_create_pending_sell_order():
    pf = _pf()
    pf["positions"]["600036"] = {"shares": 500, "avg_cost": 10.0}
    snaps = [_snap("600036", price=10.0, buy_score=30, buy_signal="avoid",
                   sell_score=30, sell_signal="sell")]
    pf = pt._create_pending_orders(snaps, pf, "2026-08-04")
    assert len(pf["pending_orders"]) == 1
    assert pf["pending_orders"][0]["action"] == "sell"


def test_execute_buy_at_open_with_fee():
    pf = _pf()
    pf["pending_orders"] = [{
        "signal_date": "2026-08-03", "code": "600036", "action": "buy",
        "shares": 1000, "buy_score": 80, "buy_signal": "strong_buy",
        "kelly_pct": 10.0, "reason": "测试", "data_source": "eastmoney",
    }]
    bars = {"600036": {"open": 10.0, "close": 10.2, "high": 10.3, "low": 9.9,
                       "prev_close": 9.8, "suspended": False}}
    pf = pt._execute_pending_orders(pf, bars, datetime.date(2026, 8, 4))
    assert pf["positions"]["600036"]["shares"] == 1000
    assert pf["trades"][0]["fee"] == 10.0
    assert pf["trades"][0]["fill_status"] == "filled"
    assert pf["cash"] == 1_000_000 - 10000 - 10


def test_execute_skips_limit_up():
    pf = _pf()
    pf["pending_orders"] = [{
        "signal_date": "2026-08-03", "code": "600036", "action": "buy",
        "shares": 1000, "buy_score": 80, "buy_signal": "strong_buy",
        "kelly_pct": 10.0, "reason": "测试", "data_source": "eastmoney",
    }]
    bars = {"600036": {"open": 10.78, "close": 10.78, "high": 10.8, "low": 10.0,
                       "prev_close": 9.8, "suspended": False}}
    pf = pt._execute_pending_orders(pf, bars, datetime.date(2026, 8, 4))
    assert pf["positions"] == {}
    assert pf["skipped_orders"][0]["reason"] == "涨停无法买入"


def test_execute_skips_suspended():
    pf = _pf()
    pf["pending_orders"] = [{
        "signal_date": "2026-08-03", "code": "600036", "action": "buy",
        "shares": 1000, "buy_score": 80, "buy_signal": "strong_buy",
        "kelly_pct": 10.0, "reason": "测试", "data_source": "eastmoney",
    }]
    bars = {"600036": {"suspended": True}}
    pf = pt._execute_pending_orders(pf, bars, datetime.date(2026, 8, 4))
    assert pf["positions"] == {}
    assert pf["skipped_orders"][0]["reason"] == "停牌/无当日行情"


def test_weekend_keeps_pending():
    pf = _pf()
    order = {
        "signal_date": "2026-08-07", "code": "600036", "action": "buy",
        "shares": 1000, "buy_score": 80, "buy_signal": "strong_buy",
        "kelly_pct": 10.0, "reason": "测试", "data_source": "eastmoney",
    }
    pf["pending_orders"] = [order]
    pf = pt._execute_pending_orders(pf, {}, datetime.date(2026, 8, 8))
    assert pf["pending_orders"] == [order]
    assert pf["trades"] == []


def test_update_paper_portfolio_t1_open(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PORTFOLIO_FILE", str(tmp_path / "pf.json"))
    monkeypatch.setattr(pt, "SNAPSHOT_FILE", str(tmp_path / "snap.json"))
    monkeypatch.setattr(pt, "NAV_FILE", str(tmp_path / "nav.json"))
    monkeypatch.setattr(pt, "VALIDATION_FILE", str(tmp_path / "val.json"))
    # paper_executor 以 from-import 复制了 PORTFOLIO_FILE 常量, 需同步 mock
    from modules import paper_executor as pe
    monkeypatch.setattr(pe, "PORTFOLIO_FILE", str(tmp_path / "pf.json"))

    def fake_bar(code, date):
        return {"open": 10.1, "close": 10.3, "high": 10.4, "low": 10.0,
                "prev_close": 10.0, "suspended": False}

    # paper_executor 内部经 _fetch_execution_bars 调用本模块的 _fetch_a_bar_for_date, 需同步 mock
    monkeypatch.setattr(pe, "_fetch_a_bar_for_date", fake_bar)
    monkeypatch.setattr(pt, "_fetch_a_bar_for_date", fake_bar)

    snaps = [_snap("600036", price=10.0, kelly=10.0)]
    pf = pt.update_paper_portfolio(snaps, execution_date=datetime.date(2026, 8, 3))
    assert len(pf["pending_orders"]) == 1

    pf = pt.update_paper_portfolio([], execution_date=datetime.date(2026, 8, 4))
    assert pf["positions"]["600036"]["shares"] == 10000
    assert pf["trades"][0]["price"] == 10.1


def test_us_signal_uses_growth_scoring():
    us = pt._compute_signal_for_stock(
        "AAPL", {}, 200.0, 0.25, {"pe_pctl": 40, "pb_pctl": 60},
        mkt="US", us_extra={"spot": {"pe": 35, "pb": 8}, "div": {"ttm": 0.9}})
    assert us["buy_signal"] in ("strong_buy", "buy", "cautious", "avoid")
    assert us["buy_score"] >= 0
    assert us["roe"] is None


def test_validate_signals_with_benchmark(tmp_path, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(pt, "SNAPSHOT_FILE", str(tmp_path / "snap.json"))
    monkeypatch.setattr(pt, "VALIDATION_FILE", str(tmp_path / "val.json"))

    dates = [f"2026-07-{1 + i:02d}" for i in range(22)]
    snapshots = {}
    for d in dates:
        snapshots[d] = [_snap("600036", price=(10.0 if d == dates[0] else 11.0))]
    pt._save_json(pt.SNAPSHOT_FILE, snapshots)

    bench = pd.Series([100.0, 110.0], index=pd.to_datetime([dates[0], dates[1]]))
    monkeypatch.setattr(pt, "_fetch_benchmark_df", lambda market="A": bench)

    summary = pt.validate_signals()
    h5 = summary["by_horizon"]["5d"]["buy"]
    assert h5["avg_ret_pct"] > 0
    assert h5["avg_excess_ret_pct"] == pytest.approx(0.0, abs=0.01)
    assert h5["excess_win_rate"] == 0.0
    assert summary["details"][0]["excess_ret_pct"] is not None


def test_validate_signals_per_market_benchmark(tmp_path, monkeypatch):
    """验证按市场取对应基准：A/HK/US 信号各自用独立基准计算超额收益。"""
    import pandas as pd
    monkeypatch.setattr(pt, "SNAPSHOT_FILE", str(tmp_path / "snap.json"))
    monkeypatch.setattr(pt, "VALIDATION_FILE", str(tmp_path / "val.json"))

    dates = [f"2026-07-{1 + i:02d}" for i in range(22)]
    snapshots = {}
    for d in dates:
        base = 10.0 if d == dates[0] else 11.0
        snapshots[d] = [
            _snap("600036", price=base),   # A 股
            _snap("00700", price=base),    # 港股
            _snap("AAPL", price=base),     # 美股
        ]
    pt._save_json(pt.SNAPSHOT_FILE, snapshots)

    # 各市场基准覆盖全部 22 天：A 基准从 100 涨到 200，HK/US 从 100 涨到 110
    full_idx = pd.to_datetime(dates)
    bench_map = {
        "A": pd.Series([100.0 + 100.0 * k / (len(dates) - 1) for k in range(len(dates))], index=full_idx),
        "HK": pd.Series([100.0 + 10.0 * k / (len(dates) - 1) for k in range(len(dates))], index=full_idx),
        "US": pd.Series([100.0 + 10.0 * k / (len(dates) - 1) for k in range(len(dates))], index=full_idx),
    }
    called = []
    def fake_bench(market="A"):
        called.append(market)
        return bench_map.get(market)
    monkeypatch.setattr(pt, "_fetch_benchmark_df", fake_bench)

    summary = pt.validate_signals()
    assert set(called) >= {"A", "HK", "US"}
    det = {x["code"]: x for x in summary["details"]}
    # 关键断言：港股/美股信号使用各自市场基准（非 A 股基准），因此超额收益
    # 与 A 股基准下不同——此处仅验证基准按市场路由，具体数值不敏感。
    assert det["00700"]["excess_ret_pct"] is not None
    assert det["AAPL"]["excess_ret_pct"] is not None
    assert det["600036"]["excess_ret_pct"] is not None
    # A 股基准 +100%（远超股票 +10%）→ A 股信号平均超额应显著为负
    assert det["600036"]["excess_ret_pct"] < -5


def test_fetch_real_price_multi_source(monkeypatch):
    """A/HK/US 价格分别走腾讯/新浪/美股多源封装。"""
    def fake_tx(codes):
        return {c: {"price": 10.0, "chg": 0.0} for c in codes}
    monkeypatch.setattr(
        "modules.data.market._fetch_tx_quotes", fake_tx)
    assert pt._fetch_real_price("600900", "A") == (10.0, "tx")

    def fake_sina(codes):
        import pandas as pd
        return pd.DataFrame([{"c": codes[0], "price": 5.0, "chg": 0.0}])
    monkeypatch.setattr(
        "modules.data.market._fetch_sina_hk", fake_sina)
    assert pt._fetch_real_price("00700", "HK") == (5.0, "sina_hk")

    def fake_us(codes):
        import pandas as pd
        return pd.DataFrame([{"c": codes[0], "price": 3.0, "chg": 0.0}])
    monkeypatch.setattr(
        "modules.data.market_us.fetch_us_spot", fake_us)
    assert pt._fetch_real_price("AAPL", "US") == (3.0, "tx_lb_us")

    # 数据源失败时返回 unavailable，不抛异常
    monkeypatch.setattr(
        "modules.data.market._fetch_tx_quotes", lambda codes: {})
    assert pt._fetch_real_price("600900", "A") == (None, "unavailable")
