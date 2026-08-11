"""picks_evaluator 测试：建议账本记录 + 回测 + 执行审计。全部用临时路径，不污染真实数据。"""
import datetime
import json
import os

import pytest

import modules.picks_evaluator as pe


@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    """把记录/报告/快照路径指向临时目录。"""
    rec_path = tmp_path / "picks_records.jsonl"
    rep_dir = tmp_path / "reports"
    snap_path = tmp_path / "signal_snapshots.json"

    monkeypatch.setattr(pe, "_RECORDS_PATH", str(rec_path))
    monkeypatch.setattr(pe, "_REPORT_DIR", str(rep_dir))
    monkeypatch.setattr(pe, "_SNAPSHOT_PATH", str(snap_path))
    monkeypatch.setattr(pe, "_DATA_DIR", str(tmp_path))

    def write_snapshot(dates_prices):
        # dates_prices: {"2026-01-05": [{"code":"600036","price":10.0}, ...]}
        snap = {d: dates_prices.get(d, []) for d in dates_prices}
        snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    return {
        "rec_path": rec_path,
        "snap_path": snap_path,
        "write_snapshot": write_snapshot,
    }


def _picks(date="2026-01-05"):
    return {
        "date": date,
        "picks": [
            {"code": "600036", "name": "招商银行", "market": "A", "category": "可建仓",
             "held": False, "price": 10.0, "kelly_final": 8.0},
            {"code": "601398", "name": "工商银行", "market": "A", "category": "可加仓",
             "held": True, "price": 5.0, "kelly_final": 6.0},
            {"code": "00700", "name": "腾讯", "market": "HK", "category": "可关注",
             "held": False, "price": 300.0, "kelly_final": 0.0},
        ],
    }


def test_record_picks_new(iso_env):
    n = pe.record_picks(_picks())
    assert n == 3
    lines = iso_env["rec_path"].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["pick_id"] == "2026-01-05|A|600036"
    assert first["market"] == "A"
    assert first["category"] == "可建仓"
    assert first["entry_price"] == 10.0


def test_record_picks_idempotent(iso_env):
    assert pe.record_picks(_picks()) == 3
    # 同日期同代码再记一次应被去重
    assert pe.record_picks(_picks()) == 0
    assert len(iso_env["rec_path"].read_text(encoding="utf-8").strip().splitlines()) == 3


@pytest.mark.parametrize("market,expected", [
    ("600036", "A"), ("00700", "HK"), ("AAPL", "US"), ("1234567", "A")])
def test_market_of(market, expected):
    assert pe._market_of(market) == expected


def test_evaluate_picks_no_snapshot(iso_env):
    r = pe.evaluate_picks()
    assert "error" in r


def test_evaluate_picks_forward_return(iso_env, monkeypatch):
    iso_env["write_snapshot"]({
        # 6 个连续交易日，600036 从 10 涨到 13
        "2026-01-05": [{"code": "600036", "price": 10.0}],
        "2026-01-06": [{"code": "600036", "price": 11.0}],
        "2026-01-07": [{"code": "600036", "price": 11.5}],
        "2026-01-08": [{"code": "600036", "price": 12.0}],
        "2026-01-09": [{"code": "600036", "price": 12.5}],
        "2026-01-10": [{"code": "600036", "price": 13.0}],
    })
    pe.record_picks(_picks())
    # 禁用基准拉取(离线测试), 只验证绝对收益
    import modules.paper_trader as pt
    monkeypatch.setattr(pt, "_fetch_benchmark_df", lambda market: None)
    r = pe.evaluate_picks(horizons=(5,))

    bh = r["by_horizon"]["5"]
    assert bh["可建仓"]["total"] == 1
    assert bh["可建仓"]["win_rate"] == 100.0
    # 10 -> 13 = 30%
    assert bh["可建仓"]["avg_ret_pct"] == 30.0
    assert r["total_records"] == 3


def test_audit_execution_broken_detection(iso_env, monkeypatch):
    iso_env["write_snapshot"]({"2026-01-05": [{"code": "600036", "price": 10.0}]})
    pe.record_picks(_picks())
    # 模拟空组合(无任何成交)
    monkeypatch.setattr(pe, "_load_json", lambda path, d=None: {"trades": []})
    audit = pe.audit_execution()
    # 可操作建议只看 A 股可建仓/可加仓 -> 600036, 601398; 00700 是可关注不参与执行审计
    assert audit["total_a_actionable"] == 2
    assert len(audit["broken"]) == 2