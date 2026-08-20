"""统一实际持仓账本测试。"""
import json
import pytest

import modules.actual_holdings as ah

_ORIG = {}


@pytest.fixture(autouse=True)
def _restore_paths():
    for k in ("BROKER_PATH", "SHADOW_PATH", "COST_PATH"):
        _ORIG.setdefault(k, getattr(ah, k))
    yield
    for k, v in _ORIG.items():
        setattr(ah, k, v)


def _mk_broker(tmp_path, records):
    p = tmp_path / "broker_trades.json"
    p.write_text(json.dumps({"records": records}), encoding="utf-8")
    ah.BROKER_PATH = str(p)
    return p


def _mk_paper(tmp_path, positions):
    p = tmp_path / "paper_portfolio.json"
    p.write_text(json.dumps({"positions": positions}), encoding="utf-8")
    ah.SHADOW_PATH = str(p)


def test_buy_aggregate_avg_cost(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "600036", "side": "buy", "shares": 100, "price": 38.0, "fee_total": 5, "market": "A"},
        {"code": "600036", "side": "buy", "shares": 100, "price": 42.0, "fee_total": 5, "market": "A"},
    ])
    out = ah.get_actual_positions()
    assert out["600036"]["shares"] == 200
    # cost = (3800+5)+(4200+5) = 8010 / 200 = 40.05
    assert abs(out["600036"]["avg_cost"] - 40.05) < 0.001


def test_buy_sell_net(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "601398", "side": "buy", "shares": 1000, "price": 7.0, "fee_total": 5, "market": "A"},
        {"code": "601398", "side": "sell", "shares": 400, "price": 7.5, "fee_total": 5, "market": "A"},
    ])
    out = ah.get_actual_positions()
    assert out["601398"]["shares"] == 600


def test_zero_or_negnet_excluded(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "X", "side": "buy", "shares": 500, "price": 10.0, "fee_total": 0, "market": "A"},
        {"code": "X", "side": "sell", "shares": 500, "price": 10.0, "fee_total": 0, "market": "A"},
    ])
    assert "X" not in ah.get_actual_positions()


def test_no_broker_fallback_to_paper(tmp_path):
    _mk_broker(tmp_path, [])
    _mk_paper(tmp_path, {"000001": {"shares": 300, "avg_cost": 11.0}})
    out = ah.get_actual_holdings()
    assert out["_source"] == "paper"
    assert out["positions"]["000001"]["shares"] == 300


def test_get_actual_holdings_broker_source(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "600036", "side": "buy", "shares": 100, "price": 38.0, "fee_total": 5, "market": "A"},
    ])
    out = ah.get_actual_holdings()
    assert out["_source"] == "broker"
    assert "600036" in out["positions"]


def test_reconcile_matches(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "600036", "side": "buy", "shares": 100, "price": 38.0, "fee_total": 0, "market": "A"},
    ])
    _mk_paper(tmp_path, {"600036": {"shares": 100, "avg_cost": 38.0}})
    p = tmp_path / "cost_tiers.json"
    p.write_text(json.dumps({"600036": {"batches": [{"shares": 100, "cost": 38.0, "date": "2026-01-01"}]}}),
                 encoding="utf-8")
    ah.COST_PATH = str(p)
    rep = ah.reconcile()
    assert rep["issues"] == []
    assert all(r["ok"] for r in rep["rows"])


def test_reconcile_flags_mismatch(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "600036", "side": "buy", "shares": 200, "price": 38.0, "fee_total": 0, "market": "A"},
    ])
    _mk_paper(tmp_path, {"600036": {"shares": 100, "avg_cost": 38.0}})
    p = tmp_path / "cost_tiers.json"
    p.write_text(json.dumps({"600036": {"batches": [{"shares": 100, "cost": 38.0, "date": "2026-01-01"}]}}),
                 encoding="utf-8")
    ah.COST_PATH = str(p)
    rep = ah.reconcile()
    assert len(rep["issues"]) == 1
    assert "600036" in rep["issues"][0]
    assert rep["rows"][0]["ok"] is False


def test_sync_shadow_from_broker(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "600036", "side": "buy", "shares": 200, "price": 38.0, "fee_total": 5, "market": "A"},
    ])
    p = tmp_path / "paper_portfolio.json"
    p.write_text(json.dumps({"positions": {"600036": {"shares": 100, "avg_cost": 38.0}}}), encoding="utf-8")
    ah.SHADOW_PATH = str(p)
    res = ah.sync_shadow_from_broker()
    assert res["updated"] is True
    assert res["positions"]["600036"]["shares"] == 200
    # 幂等：再次同步无变化
    res2 = ah.sync_shadow_from_broker()
    assert res2["updated"] is False


def test_fix_cost_batches_from_broker(tmp_path):
    _mk_broker(tmp_path, [
        {"code": "601398", "side": "buy", "shares": 1000, "price": 7.0, "fee_total": 5,
         "market": "A", "executed_at": "2026-04-10T09:30:00"},
        {"code": "601398", "side": "sell", "shares": 500, "price": 7.5, "fee_total": 5,
         "market": "A", "executed_at": "2026-05-02T09:30:00"},
    ])
    p = tmp_path / "cost_tiers.json"
    p.write_text(json.dumps({"601398": {"batches": [{"date": "2026-01-01", "shares": 1000, "cost": 7.0, "note": ""}]}}),
                 encoding="utf-8")
    ah.COST_PATH = str(p)
    res = ah.fix_cost_batches_from_broker()
    assert res["updated"] is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["601398"]["batches"][0]["shares"] == 500
    # 日期取最近一笔买入日，必须是有效日期（非 "0000-00-00" 占位）
    assert data["601398"]["batches"][0]["date"] == "2026-04-10"
    # 幂等
    res2 = ah.fix_cost_batches_from_broker()
    assert res2["updated"] is False