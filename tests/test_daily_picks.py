"""每日选股(分类/文案)单元测试"""
import datetime

import pytest

import modules.daily_picks as dp


class FakeRow:
    """最小化行对象, 只包含选股分类所需字段。"""
    def __init__(self, code="600036", name="招商银行", total=85, flags="",
                 price=10.0, kelly=12.0, ratio=5.0, pe=6.0, pb=0.9,
                 dy=5.0, roe=13.0, debt=50.0, ocf_ratio=1.2, growth=False,
                 market="A", ratio_norm=None):
        self.code = code
        self._d = {
            "code": code, "name": name, "lishing": total,
            "lishing_flags": flags, "price": price,
            "kelly_final": kelly, "ratio_norm": ratio_norm if ratio_norm is not None else ratio,
            "ratio": ratio,
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


def test_classify_add_held_gap_small(monkeypatch):
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    r = FakeRow(total=85, kelly=12, ratio=10.5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可关注"


def test_classify_held_watch_band(monkeypatch):
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    r = FakeRow(total=65, kelly=5, ratio=5)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可关注"


def test_classify_reduce_alert_tier(monkeypatch):
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "警戒"})
    r = FakeRow(total=85, kelly=12, ratio=5)
    cat, lines = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "减仓"
    assert any("减仓" in ln for ln in lines)


def test_classify_reduce_low_score():
    r = FakeRow(total=55, kelly=0, ratio=5)
    cat, lines = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "减仓"
    assert any("减仓" in ln for ln in lines)


def test_classify_reduce_overweight():
    r = FakeRow(total=85, kelly=5, ratio=20)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "减仓"


def test_classify_no_reduce_when_kelly_missing(monkeypatch):
    """Kelly 缺失(0)时，超配不应触发减仓噪声；有真实档位/评分依据时才减仓。"""
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    r = FakeRow(total=77, kelly=0, ratio=30)
    cat, _ = dp._classify(r, True, 70, 60, 2.0)
    assert cat == "可关注"
    # 同条件下若 Kelly 已知且严重超配，才应减仓
    r2 = FakeRow(total=77, kelly=5, ratio=30)
    cat2, _ = dp._classify(r2, True, 70, 60, 2.0)
    assert cat2 == "减仓"


def test_compose_action_label_mapping():
    assert dp.ACTION_LABEL["可建仓"] == "建议建仓"
    assert dp.ACTION_LABEL["可加仓"] == "建议加仓"
    assert dp.ACTION_LABEL["减仓"] == "建议减仓"
    assert dp.ACTION_LABEL["可关注"] == "持有观察"


def test_classify_operation_wrapper():
    """classify_operation 复用 _classify，并给出中文标签。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "黄金"})
    # 未持有、高分达标 -> 建仓
    r = FakeRow(total=80, kelly=12, ratio=0)
    cat, label = dp.classify_operation(r, False, holdings=None)
    assert cat == "可建仓"
    assert label == "建议建仓"
    # 已持有、警戒档位 -> 减仓
    r2 = FakeRow(total=85, kelly=12, ratio=5)
    monkeypatch.setattr(dp, "calc_overall_tier",
                        lambda code, price, stock=None: {"tier": "警戒"})
    cat2, label2 = dp.classify_operation(r2, True, holdings=None)
    assert cat2 == "减仓"
    assert label2 == "建议减仓"
    monkeypatch.undo()


def test_effective_ratio_target_fallback():
    """无真实持仓时用目标配置的未归一化原始 ratio（非 ratio_norm）。"""
    r = FakeRow(total=74, kelly=8.5, ratio=6.0, ratio_norm=25.0)
    ratio, is_actual = dp._effective_ratio("600036", r, None)
    assert ratio == 6.0
    assert is_actual is False


def test_effective_ratio_actual_holdings():
    """有真实持仓(预计算 _per/_mv_total)时按市值权重计算。"""
    r = FakeRow(total=74, kelly=8.5, ratio=4.5)
    holdings = {
        "positions": {"600036": {"shares": 1000, "avg_cost": 30}},
        "cash": 10000,
        "_per": {"600036": 33000.0},
        "_mv_total": 33000.0,
    }
    ratio, is_actual = dp._effective_ratio("600036", r, holdings)
    assert is_actual is True
    assert abs(ratio - 33000.0 / 43000.0 * 100.0) < 0.1


def test_reason_lines_ratio_label():
    """目标语义下占比标注"目标占比"。"""
    r = FakeRow(total=74, kelly=8.5, ratio=4.5)
    lines = dp._reason_lines(r, True, "可加仓", "HK", 2.0, holdings=None)
    assert any("目标占比 4.5%" in ln for ln in lines)
    assert not any("当前占比" in ln for ln in lines)


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
    assert "600036 招商银行：建议加仓" in msg
    assert "建议加仓" in msg
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
    monkeypatch.setattr(dp, "get_actual_holdings", lambda: {})
    monkeypatch.setattr(dp, "PORT_A", {"600036": {}, "000001": {}})
    monkeypatch.setattr(dp, "PORT_HK", {"02388": {}})
    monkeypatch.setattr(dp, "PORT_US", {"AAPL": {}})
    monkeypatch.setattr(dp, "PORT_REIT", {})
    held = dp._held_codes()
    assert held["600036"] is True
    assert held["02388"] is True
    assert held["AAPL"] is True
