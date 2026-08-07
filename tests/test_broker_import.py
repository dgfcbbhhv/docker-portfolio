"""A 股券商成交导入与三段对账单元测试"""
import json

import openpyxl
import pytest

import modules.broker_import as bi
import modules.push_audit as push_audit
import modules.dividend_log as dl

HEADERS = [
    "成交日期", "成交时间", "证券代码", "证券名称", "委托方向", "成交数量",
    "成交均价", "成交金额", "佣金", "其他费用", "印花税", "过户费",
    "资金余额", "股份余额", "委托编号", "成交编号", "交易市场", "币种",
]


def _make_table(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


def _sample_rows():
    return [
        ["2026-04-30", "09:58:25", "600036", "招商银行", "证券买入", 100,
         38.34, 3834.00, 4.79, 0.21, 0.0, 0.04,
         50000.00, 100, "OR1", "TR1", "上海A股", "人民币"],
        ["2026-05-06", "14:02:10", "601398", "工商银行", "证券卖出", 200,
         5.10, 1020.00, 1.02, 0.10, 0.51, 0.02,
         51000.00, 0, "OR2", "TR2", "上海A股", "人民币"],
    ]


def test_parse_trade_table(tmp_path):
    path = tmp_path / "Table_3923.xlsx"
    _make_table(path, _sample_rows())

    records = bi.parse_trade_table(str(path))
    assert len(records) == 2

    buy, sell = records
    assert buy["executed_at"] == "2026-04-30T09:58:25"
    assert buy["code"] == "600036"
    assert buy["side"] == "buy"
    assert buy["shares"] == 100
    assert buy["price"] == 38.34
    assert buy["amount"] == 3834.0
    assert buy["fee_total"] == pytest.approx(5.04, abs=1e-4)
    assert buy["trade_ref"] == "TR1"
    assert sell["side"] == "sell"
    assert sell["fee_breakdown"]["stamp_tax"] == 0.51


def test_parse_normalizes_code_and_side(tmp_path):
    path = tmp_path / "codes.xlsx"
    _make_table(path, [
        ["2026-05-06", "10:00:00", "600036.SH", "招商银行", "BUY", 100,
         10.0, 1000.0, 1.0, 0.0, 0.0, 0.0, 0.0, 100, "O1", "T1", "上海A股", "人民币"],
    ])

    records = bi.parse_trade_table(str(path))
    assert records[0]["code"] == "600036"
    assert records[0]["side"] == "buy"


def test_import_dedup_and_auto_link(tmp_path, monkeypatch):
    trades_path = tmp_path / "broker_trades.json"
    audit_path = tmp_path / "push_audit.jsonl"
    monkeypatch.setattr(bi, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bi, "TRADES_PATH", str(trades_path))
    monkeypatch.setattr(push_audit, "AUDIT_PATH", str(audit_path))

    push_audit.log_push({
        "push_id": "push-600036",
        "channel": "bark",
        "title": "今日价值投资选股",
        "content": "招商银行",
        "status": "sent",
        "meta": {"kind": "daily_picks", "date": "2026-04-29",
                 "codes": ["600036", "601398"]},
    })

    path = tmp_path / "Table.xlsx"
    _make_table(path, _sample_rows())
    records = bi.parse_trade_table(str(path))

    result = bi.import_records(records)
    assert result["added"] == 2
    assert result["duplicates"] == 0

    loaded = bi._load()
    buy = next(r for r in loaded if r["side"] == "buy")
    assert buy["push_id"] == "push-600036"

    result2 = bi.import_records(records)
    assert result2["added"] == 0
    assert result2["duplicates"] == 2
    assert len(bi._load()) == 2


def test_manual_push_link(tmp_path, monkeypatch):
    trades_path = tmp_path / "broker_trades.json"
    monkeypatch.setattr(bi, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bi, "TRADES_PATH", str(trades_path))

    path = tmp_path / "Table.xlsx"
    _make_table(path, _sample_rows())
    bi.import_records(bi.parse_trade_table(str(path)))

    assert bi.update_push_link("TR1", "manual-1") == 1
    trade = next(r for r in bi._load() if r["trade_ref"] == "TR1")
    assert trade["push_id"] == "manual-1"

    assert bi.update_push_link("TR1", None) == 1
    trade = next(r for r in bi._load() if r["trade_ref"] == "TR1")
    assert trade["push_id"] is None


def test_reconcile_report(tmp_path, monkeypatch):
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    trades_path = tmp_path / "broker_trades.json"
    monkeypatch.setattr(bi, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bi, "TRADES_PATH", str(trades_path))

    trades = [
        {"executed_at": "2026-05-06T09:30:00", "code": "600036",
         "name": "招商银行", "side": "buy", "shares": 100, "price": 10.0,
         "amount": 1000.0, "fee_total": 1.0, "fee_breakdown": {},
         "cash_after": 0, "shares_after": 100, "order_ref": "", "trade_ref": "R1",
         "market": "上海A股", "currency": "人民币", "push_id": "push-1"},
        {"executed_at": "2026-05-08T14:00:00", "code": "600036",
         "name": "招商银行", "side": "sell", "shares": 100, "price": 12.0,
         "amount": 1200.0, "fee_total": 1.5, "fee_breakdown": {},
         "cash_after": 0, "shares_after": 0, "order_ref": "", "trade_ref": "R2",
         "market": "上海A股", "currency": "人民币", "push_id": "push-1"},
    ]
    bi._save(trades)

    paper = {
        "trades": [
            {"date": "2026-05-06", "code": "600036", "action": "buy",
             "price": 10.1, "shares": 100},
            {"date": "2026-05-08", "code": "600036", "action": "sell",
             "price": 11.9, "shares": 100},
        ]
    }
    (paper_dir / "paper_portfolio.json").write_text(
        json.dumps(paper, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(dl, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "DIVIDEND_PATH", str(tmp_path / "dividend_log.json"))
    dl.add_dividend("600036", "招商银行", "2026-07-10", 50.0, "测试分红")

    report = bi.reconcile_report()
    assert report["total_trades"] == 2
    assert report["linked_trades"] == 2
    assert report["paper_matched"] == 2
    assert report["avg_price_deviation_pct"] == pytest.approx(
        ((10.0 - 10.1) / 10.1 + (12.0 - 11.9) / 11.9) / 2 * 100, abs=0.01)
    assert report["realized_pnl"] == pytest.approx(197.5, abs=0.01)
    assert report["total_fees"] == pytest.approx(2.5, abs=0.01)
    assert report["dividend_income"] == pytest.approx(50.0, abs=0.01)
    assert report["total_profit"] == pytest.approx(247.5, abs=0.01)
