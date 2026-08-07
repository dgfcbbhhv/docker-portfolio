# -*- coding: utf-8 -*-
"""主营业务估值引擎单元测试"""
import pytest

from modules.valuation import (
    valuate, market_of, quality_score, fair_pe, push_lines,
)


def test_market_of_by_group():
    assert market_of({"code": "600036", "group": "A股持仓"}) == "A"
    assert market_of({"code": "00002", "group": "港股持仓"}) == "HK"
    assert market_of({"code": "AAPL", "group": "美股"}) == "US"
    assert market_of({"code": "508098", "group": "REIT持仓"}) == "REIT"


def test_market_of_by_code_shape():
    assert market_of({"code": "600900"}) == "A"
    assert market_of({"code": "00002"}) == "HK"   # 5 位代码是港股
    assert market_of({"code": "00700"}) == "HK"
    assert market_of({"code": "AAPL"}) == "US"


def test_quality_score_full_marks():
    score, parts, missing = quality_score(25.0, 120.0, 30.0, "A")
    assert score == 100
    assert missing == []


def test_quality_score_missing_dims():
    score, parts, missing = quality_score(None, None, None, "A")
    assert score == 0
    assert len(missing) == 3


def test_fair_pe_monotonic_and_bounds():
    assert fair_pe(100, "A") == 20.0
    assert fair_pe(0, "A") == 8.0
    assert fair_pe(100, "HK") == 17.0
    assert fair_pe(100, "US") == 35.0
    assert fair_pe(50, "A") < fair_pe(90, "A")


def test_cheap_stock_worth_buying():
    row = {"code": "600036", "name": "招商银行", "group": "A股持仓",
           "price": 10.0, "pe": 6.0, "pb": 0.9, "roe": 13.0,
           "ocf_ratio": 120.0, "debt": 50.0, "dy": 5.0}
    v = valuate(row)
    assert v["available"] is True
    assert v["market"] == "A"
    assert v["price_level"] == "便宜"
    assert v["margin_of_safety"] > 30
    assert "值得买" in v["verdict"]
    assert v["li"]["conclusion"].startswith("稳健")
    assert "值得长期持有" in v["munger"]["conclusion"]


def test_expensive_stock_avoid():
    row = {"code": "X", "name": "X", "group": "A股持仓",
           "price": 50.0, "pe": 60.0, "pb": 8.0, "roe": 6.0,
           "ocf_ratio": 40.0, "debt": 75.0, "dy": 0.5}
    v = valuate(row)
    assert v["available"] is True
    assert v["price_level"] == "昂贵"
    assert v["verdict"].startswith("回避")


def test_no_price_data():
    row = {"code": "Y", "name": "Y", "group": "A股持仓", "price": 10.0,
           "pe": None, "pb": None, "roe": None,
           "ocf_ratio": None, "debt": None, "dy": None}
    v = valuate(row)
    assert v["available"] is False
    assert v["price_level"] == "数据不足"
    assert v["verdict"] == "数据不足"


def test_missing_quality_dims_verdict_watch_not_avoid():
    """港股银行缺现金流/负债率时, 质量分低但不应武断判'回避'。"""
    row = {"code": "01398", "name": "工商银行(H)", "group": "港股持仓",
           "price": 5.0, "pe": 4.5, "pb": 0.5, "roe": 11.0,
           "ocf_ratio": None, "debt": None, "dy": 4.6}
    v = valuate(row)
    assert v["available"] is True
    assert "数据不足" in v["verdict"]
    assert "回避" not in v["verdict"]


def test_us_prefers_pe_route():
    """美股高 PB 不因 PB 法被过度惩罚。"""
    row = {"code": "AAPL", "name": "苹果", "group": "美股",
           "price": 200.0, "pe": 35.0, "pb": 40.0, "roe": 30.0,
           "ocf_ratio": 110.0, "debt": 45.0, "dy": 0.4}
    v = valuate(row)
    assert v["market"] == "US"
    assert v["margin_of_safety"] > -30   # 不应是极度昂贵
    assert v["price_level"] in ("合理", "合理偏低", "偏贵")


def test_push_lines_content():
    row = {"code": "600036", "name": "招商银行", "group": "A股持仓",
           "price": 10.0, "pe": 6.0, "pb": 0.9, "roe": 13.0,
           "ocf_ratio": 120.0, "debt": 50.0, "dy": 5.0}
    lines = push_lines(valuate(row))
    assert any("估值贵不贵" in l for l in lines)
    assert any("值不值价值投资" in l for l in lines)
    assert any("李嘉诚视角" in l for l in lines)
    assert any("芒格视角" in l for l in lines)
    assert len(lines) == 4


def test_push_lines_no_data():
    v = valuate({"code": "Y", "price": 10.0, "pe": None, "pb": None,
                 "roe": None, "ocf_ratio": None, "debt": None, "dy": None})
    lines = push_lines(v)
    assert len(lines) == 1
    assert "数据不足" in lines[0]