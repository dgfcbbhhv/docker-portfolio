"""数据源字段 schema 集中化：把散落的字段码与比例映射收拢到一处。

背景
----
东方财富等接口的字段码与"整数刻度"在不同市场下不一致（已实测）：
  - A股 secid=1.x/0.x，最新价 f43 = 分（÷100）
  - 港股 secid=116.x，最新价 f43 = 厘（÷1000）
新浪会把"昨收"当现价返回（脏数据），需用东财等独立源仲裁。
把这些知识显式登记，避免在调用处再写 magic number。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EastmoneySpec:
    """东财单报价 stock/get 的字段与比例约定。"""
    price_field: str = "f43"      # 最新价（整数刻度）
    prev_field: str = "f60"       # 昨收（同刻度）
    chg_field: str = "f170"       # 涨跌幅（×100）
    pe_field: str = "f167"        # 市盈率（×10）
    pb_field: str = "f168"        # 市净率（×100）
    mcap_field: str = "f116"      # 总市值（元）
    amount_field: str = "f117"    # 成交额（元）
    code_field: str = "f57"
    name_field: str = "f58"


# 市场 -> 东财 secid 前缀 + 价格刻度
EASTMONEY = {
    "A": {"secid_prefix": lambda c: "1." if c.startswith(("6", "5")) else "0.",
          "price_scale": 100, "spec": EastmoneySpec()},
    "HK": {"secid_prefix": lambda c: "116.", "price_scale": 1000,
           "spec": EastmoneySpec()},
}


def eastmoney_secid(market, code):
    return EASTMONEY[market]["secid_prefix"](code) + str(code)


def eastmoney_price_scale(market):
    return EASTMONEY[market]["price_scale"]


def decode_eastmoney_price(market, raw):
    """把东财 f43 整数刻度换算为真实价格。"""
    try:
        return float(raw) / eastmoney_price_scale(market)
    except (TypeError, ValueError):
        return 0.0
