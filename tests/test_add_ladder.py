"""正金字塔加仓层级单元测试"""
from modules.add_ladder import plan_add_ladder


def test_ladder_decreasing_allocation():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=82.0)
    assert r["verdict"] == "可加仓"
    assert r["step"] == 1
    assert r["add_pct"] == 5.0
    assert r["ladder_alloc"] == [5.0, 3.0, 2.0]


def test_ladder_blocks_on_alert_tier():
    r = plan_add_ladder(10.0, 20.0, tier="警戒", lishing=82.0)
    assert r["verdict"] == "持有"
    assert "档位" in r["reason"]


def test_ladder_blocks_on_low_score():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=55.0, buy_th=70.0)
    assert r["verdict"] == "持有"
    assert "评分" in r["reason"]


def test_ladder_blocks_when_at_maxpos():
    r = plan_add_ladder(30.0, 35.0, tier="黄金", lishing=82.0, maxpos=30.0)
    assert r["verdict"] == "持有"
    assert r["cap_hit"] is True


def test_ladder_blocks_when_gap_too_small():
    r = plan_add_ladder(10.0, 11.0, tier="黄金", lishing=82.0, min_gap=2.0)
    assert r["verdict"] == "持有"


def test_ladder_second_step_uses_add_count():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=82.0, add_count=1)
    assert r["step"] == 2
    assert r["add_pct"] == 3.0


def test_ladder_caps_at_last_step():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=82.0, add_count=2)
    assert r["step"] == 3
    assert r["add_pct"] == 2.0


def test_ladder_holds_after_all_steps_used():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=82.0, add_count=3)
    assert r["verdict"] == "持有"
    assert r["add_pct"] == 0.0
    assert r["cap_hit"] is True


def test_ladder_caps_by_maxpos():
    r = plan_add_ladder(28.0, 40.0, tier="黄金", lishing=82.0, maxpos=30.0)
    assert r["verdict"] == "可加仓"
    assert r["add_pct"] == 2.0
    assert r["cap_hit"] is True
    assert sum(r["ladder_alloc"]) <= 2.0


def test_ladder_scores_zero_blocks():
    r = plan_add_ladder(10.0, 20.0, tier="黄金", lishing=0.0)
    assert r["verdict"] == "持有"
    assert "红旗" in r["reason"]