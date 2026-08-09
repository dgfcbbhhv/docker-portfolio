"""
李氏选股评分引擎 v3 — 红线机制 + 权重回收

假设:
- 红线条件: 审计非标准无保留 / ST / 连续两年经营现金流为负
- 缺失维度: 自动跳过, 权重回收至其他维度
- 关联交易: 默认不计分(数据不可获取), 人工备注可启用
"""
import json
import os

NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "notes")
os.makedirs(NOTES_DIR, exist_ok=True)

DIMENSIONS = {
    "pe":     {"weight": 15, "label": "PE评分"},
    "debt":   {"weight": 10, "label": "负债率"},
    "roe":    {"weight": 15, "label": "ROE评分"},
    "div":    {"weight": 20, "label": "分红评分"},
    "cf":     {"weight": 10, "label": "现金流"},
    "pb":     {"weight": 10, "label": "PB评分"},
    "mcap":   {"weight": 5,  "label": "市值评分"},
    "audit":  {"weight": 5,  "label": "审计意见"},
    "pledge": {"weight": 5,  "label": "质押率"},
}
RPT_W = 5

GRADE_MAP = [
    (85, "🟢 极度安全"),
    (70, "🟡 符合李氏"),
    (55, "🟠 谨慎"),
    (0,  "🔴 高风险"),
]

def grade_label(score):
    for threshold, label in GRADE_MAP:
        if score >= threshold:
            return label
    return "🔴 高风险"

def load_note(code):
    p = os.path.join(NOTES_DIR, f"{code}.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_note(code, data):
    path = os.path.join(NOTES_DIR, f"{code}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def calc(row, market="A"):
    """
    返回:
      total, status, msg, details, flags, use_rpt, note,
      actual_weight, grade, missing_dims
    """
    note = load_note(row.get("code", ""))
    use_rpt = bool(note and note.get("rpt_ratio") is not None)

    # ─── 一票否决（红线） ───
    audit = row.get("audit")
    if audit is not None and audit is not True:
        if isinstance(audit, str) and "标准无保留" not in audit:
            return _red("审计异常: " + audit, note)
        if audit is False:
            return _red("审计未通过", note)

    name = row.get("name", "")
    st_flag = row.get("st_flag", False)
    if st_flag or "ST" in name or "退" in name:
        return _red("ST风险", note)

    ocf_two = row.get("ocf_two_years")
    if ocf_two is not None and ocf_two < 0:
        return _red("连续两年经营现金流为负", note)

    # ─── 正常评分 ───
# ─── 市场特异性阈值 ───
    mkt = market.upper()[:2]
    if mkt == "HK":
        pe_t = [(20, 1.0), (25, 0.8), (30, 0.5), (40, 0.3)]
        pb_t = [(1.0, 1.0), (1.5, 0.8), (2.0, 0.6), (3.0, 0.4), (5.0, 0.2)]
        roe_t = [(12, 1.0), (10, 0.8), (8, 0.6), (5, 0.3)]
    elif mkt == "US":
        pe_t = [(30, 1.0), (40, 0.8), (50, 0.5), (60, 0.3)]
        pb_t = [(3.0, 1.0), (5.0, 0.8), (8.0, 0.6), (12.0, 0.4)]
        roe_t = [(20, 1.0), (15, 0.8), (10, 0.6), (5, 0.3)]
    else:
        pe_t = [(15, 1.0), (20, 0.8), (25, 0.5), (30, 0.3)]
        pb_t = [(0.5, 1.0), (1.0, 0.8), (1.5, 0.6), (2.0, 0.4), (3.0, 0.2)]
        roe_t = [(15, 1.0), (12, 0.8), (8, 0.6), (5, 0.3)]

    def _thresh(val, table):
        """table: list of (limit, score) — find first where val < limit"""
        if val is None:
            return None
        for limit, score in table:
            if val < limit:
                return score
        return table[-1][1] if table else 0.1
    details = {}
    sw = 0   # scored weight
    st = 0   # scored total
    md = []  # missing dims
    fl = []  # flags

    def _add(k, raw_score, weight, label, missing=False):
        nonlocal sw, st
        if missing:
            details[k] = {"score": 0, "raw": None, "weight": weight,
                          "label": label, "missing": True}
            md.append(label)
            return
        s = round(raw_score * weight, 1)
        details[k] = {"score": s, "raw": raw_score, "weight": weight,
                      "label": label, "missing": False}
        sw += weight
        st += s
        if 0 < raw_score < 0.5:
            fl.append(label)

    # PE
    pe = row.get("pe")
    if pe is None or pe <= 0:
        _add("pe", 0.2, DIMENSIONS["pe"]["weight"], "PE评分", missing=True)
    else:
        v = _thresh(pe, pe_t)
        _add("pe", v, DIMENSIONS["pe"]["weight"], "PE评分")

    # 负债率
    debt = row.get("debt")
    if debt is None:
        _add("debt", 0.3, DIMENSIONS["debt"]["weight"], "负债率", missing=True)
    else:
        v = 1.0 if debt < 40 else (0.8 if debt < 50 else (0.6 if debt < 60 else (0.3 if debt < 70 else 0.0)))
        _add("debt", v, DIMENSIONS["debt"]["weight"], "负债率")

    # ROE
    roe = row.get("roe")
    if roe is None:
        _add("roe", 0.3, DIMENSIONS["roe"]["weight"], "ROE评分", missing=True)
    else:
        # ROE uses inverted logic (higher is better)
        for limit, score in roe_t:
            if roe > limit:
                v = score
                break
        else:
            v = roe_t[-1][1] if roe_t else 0.1
        _add("roe", v, DIMENSIONS["roe"]["weight"], "ROE评分")

    # 分红
    dy = row.get("dy", 0) or 0
    yrs = row.get("div_years", 0) or 0
    if yrs >= 5 and dy > 4:  dv = 1.0
    elif yrs >= 3 and dy > 3: dv = 0.8
    elif yrs >= 3:           dv = 0.6
    elif dy > 3:             dv = 0.5
    elif dy > 0:             dv = 0.2
    else:                    dv = 0.0
    _add("div", dv, DIMENSIONS["div"]["weight"], "分红评分")

    # 现金流
    ocf = row.get("ocf_ratio")
    if ocf is None:
        _add("cf", 0.3, DIMENSIONS["cf"]["weight"], "现金流", missing=True)
    else:
        v = 1.0 if ocf > 100 else (0.8 if ocf > 80 else (0.5 if ocf > 60 else (0.3 if ocf > 0 else 0.0)))
        _add("cf", v, DIMENSIONS["cf"]["weight"], "现金流")

    # PB
    pb = row.get("pb")
    if pb is None or pb <= 0:
        _add("pb", 0.2, DIMENSIONS["pb"]["weight"], "PB评分", missing=True)
    else:
        v = _thresh(pb, pb_t)
        _add("pb", v, DIMENSIONS["pb"]["weight"], "PB评分")

    # 市值
    mcap = row.get("mcap", 0) or 0
    if mcap <= 0:
        _add("mcap", 0.2, DIMENSIONS["mcap"]["weight"], "市值评分", missing=True)
    else:
        v = 1.0 if mcap > 1000 else (0.8 if mcap > 500 else (0.6 if mcap > 200 else (0.4 if mcap > 100 else (0.2 if mcap > 50 else 0.1))))
        _add("mcap", v, DIMENSIONS["mcap"]["weight"], "市值评分")

    # 审计意见 / 质押率: 当前无可靠数据源, 显式计为缺失, 不默认给满分,
    # 避免评分天花板虚高。后续接入真实数据源后改为实际计分。
    _add("audit", 0.2, DIMENSIONS["audit"]["weight"], "审计意见", missing=True)
    _add("pledge", 0.2, DIMENSIONS["pledge"]["weight"], "质押率", missing=True)

    # 关联交易
    if use_rpt:
        rpt = note.get("rpt_ratio", 0)
        rv = 1.0 if rpt < 10 else (0.5 if rpt < 30 else 0.0)
        _add("rpt", rv, RPT_W, "关联交易")
    else:
        details["rpt"] = {"score": 0, "raw": None, "weight": RPT_W,
                          "label": "关联交易(无数据)", "missing": True}
        md.append("关联交易")

    # ─── 盈利趋势标记（可选数据，来自 modules.earnings） ───
    for _f in (row.get("earn_flags") or []):
        if _f not in fl:
            fl.append(_f)

    # ─── 权重回收 ───
    if sw == 0:
        return _result(0, "warning", "无有效数据", details, fl, use_rpt, note, 0, md)
    final = round(st / sw * 100, 1)

    status = "normal"
    msg_parts = [grade_label(final)]
    if fl:
        status = "warning"
        msg_parts.append(f"⚠ 不达标项: {len(fl)} 个")
    if md:
        status = "warning" if status == "normal" else status
        msg_parts.append(f"缺失: {', '.join(md)}，已折算")
    msg = " | ".join(msg_parts)

    return _result(final, status, msg, details, fl, use_rpt, note, sw, md)


def _result(total, status, msg, details, flags, use_rpt, note, aw, md):
    return {
        "total": total,
        "status": status,
        "msg": msg,
        "details": details,
        "flags": flags,
        "use_rpt": use_rpt,
        "note": note,
        "actual_weight": aw,
        "grade": grade_label(total),
        "missing_dims": md,
    }

def _red(msg, note=None):
    return _result(0, "red", msg, {}, [msg], False, note or {}, 0, [])
