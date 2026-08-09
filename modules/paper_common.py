"""模拟盘共享常量与 JSON 读写（打破 paper_trader ↔ paper_executor 循环导入）。"""

import json
import os

PAPER_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "paper")
SNAPSHOT_FILE = os.path.join(PAPER_DIR, "signal_snapshots.json")
PORTFOLIO_FILE = os.path.join(PAPER_DIR, "paper_portfolio.json")
VALIDATION_FILE = os.path.join(PAPER_DIR, "signal_validation.json")
NAV_FILE = os.path.join(PAPER_DIR, "paper_nav.json")

VIRTUAL_CAPITAL = 1_000_000.0
MAX_POSITION_PCT = 30.0
SIGNAL_BUY_THRESHOLD = 65
SIGNAL_SELL_THRESHOLD = 55
SIGNAL_HOLD_THRESHOLD = 45

# A 股成交假设：T+1 开盘成交、涨停/跌停/停牌限制、费用
FEE_RATE = 0.001  # 单边费用（佣金+过户费等，可配置）
STAMP_TAX_RATE = 0.0005  # 卖出印花税
MIN_LOT = 100
A_LIMIT_DEFAULT = 0.10
A_LIMIT_ST = 0.05
A_LIMIT_CHINEXT = 0.20
A_LIMIT_BSE_REIT = 0.30


def _ensure_dir():
    os.makedirs(PAPER_DIR, exist_ok=True)


def _load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_json(path, data):
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)