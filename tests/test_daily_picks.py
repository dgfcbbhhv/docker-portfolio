"""每日选股(分类/文案)单元测试"""
import datetime

import pytest

import modules.daily_picks as dp


class FakeRow:
    """最小化行对象, 只包含选股分类所需字段。"""
    def __init__(self, code="600036", name="招商银行", total=85, flags="",
                 price=10.0, kelly=12.0, ratio=5.0, pe=6.0, pb=0.9,
                 dy=5.0, roe=13.0, debt=50.0, ocf_ratio=1.2, growth=False,
                 market="A"):
        self.code = code
        self._d = {
            "code": code, "name": name, "lishing": total,
            "lishing_flags": flags, "price": price,
            "kelly_final": kelly, "ratio_norm": ratio,
            "pe": pe, "pb": pb, "dy": dy, "roe": roe, "debt": debt,
            "ocf_ratio": ocf_ratio,
        }
        self._growth = growth

    def get(self, k, default=None):
        if k == "growth_factors":
            return [1] if self._growth else []
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d.get(k)


@pytest.fixture
def buy_th():
    return 70.0


@pytest.fixture
def watch_th():
    return 60.0


@pytest.fixture
def add_gap():
    return 2.0


def test_parse_market():
    assert dp._market_for_code("600036") == "A"
    assert dp._market_for_code("000001") == "A"
    assert dp._market_for_code("02388") == "HK"
    assert dp._market_for_code("AAPL") == "US"
    assert dp._market_for_code("REIT-S") == "US"


def test_fmt_none_and_zero():
    assert dp._fmt(None) == "数据不足"
    assert dp._fmt(0) == "数据不足"
    assert dp._fmt(6.66, 1) == "6.7"
    assert dp._fmt(5, 0) == "5"
    assert dp._fmt("abc") == "数据不足"


def test_classify_insufficient():
    r = FakeRow(total=0, price=0)
    cat, lines = dp._classify(r, False, 70, 60, 2.0)
    assert cat == "不推送"
    assert lines[0] == "数据不足，无法评估"


def test_classify_red_flag():
    r = FakeRow(total=80, flags="ROE评分: 低")
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "不推送"


def test_classify_growth_flags_ignored(monkeypatch):
    """US 成长股路径的 lishing_flags 是因子拆分, 不视为红旗。"""
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    r = FakeRow(total=75, flags="估值分位(PE/PB):10/30", growth=True,
                kelly=12, ratio=5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可加仓"


def test_classify_buyable_unheld():
    r = FakeRow(total=80, kelly=12, ratio=0)
    cat, lines = dp._classify(r, False, 70, 60, 2.0)
    assert cat == "可建仓"
    assert any("建仓" in ln for ln in lines)


def test_classify_watch_unheld_low_score():
    r = FakeRow(total=65, kelly=0, ratio=0)
    cat, _ = dp._classify(r, False, 70, 60, 2.0)
    assert cat == "可关注"


def test_classify_not_push_unheld_low():
    r = FakeRow(total=50, kelly=0, ratio=0)
    cat, _ = dp._classify(r, False, 70, 60, 2.0)
    assert cat == "不推送"


def test_classify_add_held_gap_ok(monkeypatch):
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    r = FakeRow(total=85, kelly=12, ratio=5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可加仓"


def test_classify_add_held_gap_small():
    r = FakeRow(total=85, kelly=12, ratio=10.5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可关注"


def test_classify_held_watch_band():
    r = FakeRow(total=65, kelly=0, ratio=5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可关注"


def test_reason_lines_fields():
    r = FakeRow(total=85, kelly=12, ratio=5)
    lines = dp._reason_lines(r, True, "可加仓", "A", 2.0)
    joined = "\n".join(lines)
    assert "李式评分 85/100" in joined
    assert "PE 6.0" in joined
    assert "股息率 5.0%" in joined
    assert "ROE 13.0%" in joined


def test_compose_message_structure():
    result = {
        "date": "2026-07-31",
        "picks": [
            {"code": "600036", "name": "招商银行", "category": "可加仓",
             "market": "A", "held": True, "lines": ["- 理由一", "- 理由二"],
             "announcement": None},
        ],
    }
    msg = dp.compose_message(result, market="AH")
    assert "【A股/港股】" in msg
    assert "600036 招商银行 | 可加仓" in msg
    assert "理由一" in msg
    assert "不构成投资建议" in msg


def test_compose_message_empty():
    assert dp.compose_message({"picks": []}, market="AH") == ""


def test_announcement_line_no_url():
    ann = {"date": "2026-07-14", "title": "公告标题", "url": ""}
    assert "原文链接数据不足" in dp._announcement_line(ann)
    assert "公告标题" in dp._announcement_line(ann)


def test_announcement_line_with_url():
    ann = {"date": "2026-07-14", "title": "公告标题", "url": "http://x"}
    assert "http://x" in dp._announcement_line(ann)


def test_announcement_line_empty():
    assert dp._announcement_line({}) == "- 最新公告: 数据不足"


def test_held_codes_aggregates_all_markets(monkeypatch):
    monkeypatch.setattr(dp, "PORT_A", {"600036": {}, "000001": {}})
    monkeypatch.setattr(dp, "PORT_HK", {"02388": {}})
    monkeypatch.setattr(dp, "PORT_US", {"AAPL": {}})
    monkeypatch.setattr(dp, "PORT_REIT", {})
    held = dp._held_codes()
    assert held["600036"] is True
    assert held["02388"] is True
    assert held["AAPL"] is True
