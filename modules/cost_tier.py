import csv
import json
import os
from io import StringIO

from modules.paths import notes_dir

COST_FILE = os.path.join(notes_dir(), "cost_tiers.json")

def _load():
    if not os.path.exists(COST_FILE):
        return {}
    with open(COST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(data):
    os.makedirs(os.path.dirname(COST_FILE), exist_ok=True)
    with open(COST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_all():
    return _load()

def get_stock(code):
    data = _load()
    return data.get(code, {"batches": [], "dividend_per_share": 0.0})

def save_stock(code, stock_data):
    data = _load()
    data[code] = stock_data
    _save(data)

def add_batch(code, date, shares, cost, note=""):
    """添加批次; 若(日期,股数,成本)完全相同则跳过, 返回是否新增"""
    stock = get_stock(code)
    for b in stock["batches"]:
        if (b.get("date"), b.get("shares"), b.get("cost")) == (date, shares, cost):
            return False
    stock["batches"].append({"date": date, "shares": shares, "cost": cost, "note": note})
    save_stock(code, stock)
    return True

def batch_import(code, csv_text):
    """
    批量导入批次数据
    CSV格式: 日期,股数,成本,备注
    示例: 2025-01-15,1000,12.50,建仓
    """
    stock = get_stock(code)
    reader = csv.reader(StringIO(csv_text))
    imported = 0
    for row in reader:
        if len(row) < 3:
            continue
        try:
            date = row[0].strip()
            shares = int(row[1].strip())
            cost = float(row[2].strip())
            note = row[3].strip() if len(row) > 3 else ""
            if shares > 0 and cost > 0:
                stock["batches"].append({"date": date, "shares": shares, "cost": cost, "note": note})
                imported += 1
        except (ValueError, IndexError):
            continue
    if imported > 0:
        save_stock(code, stock)
    return imported

def update_dividend(code, dividend):
    stock = get_stock(code)
    stock["dividend_per_share"] = dividend
    save_stock(code, stock)

def delete_batch(code, batch_index):
    stock = get_stock(code)
    if 0 <= batch_index < len(stock["batches"]):
        stock["batches"].pop(batch_index)
        save_stock(code, stock)

def deduct_sell(code, shares, note="卖出"):
    """卖出扣减: 按日期正序(FIFO)扣除最早批次, 反映真实持仓。

    返回 {"removed": int已扣, "matched_cost": float, "batches": 剩余批次}。
    超出总持仓时按全部可扣处理, 并空批次剔除。
    """
    stock = get_stock(code)
    batches = stock.get("batches", [])
    remaining = int(shares)
    removed = 0
    matched_cost = 0.0
    out = []
    for b in batches:
        bsh = int(b.get("shares") or 0)
        if remaining <= 0:
            out.append(b)
            continue
        if bsh <= remaining:
            removed += bsh
            matched_cost += bsh * float(b.get("cost") or 0)
            remaining -= bsh
            continue  # 整批扣完, 不保留空批次
        b["shares"] = bsh - remaining
        removed += remaining
        matched_cost += remaining * float(b.get("cost") or 0)
        remaining = 0
        out.append(b)
    stock["batches"] = out
    save_stock(code, stock)
    return {"removed": removed, "matched": round(matched_cost, 4), "batches": out}

def sell_then_report(code, shares, price=None, date="", note="卖出"):
    """卖出扣减 + 返回含已实现盈亏的报告, 兼容 reconcile 输出语义。"""
    res = deduct_sell(code, shares, note)
    if price:
        res["proceeds"] = round(res["removed"] * price, 2)
        res["realized_pnl"] = round(res["proceeds"] - res["matched"], 2)
    return res

def calc_batch_tier(batch_cost, dividend, current_price):
    if current_price <= 0 or dividend <= 0 or batch_cost <= 0:
        return {"ratio": None, "tier": "数据不足", "cost_dy": 0, "current_dy": 0}
    cost_dy = dividend / batch_cost
    current_dy = dividend / current_price
    ratio = cost_dy / current_dy if current_dy > 0 else 999
    if ratio >= 1.2:
        tier = "黄金"
    elif ratio >= 0.9:
        tier = "白银"
    else:
        tier = "警戒"
    return {
        "ratio": round(ratio, 3),
        "tier": tier,
        "cost_dy": round(cost_dy * 100, 2),
        "current_dy": round(current_dy * 100, 2),
    }

def calc_overall_tier(code, current_price, stock=None):
    """计算某股整体成本档位。stock 可注入以隔离全局 config（利于测试）；
    缺省从全局配置读取，保持向后兼容。"""
    if stock is None:
        stock = get_stock(code)
    dividend = stock.get("dividend_per_share", 0)
    batches = stock.get("batches", [])
    if not batches or dividend <= 0 or current_price <= 0:
        return {"ratio": None, "tier": "数据不足", "cost_dy": 0, "current_dy": 0, "batches": []}
    total_shares = sum(b["shares"] for b in batches)
    total_cost = sum(b["shares"] * b["cost"] for b in batches)
    if total_shares <= 0:
        return {"ratio": None, "tier": "数据不足", "cost_dy": 0, "current_dy": 0, "batches": []}
    avg_cost = total_cost / total_shares
    cost_dy = dividend / avg_cost
    current_dy = dividend / current_price
    ratio = cost_dy / current_dy if current_dy > 0 else 999
    if ratio >= 1.2:
        tier = "黄金"
    elif ratio >= 0.9:
        tier = "白银"
    else:
        tier = "警戒"
    batch_results = []
    for i, b in enumerate(batches):
        bt = calc_batch_tier(b["cost"], dividend, current_price)
        batch_results.append({**b, "index": i, **bt})
    return {
        "ratio": round(ratio, 3),
        "tier": tier,
        "cost_dy": round(cost_dy * 100, 2),
        "current_dy": round(current_dy * 100, 2),
        "avg_cost": round(avg_cost, 3),
        "total_shares": total_shares,
        "batches": batch_results,
    }

def init_batch(code, cost_price, dividend_per_share, shares=100, date=""):
    """
    自动初始化一只股票的成本数据。
    使用当前股价作为成本价，创建初始批次。
    适用于用户未录入任何成本数据的情况。
    """
    stock = get_stock(code)
    if stock.get("batches"):
        return False  # 已有数据，不覆盖
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")
    stock["batches"] = [{
        "date": date,
        "shares": shares,
        "cost": cost_price,
        "note": "自动初始化",
    }]
    stock["dividend_per_share"] = dividend_per_share
    stock["dividend_source"] = "manual"
    save_stock(code, stock)
    return True

def get_dividend_source(code):
    stock = get_stock(code)
    return stock.get("dividend_source", "auto")

def set_dividend_source(code, source):
    stock = get_stock(code)
    stock["dividend_source"] = source
    save_stock(code, stock)