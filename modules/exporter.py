#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出模块：Excel 报告导出
"""
import datetime
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)
EXPORT_DIR = Path("data/exports")
SNAPSHOT_DIR = Path("data/snapshots")


def export_portfolio_excel(df: pd.DataFrame, factor_batch_results: dict = None,
                           cost_data: dict = None, filename: str = None) -> str:
    """
    导出完整持仓分析 Excel 报告
    Sheets:
      1. 持仓概览
      2. Alpha因子分析
      3. 成本分层
      4. Kelly配置
    """
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if filename is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"portfolio_report_{ts}.xlsx"
    filepath = EXPORT_DIR / filename

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        # Sheet 1: 持仓概览
        _export_summary(df, writer)
        # Sheet 2: Alpha因子分析
        if factor_batch_results:
            _export_factors(factor_batch_results, writer)
        # Sheet 3: 成本分层
        if cost_data:
            _export_cost_tier(cost_data, writer)
        # Sheet 4: Kelly配置
        _export_kelly_config(writer)

    return str(filepath)


def _export_summary(df: pd.DataFrame, writer: pd.ExcelWriter):
    """持仓概览 Sheet"""
    cols = ["group", "code", "name", "ratio", "price", "chg", "pe", "pb",
            "dy_lfy", "dy_ttm", "mcap", "roe", "vol",
            "lishing", "kelly_final", "p_signal"]
    out = df[[c for c in cols if c in df.columns]].copy()
    out.columns = ["分组", "代码", "名称", "仓位%", "现价", "涨跌%", "PE", "PB",
                   "股息率LFY%", "股息率TTM%", "市值亿", "ROE%", "波动率%",
                   "李氏评分", "Kelly仓位%", "买入信号"]
    out["买入信号"] = out["买入信号"].map({1: "🟢买入", -1: "🔴卖出", 0: "⚪观望"})
    out.to_excel(writer, sheet_name="持仓概览", index=False)

    # 调整列宽
    ws = writer.sheets["持仓概览"]
    for i, col in enumerate(out.columns, 1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = max(12, len(col) * 2)


def _export_factors(batch_results: dict, writer: pd.ExcelWriter):
    """Alpha因子分析 Sheet"""
    rows = []
    for code, data in batch_results.items():
        if code.startswith("_") or not isinstance(data, dict): continue
        result = data.get("result", {})
        if not result:
            continue
        for r in result.get("recommended", []):
            rows.append({
                "代码": code,
                "因子名": r.get("name", "?"),
                "IR": round(r.get("ir", 0), 4),
                "收益%": round(r.get("annual_return", 0) * 100, 2),
                "回撤%": round(r.get("max_drawdown", 0) * 100, 2),
                "波动率%": round(r.get("volatility", 0) * 100, 2),
                "夏普": round(r.get("sharpe", 0), 3),
            })
    if rows:
        pd.DataFrame(rows).to_excel(writer, sheet_name="Alpha因子", index=False)


def _export_cost_tier(cost_data: dict, writer: pd.ExcelWriter):
    """成本分层 Sheet"""
    rows = []
    for code, data in cost_data.items():
        if not data.get("batches"):
            continue
        batches = data["batches"]
        total_shares = sum(b["shares"] for b in batches)
        total_cost = sum(b["shares"] * b["price"] for b in batches)
        avg_cost = total_cost / total_shares if total_shares > 0 else 0
        dps = data.get("dividend_per_share", 0)
        cost_dy = dps / avg_cost * 100 if avg_cost > 0 else 0
        rows.append({
            "代码": code,
            "批次": len(batches),
            "总股数": total_shares,
            "均价": round(avg_cost, 3),
            "每股分红": round(dps, 4),
            "成本股息率%": round(cost_dy, 2),
        })
    if rows:
        pd.DataFrame(rows).to_excel(writer, sheet_name="成本分层", index=False)


def _export_kelly_config(writer: pd.ExcelWriter):
    """Kelly配置 Sheet"""
    from modules.kelly import MAXPOS
    config = pd.DataFrame([
        {"参数": "Kelly分数阈值", "值": 1.0, "说明": "分数低于此值时不给仓位"},
        {"参数": "最大单票仓位%", "值": MAXPOS * 100, "说明": "单票上限"},
        {"参数": "成长股系数", "值": 1.3, "说明": "美股等成长股加成"},
        {"参数": "价值股系数", "值": 1.0, "说明": "A股港股标准系数"},
    ])
    config.to_excel(writer, sheet_name="Kelly配置", index=False)


def save_snapshot(df: pd.DataFrame) -> str:
    """
    保存持仓快照（用于历史对比）
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now()
    filepath = SNAPSHOT_DIR / f"snapshot_{ts.strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(filepath, index=False, encoding="utf-8-sig")

    # 清理超过30天的快照
    _cleanup_old_snapshots(days=30)
    return str(filepath)


def load_snapshots(days: int = 7, max_count: int = 60) -> list:
    """
    加载最近N天的快照列表
    Args:
        days: 加载最近多少天
        max_count: 最多加载多少个快照（防止文件过多导致卡顿）
    Returns: [(timestamp, df), ...]
    """
    if not SNAPSHOT_DIR.exists():
        return []
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    # 先按文件名过滤日期，再抽样限制数量，最后才读取内容
    candidates = []
    for f in sorted(SNAPSHOT_DIR.glob("snapshot_*.csv")):
        try:
            ts_str = f.stem.replace("snapshot_", "")
            ts = datetime.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts >= cutoff:
                candidates.append((ts, f))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[0])
    # 抽样限制数量，尽量保留首尾快照以显示趋势
    if max_count and len(candidates) > max_count:
        step = len(candidates) / max_count
        idxs = sorted(set(int(i * step) for i in range(max_count)))
        idxs = [i if i < len(candidates) else len(candidates) - 1 for i in idxs]
        candidates = [candidates[i] for i in sorted(set(idxs))]
    snapshots = []
    for ts, f in candidates:
        try:
            snapshots.append((ts, pd.read_csv(f)))
        except Exception:
            continue
    return snapshots


def _cleanup_old_snapshots(days: int = 30):
    """清理超过N天的旧快照"""
    if not SNAPSHOT_DIR.exists():
        return
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    for f in SNAPSHOT_DIR.glob("snapshot_*.csv"):
        try:
            ts_str = f.stem.replace("snapshot_", "")
            ts = datetime.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            if ts < cutoff:
                f.unlink()
        except Exception:
            continue
