"""推送→模拟盘→成交 全链路冒烟测试 (隔离运行, 不触碰生产数据)。

覆盖:
1. 合成 picks → apply_picks_to_paper 挂单 (A 股建仓/减仓, 港美股跳过入影子台账)
2. 幂等性: 同日重复调用不重复挂单
3. T+1 成交: 次日开盘执行挂单, 更新持仓/现金/成交记录
4. T+1 边界: signal_date == execution_date 的挂单不执行
5. 港美股影子台账记录非 A 股可执行建议

运行: docker exec dividend-alert-worker python /app/scripts/paper_loop_smoke_test.py
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "/app")

# ---- 隔离: 全部指向临时目录 ----
_TMP = tempfile.mkdtemp(prefix="paper_smoke_")
_PF = os.path.join(_TMP, "paper_portfolio.json")
_LEDGER = os.path.join(_TMP, "shadow_ledger.jsonl")

import modules.paper_common as _pc
import modules.paper_picks as _pp
import modules.paper_executor as _pe
import modules.shadow_ledger as _sl

_pc.PORTFOLIO_FILE = _PF
_pp.PORTFOLIO_FILE = _PF
_pe.PORTFOLIO_FILE = _PF
_sl.SHADOW_FILE = _LEDGER
_pp.PAPER_TRADE = {"auto_follow_picks": True, "picks_buy_pct": 10.0,
                   "picks_reduce_ratio": 0.5}

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name +
          ((" | " + str(detail)) if detail else ""))
    if not cond:
        failures.append(name)


# 初始组合: 持有 601398 2000 股, 便于验证减仓
init = {
    "capital": 1_000_000.0,
    "cash": 1_000_000.0,
    "positions": {"601398": {"shares": 2000, "avg_cost": 7.5}},
    "trades": [],
    "pending_orders": [],
    "skipped_orders": [],
    "created_at": "2026-08-01",
}
with open(_PF, "w", encoding="utf-8") as f:
    json.dump(init, f)

# ---- 1. 合成 picks → 挂单 ----
result = {
    "date": "2026-08-13",
    "picks": [
        {"code": "600900", "name": "长江电力", "market": "A", "category": "可建仓",
         "price": 28.31, "kelly_final": 8.2, "risk_flags": "PB评分"},
        {"code": "00005", "name": "汇丰控股", "market": "HK", "category": "可建仓",
         "price": 80.0, "kelly_final": 6.0, "risk_flags": ""},
        {"code": "601398", "name": "工商银行", "market": "A", "category": "减仓",
         "price": 7.6, "kelly_final": 5.0, "risk_flags": ""},
        {"code": "600036", "name": "招商银行", "market": "A", "category": "可建仓",
         "price": 0, "kelly_final": 3.0, "risk_flags": ""},
    ],
}

out = _pp.apply_picks_to_paper(result, signal_date="2026-08-13")
check("created 包含 600900+601398", set(out["created"]) == {"600900", "601398"},
      out["created"])
skipped_codes = {s.get("code") for s in out["skipped"]}
check("skipped 包含 00005+600036", skipped_codes == {"00005", "600036"},
      out["skipped"])

pf = _pe._get_paper_portfolio()
orders = {o["code"]: o for o in pf["pending_orders"]}
check("挂单数=2", len(pf["pending_orders"]) == 2, len(pf["pending_orders"]))
o600 = orders.get("600900")
check("600900 buy 挂单(整手 2800, source=daily_picks)",
      o600 and o600["action"] == "buy" and o600["source"] == "daily_picks"
      and o600["shares"] == 2800, o600)
check("600900 带 risk_flags", o600 and o600.get("risk_flags") == "PB评分")
o601 = orders.get("601398")
check("601398 sell 挂单(半仓 1000)",
      o601 and o601["action"] == "sell" and o601["shares"] == 1000, o601)

# ---- 2. 幂等 ----
_pp.apply_picks_to_paper(result, signal_date="2026-08-13")
pf2 = _pe._get_paper_portfolio()
check("幂等: 挂单数不变", len(pf2["pending_orders"]) == 2,
      len(pf2["pending_orders"]))

# ---- 5. 影子台账 ----
rows = []
if os.path.exists(_LEDGER):
    with open(_LEDGER, "r", encoding="utf-8") as f:
        rows = [json.loads(l) for l in f if l.strip()]
check("影子台账记录 00005 (action=buy)",
      len(rows) == 1 and rows[0]["code"] == "00005"
      and rows[0]["action"] == "buy" and rows[0]["market"] == "HK", rows)

# ---- 3. T+1 成交 ----
bars = {
    "600900": {"open": 28.50, "suspended": False},
    "601398": {"open": 7.55, "suspended": False},
}
_pe._execute_pending_orders(pf, bars, datetime.date(2026, 8, 14))
_pc._save_json(_PF, pf)
check("成交后 600900 持仓 2800",
      pf["positions"].get("600900", {}).get("shares") == 2800,
      pf["positions"].get("600900"))
check("成交后 601398 剩余 1000",
      pf["positions"].get("601398", {}).get("shares") == 1000,
      pf["positions"].get("601398"))
buys = [t for t in pf["trades"] if t["action"] == "buy"]
sells = [t for t in pf["trades"] if t["action"] == "sell"]
check("成交记录 buy=1 sell=1", len(buys) == 1 and len(sells) == 1,
      (len(buys), len(sells)))
check("现金已扣减", pf["cash"] < 1_000_000.0, pf["cash"])
check("挂单已清空", pf["pending_orders"] == [], pf["pending_orders"])

# ---- 4. T+1 边界 ----
_pp.apply_picks_to_paper({
    "date": "2026-08-14",
    "picks": [{"code": "601088", "name": "中国神华", "market": "A",
               "category": "可建仓", "price": 40.0, "kelly_final": 4.0,
               "risk_flags": ""}],
}, signal_date="2026-08-14")
pf3 = _pe._get_paper_portfolio()
_pe._execute_pending_orders(
    pf3, {"601088": {"open": 40.0, "suspended": False}},
    datetime.date(2026, 8, 14))
check("T+1: 当日挂单不执行",
      any(o["code"] == "601088" for o in pf3["pending_orders"]),
      pf3["pending_orders"])
check("T+1: 未产生 601088 成交",
      not any(t["code"] == "601088" for t in pf3["trades"]))

shutil.rmtree(_TMP, ignore_errors=True)
if failures:
    print("\nSMOKE FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nSMOKE PASS: 推送→挂单→T+1成交 全链路正常, 港美股影子台账已记录")
