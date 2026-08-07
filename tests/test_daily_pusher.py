"""每日推送(dry-run/交易日/降级)单元测试"""
import datetime

import pytest

import modules.daily_pusher as dpusher
import modules.daily_picks as dpicks


def test_parse_hhmm_valid():
    from modules.scheduler import _parse_hhmm
    assert _parse_hhmm("16:30") == (16, 30)
    assert _parse_hhmm("06:05") == (6, 5)
    assert _parse_hhmm("24:00") is None
    assert _parse_hhmm("abc") is None


def test_us_trading_day():
    # 2026-07-31 周五
    assert dpusher._is_trading_day_us(datetime.date(2026, 7, 31)) is True
    # 周六
    assert dpusher._is_trading_day_us(datetime.date(2026, 8, 1)) is False
    # 圣诞 (12/25 周五) 跳过
    assert dpusher._is_trading_day_us(datetime.date(2026, 12, 25)) is False


def test_ah_trading_day_weekend_fallback(monkeypatch):
    monkeypatch.setattr(dpusher, "_load_calendar", lambda year: None)
    # 周六
    assert dpusher._is_trading_day_ah(datetime.date(2026, 8, 1)) is False
    # 周五, 日历不可用则按周一~五
    assert dpusher._is_trading_day_ah(datetime.date(2026, 7, 31)) is True


def test_ah_trading_day_from_calendar(monkeypatch):
    monkeypatch.setattr(dpusher, "_load_calendar",
                        lambda year: {"2026-07-31", "2026-08-03"})
    # 周日但日历里有 08-03(周一), 该日应判定
    assert dpusher._is_trading_day_ah(datetime.date(2026, 8, 3)) is True
    # 周六
    assert dpusher._is_trading_day_ah(datetime.date(2026, 8, 1)) is False


def test_us_uses_prev_day_for_trading_check(monkeypatch):
    """北京时间周六早上推送美股, 判定的是美股周五(北京日期-1)。"""
    monkeypatch.setattr(dpusher, "_freshness_ok", lambda: True)
    monkeypatch.setattr(dpusher, "build_picks",
                        lambda include_announcements=True: {
                            "date": "2026-08-01",
                            "picks": [
                                {"code": "AAPL", "name": "苹果", "category": "可关注",
                                 "held": True, "market": "US",
                                 "lines": ["- 理由"], "announcement": None},
                            ],
                            "skipped": [],
                        })
    sent, msg = dpusher.build_and_send("US", dry_run=True,
                                       day=datetime.date(2026, 8, 1),
                                       skip_trading_check=False)
    # 08-01 是周六, 但美股判定用 07-31(周五) → 应生成文案
    assert sent is False
    assert msg is not None
    assert "AAPL" in msg


def test_freshness_degradation(monkeypatch):
    """数据新鲜度异常时降级为提醒, 不生成选股。"""
    monkeypatch.setattr(dpusher, "_freshness_ok", lambda: False)
    sent, msg = dpusher.build_and_send("AH", dry_run=True,
                                       day=datetime.date(2026, 7, 31),
                                       skip_trading_check=True)
    assert "数据异常" in msg
    assert sent is True

def test_freshness_check_exception_fails_closed(monkeypatch):
    """新鲜度检查自身异常时必须视为不可靠，而不是照常推送。"""
    def boom():
        raise RuntimeError("freshness broken")
    monkeypatch.setattr(dpusher, "freshness_issues", boom)
    assert dpusher._freshness_ok() is False


def test_dry_run_no_send(monkeypatch):
    """dry-run 不触发 AlertSender。"""
    monkeypatch.setattr(dpusher, "_freshness_ok", lambda: True)
    calls = []
    monkeypatch.setattr(dpicks, "_build_watch_pool",
                        lambda: (None, {"error": "无数据"}))
    monkeypatch.setattr(dpusher, "build_picks",
                        lambda include_announcements=True: {
                            "date": "2026-07-31", "picks": [], "skipped": []})
    monkeypatch.setattr(dpusher.AlertSender, "send",
                        lambda self, *a, **k: calls.append(a))
    sent, msg = dpusher.build_and_send("AH", dry_run=True,
                                       day=datetime.date(2026, 7, 31),
                                       skip_trading_check=True)
    assert msg is None
    assert calls == []


def test_send_non_dry(monkeypatch):
    """非 dry-run 触发推送。"""
    monkeypatch.setattr(dpusher, "_freshness_ok", lambda: True)
    sent_calls = []
    monkeypatch.setattr(dpusher, "build_picks",
                        lambda include_announcements=True: {
                            "date": "2026-07-31",
                            "picks": [
                                {"code": "600036", "name": "招商银行",
                                 "category": "可加仓", "held": True, "market": "A",
                                 "lines": ["- 理由"], "announcement": None},
                            ],
                            "skipped": [],
                        })
    monkeypatch.setattr(dpusher.AlertSender, "send",
                        lambda self, *a, **k: sent_calls.append(a) or True)
    sent, msg = dpusher.build_and_send("AH", dry_run=False,
                                       day=datetime.date(2026, 7, 31),
                                       skip_trading_check=True)
    assert sent is True
    assert len(sent_calls) == 1
    assert "招商银行" in sent_calls[0][1]
    assert "可加仓" in sent_calls[0][1]


def test_market_filter_us(monkeypatch):
    """US 市场只保留美股标的。"""
    monkeypatch.setattr(dpusher, "_freshness_ok", lambda: True)
    sent_calls = []
    monkeypatch.setattr(dpusher, "build_picks",
                        lambda include_announcements=True: {
                            "date": "2026-07-31",
                            "picks": [
                                {"code": "AAPL", "name": "苹果", "category": "可关注",
                                 "held": True, "market": "US",
                                 "lines": ["- 理由"], "announcement": None},
                                {"code": "600036", "name": "招商银行", "category": "可加仓",
                                 "held": True, "market": "A",
                                 "lines": ["- 理由"], "announcement": None},
                            ],
                            "skipped": [],
                        })
    monkeypatch.setattr(dpusher.AlertSender, "send",
                        lambda self, *a, **k: sent_calls.append(a) or True)
    sent, msg = dpusher.build_and_send("US", dry_run=False,
                                       day=datetime.date(2026, 7, 31),
                                       skip_trading_check=True)
    assert sent is True
    assert "AAPL" in msg
    assert "600036" not in msg
