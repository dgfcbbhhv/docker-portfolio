"""参数演进 / 校准（回测驱动，report-only，不自动套用）。

思路：复用 picks_evaluator 的建议账本 + 模拟盘快照价格，对关键阈值做反事实扫描，
找出「令买入信号(可建仓)胜率最高」的参数组合，写入 reports/param_evolution_<date>.json。

刻意不做自动套用：避免过拟合 / 静默劣化；报告供人工审阅后决定是否采纳。

依赖：record_picks 需已记录 lishing_total / mos 等特征（2026-08-28 起增强）。
旧账本缺特征时仅输出 baseline，并提示「需累积带特征的新账本」。
"""
import datetime
import json
import logging
import os
from collections import defaultdict

from modules import picks_evaluator as PE

logger = logging.getLogger(__name__)

_DATA_DIR = PE._DATA_DIR
_RECORDS_PATH = PE._RECORDS_PATH
_SNAPSHOT_PATH = PE._SNAPSHOT_PATH
_REPORT_DIR = PE._REPORT_DIR

# 可调参数 + 候选取值（当前默认：买入门槛 65，安全边际门槛 -10）
BUY_TH_CANDIDATES = [60, 65, 70, 75, 80]
MOS_TH_CANDIDATES = [-20, -10, 0, 10, 20]
_HORIZON = 5          # 用 5 日胜率衡量买入信号质量
_MIN_COUNT = 5        # 反事实组合样本下限，低于则不可靠


def _build_snapshot_index():
    snaps = PE._load_json(_SNAPSHOT_PATH)
    if not snaps:
        return None, None, None
    sorted_dates = sorted(snaps.keys())
    date_index = {d: i for i, d in enumerate(sorted_dates)}
    return snaps, sorted_dates, date_index


def _record_return(rec, snaps, sorted_dates, date_index, bench_cache):
    code = rec["code"]
    market = rec["market"]
    date = rec["date"]
    entry = rec.get("entry_price")
    if not entry or entry <= 0 or date not in date_index:
        return None
    start_i = date_index[date]
    ti = start_i + _HORIZON
    if ti >= len(sorted_dates):
        return None
    fdate = sorted_dates[ti]
    px = PE._price_on_date(snaps, fdate, code, market)
    if px is None or px <= 0:
        return None
    ret = (px - entry) / entry * 100
    bench = bench_cache.get(market)
    if bench is None:
        try:
            bench = PE._fetch_benchmark_df(market)
        except Exception:
            bench = None
        bench_cache[market] = bench
    excess = None
    if bench is not None:
        b0 = PE._bench_close(bench, date)
        b1 = PE._bench_close(bench, fdate)
        if b0 and b1:
            excess = ret - (b1 - b0) / b0 * 100
    return ret, excess


def _load_records():
    recs = []
    if not os.path.exists(_RECORDS_PATH):
        return recs
    with open(_RECORDS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    return recs


def run():
    report_date = datetime.date.today().isoformat()
    snaps, sorted_dates, date_index = _build_snapshot_index()
    recs = _load_records()

    # 1) baseline：当前账本下各信号胜率（直接复用 picks_evaluator）
    baseline = PE.evaluate_picks() if snaps else {"error": "无快照数据"}

    # 2) 反事实扫描：仅对「原可建仓且带特征」的记录重新按阈值分类
    if not snaps:
        result = {
            "date": report_date,
            "baseline": baseline,
            "counterfactual": None,
            "note": "无 signal_snapshots，无法计算收益；仅 baseline。",
        }
        _save(result, report_date)
        return result

    feat_recs = [
        r for r in recs
        if r.get("category") == "可建仓" and r.get("decision") == "push"
        and r.get("lishing_total") is not None
    ]
    bench_cache = {}
    # 预计算每条记录收益
    for r in feat_recs:
        rr = _record_return(r, snaps, sorted_dates, date_index, bench_cache)
        r["_ret"] = rr[0] if rr else None
        r["_excess"] = rr[1] if rr else None

    sweep = []
    for bt in BUY_TH_CANDIDATES:
        for mt in MOS_TH_CANDIDATES:
            sel = [r for r in feat_recs
                   if r["lishing_total"] >= bt
                   and (r.get("mos") is None or r["mos"] >= mt)
                   and r.get("_ret") is not None]
            n = len(sel)
            if n < _MIN_COUNT:
                continue
            wins = sum(1 for r in sel if r["_ret"] > 0)
            avg = sum(r["_ret"] for r in sel) / n
            ex = [r["_excess"] for r in sel if r.get("_excess") is not None]
            avg_ex = sum(ex) / len(ex) if ex else None
            sweep.append({
                "buy_th": bt, "mos_th": mt, "n": n,
                "win_rate": round(wins / n * 100, 1),
                "avg_ret": round(avg, 2),
                "avg_excess": round(avg_ex, 2) if avg_ex is not None else None,
            })
    sweep.sort(key=lambda x: (-x["win_rate"], -x["n"]))
    best = sweep[0] if sweep else None

    result = {
        "date": report_date,
        "baseline": baseline,
        "feature_records": len(feat_recs),
        "counterfactual": {
            "horizon": _HORIZON,
            "best": best,
            "all": sweep,
        },
        "note": ("反事实扫描基于带特征的历史可建仓记录；建议采纳 best 组合并观察"
                 "后续胜率是否稳定，再决定是否写入参数。") if best else
                "带特征的可建仓记录不足，暂无法扫描；后续账本累积后自动可用。",
    }
    _save(result, report_date)
    return result


def _save(result, report_date):
    PE._ensure_dirs()
    path = os.path.join(_REPORT_DIR, f"param_evolution_{report_date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    logger.info("[Calibrate] 参数演进报告已生成: %s", path)


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
