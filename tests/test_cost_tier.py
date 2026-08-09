"""成本分层批次操作测试（含卖出 FIFO 扣减）。"""
import json
import pytest

import modules.cost_tier as ct


@pytest.fixture
def cost_file(tmp_path):
    p = tmp_path / "cost_tiers.json"
    p.write_text(json.dumps({
        "600036": {
            "batches": [
                {"date": "2026-01-05", "shares": 100, "cost": 38.0, "note": "建仓"},
                {"date": "2026-02-01", "shares": 100, "cost": 42.0, "note": "加仓"},
            ],
            "dividend_per_share": 2.0,
        },
    }), encoding="utf-8")
    ct.COST_FILE = str(p)
    return p


def test_deduct_sell_fifo_keeps_earlier_batch(cost_file):
    res = ct.deduct_sell("600036", 100)
    assert res["removed"] == 100
    assert res["matched"] == 100 * 38.0
    stock = ct.get_stock("600036")
    assert len(stock["batches"]) == 1
    assert stock["batches"][0]["shares"] == 100
    assert stock["batches"][0]["cost"] == 42.0


def test_deduct_sell_partial_within_one_batch(cost_file):
    res = ct.deduct_sell("600036", 50)
    assert res["removed"] == 50
    stock = ct.get_stock("600036")
    assert len(stock["batches"]) == 2
    assert stock["batches"][0]["shares"] == 50
    assert stock["batches"][1]["shares"] == 100


def test_deduct_sell_over_total_removes_all(cost_file):
    res = ct.deduct_sell("600036", 999)
    assert res["removed"] == 200
    assert ct.get_stock("600036")["batches"] == []


def test_deduct_sell_no_batches_unchanged(cost_file):
    res = ct.deduct_sell("000001", 10)
    assert res["removed"] == 0
    assert res["batches"] == []


def test_deduct_sell_persists_to_file(cost_file):
    ct.deduct_sell("600036", 100)
    data = json.loads(cost_file.read_text(encoding="utf-8"))
    assert data["600036"]["batches"][0]["shares"] == 100
    assert data["600036"]["batches"][0]["cost"] == 42.0


def test_sell_then_report_realized_pnl(cost_file):
    res = ct.sell_then_report("600036", 100, price=40.0)
    assert res["removed"] == 100
    assert abs(res["proceeds"] - 4000.0) < 0.01
    assert abs(res["realized_pnl"] - (4000.0 - 100 * 38.0)) < 0.01
