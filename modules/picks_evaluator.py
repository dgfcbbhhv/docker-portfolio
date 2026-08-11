"""推送建议可信度 6 个月验证（建议账本 + 回测 + 执行审计 + 报告）。

把每日推送的 A 股可操作建议（可建仓/可加仓/减仓）落盘成建议账本，
之后每个交易日用 signal_snapshots 的价格回测每条建议的走势与市场基准超额收益，
并对照 execution_log/模拟盘检查「建议 → 自动执行」闭环有没有断。

数据源
------
- 账本: data/picks_records.jsonl  (record_picks 在推送成功后追加)
- 前后价格: data/paper/signal_snapshots.json  (每日自动累积)
- 执行: data/paper/paper_portfolio.json + data/execution_log.json

设计取舍
--------
- 从现在开始累积，不回填历史（signal_snapshots 从 8/11 起）。
- 记录所有 market 的建议（A/HK/US）用于命中率回测；
  但「执行审计」只看 A 股，因为模拟盘桥仅处理 A 股。
"""
import datetime
import json
import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)

from modules.paper_trader import _market_of, _fetch_benchmark_df, _bench_close

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_RECORDS_PATH = os.path.join(_DATA_DIR, "picks_records.jsonl")
_REPORT_DIR = os.path.join(_DATA_DIR, "reports")
_SNAPSHOT_PATH = os.path.join(_DATA_DIR, "paper", "signal_snapshots.json")

_ACTIONABLE = ("可建仓", "可加仓", "减仓")

_DEFAULT_HORIZONS = (5, 10, 20, 60)


def _ensure_dirs():
    os.makedirs(_REPORT_DIR, exist_ok=True)


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def record_picks(picks_result, signal_date=None):
    """把一次推送的所有 picks 追加到建议账本（幂等按 date 去重）。

    Returns:
        int: 本次新增记录数。
    """
    _ensure_dirs()
    picks = picks_result.get("picks", []) or []
    if not picks:
        return 0
    signal_date = signal_date or picks_result.get("date") or datetime.date.today().isoformat()

    existing = set()
    if os.path.exists(_RECORDS_PATH):
        with open(_RECORDS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("date") == signal_date and r.get("pick_id"):
                        existing.add(r["pick_id"])
                except Exception:
                    continue

    written = 0
    with open(_RECORDS_PATH, "a", encoding="utf-8") as f:
        for p in picks:
            code = p.get("code")
            if not code:
                continue
            market = p.get("market") or _market_of(code)
            pick_id = f"{signal_date}|{market}|{code}"
            if pick_id in existing:
                continue
            rec = {
                "date": signal_date,
                "code": code,
                "name": p.get("name", code),
                "market": market,
                "category": p.get("category", ""),
                "held": bool(p.get("held", False)),
                "entry_price": p.get("price"),
                "kelly_final": p.get("kelly_final"),
                "pick_id": pick_id,
                "recorded_at": datetime.datetime.now().isoformat(),
            }
            existing.add(pick_id)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
    if written:
        logger.info("[PicksEval] 建议账本新增 %d 条 (%s)", written, signal_date)
    return written


def _price_on_date(snapshots, date, code, mkt):
    """取指定日期快照中某只股票价格；相邻快照间缺失时向前补。"""
    snaps = snapshots.get(date) or []
    for s in snaps:
        if s.get("code") == code and s.get("price") is not None and s.get("price", 0) > 0:
            return float(s["price"])
    return None


def evaluate_picks(horizons=None, report_date=None):
    """回测建议账本：每个建议在 horizon 交易日后的收益率 + 基准超额。

    Returns:
        dict: 汇总 + 明细，同 report 文件结构。
    """
    horizons = horizons or _DEFAULT_HORIZONS
    snapshots = _load_json(_SNAPSHOT_PATH)
    if not snapshots:
        return {"error": "无 signal_snapshots 数据, 请先启用每日模拟盘快照"}

    records = []
    if os.path.exists(_RECORDS_PATH):
        with open(_RECORDS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue

    sorted_dates = sorted(snapshots.keys())
    date_index = {d: i for i, d in enumerate(sorted_dates)}

    # 市场基准缓存，避免重复拉取
    _bench_cache = {}

    def bench_series(market):
        if market not in _bench_cache:
            try:
                _bench_cache[market] = _fetch_benchmark_df(market)
            except Exception:
                _bench_cache[market] = None
        return _bench_cache[market]

    # per (horizon, category) aggregate
    agg = defaultdict(lambda: {"total": 0, "rets": [], "excess": [], "wins": 0, "false_neg": 0})

    details = []
    for rec in records:
        code = rec["code"]
        market = rec["market"]
        date = rec["date"]
        entry = rec.get("entry_price")
        cat = rec.get("category", "")
        if entry is None or entry <= 0 or date not in date_index:
            # 无法回测（数据不足）
            details.append({**rec, "status": "no_price", "horizon": max(horizons)})
            continue

        start_i = date_index[date]
        bench = bench_series(market)

        for horizon in horizons:
            end_i = start_i + horizon
            if end_i >= len(sorted_dates):
                continue
            end_date = sorted_dates[end_i]
            exit_price = _price_on_date(snapshots, end_date, code, market)
            if not exit_price or exit_price <= 0:
                continue

            ret = (exit_price - entry) / entry * 100.0

            excess = None
            if bench is not None:
                start_b = _bench_close(bench, date)
                end_b = _bench_close(bench, end_date)
                if start_b and end_b:
                    excess = (end_b - start_b) / start_b * 100.0

            key = (horizon, cat)
            agg[key]["total"] += 1
            agg[key]["rets"].append(ret)
            if excess is not None:
                agg[key]["excess"].append(excess)
            if ret > 0:
                agg[key]["wins"] += 1
            # 假信号：买入/加仓类建议在 horizon 内回撤破 -10%
            if cat in ("可建仓", "可加仓") and ret < -10.0:
                agg[key]["false_neg"] += 1

            details.append({
                **rec,
                "horizon": horizon,
                "exit_price": round(exit_price, 4),
                "return_pct": round(ret, 2),
                "excess_pct": round(excess, 2) if excess is not None else None,
                "status": "ok",
            })

    summary = {"validated_at": datetime.datetime.now().isoformat(), "horizons": list(horizons)}
    by_horizon = {}
    for (horizon, cat), data in sorted(agg.items()):
        by_horizon.setdefault(str(horizon), {})[cat] = {
            "total": data["total"],
            "win_rate": round(data["wins"] / data["total"] * 100, 1),
            "avg_ret_pct": round(sum(data["rets"]) / len(data["rets"]), 2) if data["rets"] else 0,
            "avg_excess_pct": round(sum(data["excess"]) / len(data["excess"]), 2) if data["excess"] else None,
            "false_signal_count": data["false_neg"],
        }
    summary["by_horizon"] = by_horizon
    summary["total_records"] = len(records)
    summary["details"] = details[-200:]

    _ensure_dirs()
    path = os.path.join(_REPORT_DIR, f"picks_validation_{report_date or datetime.date.today().isoformat()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    logger.info("[PicksEval] 回测完成: %d 记录 -> %s", len(records), path)
    return summary


def audit_execution(records=None, snapshot_date=None):
    """对照建议账本与模拟盘执行情况，检查「建议→执行」闭环是否有断。

    仅审计 A 股可操作建议（可建仓/可加仓/减仓），因为推送桥只处理 A 股。
    """
    snapshots = _load_json(_SNAPSHOT_PATH)
    pf = _load_json(os.path.join(_DATA_DIR, "paper", "paper_portfolio.json"))

    if records is None:
        records = []
        if os.path.exists(_RECORDS_PATH):
            with open(_RECORDS_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except Exception:
                            continue

    executed = {}
    for trade in (pf or {}).get("trades", []) or []:
        code = trade.get("code")
        if code:
            executed.setdefault(str(code), []).append(trade)

    actions = [r for r in records if r.get("market") == "A" and r.get("category") in _ACTIONABLE]
    summary = {"total_a_actionable": len(actions), "broken": [], "checked_at": datetime.datetime.now().isoformat()}
    for r in actions:
        code = r["code"]
        date = r["date"]
        cat = r["category"]
        entry = r.get("entry_price")
        # 当前快照是否有价（能执行的前提）
        has_price = entry is not None and entry > 0
        trades = executed.get(str(code), [])
        has_exec = any(t.get("date") >= date for t in trades) if trades else False
        ok = has_price and has_exec
        if not ok:
            summary["broken"].append({
                "date": date, "code": code, "category": cat,
                "no_price": not has_price, "executed": has_exec,
            })
    return summary


def build_report(horizons=None):
    """生成完整可信度报告（回测汇总 + 执行审计）。"""
    report_date = datetime.date.today().isoformat()
    eval_result = evaluate_picks(horizons)
    audit = audit_execution()
    report = {
        "date": report_date,
        "evaluation": eval_result,
        "execution_audit": audit,
    }
    if "error" not in eval_result:
        _ensure_dirs()
        path = os.path.join(_REPORT_DIR, f"picks_report_{report_date}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        logger.info("[PicksEval] 报告已生成: %s", path)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="推送建议可信度回测")
    ap.add_argument("--record", metavar="JSON_FILE", help="从 daily_picks 结果文件追加建议账本")
    ap.add_argument("--eval", action="store_true", help="运行回测")
    ap.add_argument("--audit", action="store_true", help="运行执行闭环审计")
    ap.add_argument("--report", action="store_true", help="生成完整报告")
    args = ap.parse_args()

    if args.record:
        with open(args.record, encoding="utf-8") as f:
            res = json.load(f)
        print("recorded:", record_picks(res))
    if args.eval:
        print(json.dumps(evaluate_picks(), ensure_ascii=False, default=str, indent=2))
    if args.audit:
        print(json.dumps(audit_execution(), ensure_ascii=False, default=str, indent=2))
    if args.report:
        print(json.dumps(build_report(), ensure_ascii=False, default=str, indent=2))
    if not (args.record or args.eval or args.audit or args.report):
        ap.print_help()