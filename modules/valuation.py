# -*- coding: utf-8 -*-
"""主营业务估值引擎 — 贵不贵 / 值不值得买 / 李嘉诚视角 / 芒格视角

口径说明（无分业务披露数据，用整体财务代理主营质量）:
- 主营质量分 = ROE(50) + 经营现金流/净利润(30) + 资产负债率(20)
- 合理PE 由质量分按市场定价; 内在价值 = EPS×合理PE 与 BPS×合理PB 取保守值(取小)
- 安全边际 = (内在价值 - 现价) / 内在价值
- 贵不贵 = f(安全边际); 值不值得买 = 质量门 + 安全边际 + 股息垫
- 李嘉诚视角: 现金 / 低负债 / 股息垫 / 先求不亏再求赢
- 芒格视角: 好生意合理价 / 反过来想

纯逻辑模块, 不依赖 Streamlit, 供界面 Tab 与每日推送共用。
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_ROE = 12.0   # 缺 ROE 时的保守代理值(不参与质量分, 仅用于合理PB估算)


def _num(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _fmt(v, nd=1, suffix=""):
    if v is None:
        return "数据不足"
    return f"{v:.{nd}f}{suffix}"


def market_of(row):
    """从 group/代码 推断市场: A / HK / US / REIT。"""
    group = str(row.get("group", "") or "")
    code = str(row.get("code", "") or "")
    if "美" in group or "US" in group.upper():
        return "US"
    if "港" in group or "HK" in group.upper():
        return "HK"
    if "REIT" in group.upper() or "REIT" in code.upper():
        return "REIT"
    if group.strip():
        return "A"   # 其余持仓分组默认 A股
    if code.isdigit():
        if len(code) == 5:        # 港股 5 位代码
            return "HK"
        if code.startswith(("60", "00", "30", "68", "50")):
            return "A"
        return "HK"
    return "US"


def _roe_pts(roe, market):
    """ROE 越高越好, 满分 50。"""
    table = {
        "A":  [(20, 50), (15, 40), (12, 30), (8, 20), (5, 10)],
        "HK": [(18, 50), (13, 40), (10, 30), (7, 20), (4, 10)],
        "US": [(25, 50), (18, 40), (13, 30), (9, 20), (5, 10)],
    }
    for limit, pts in table.get(market, table["A"]):
        if roe >= limit:
            return pts
    return 0


def _ocf_pts(ocf):
    """经营现金流/净利润, 越高代表主营赚的是真钱, 满分 30。"""
    if ocf is None:
        return 0
    if ocf >= 100:
        return 30
    if ocf >= 80:
        return 24
    if ocf >= 60:
        return 18
    if ocf >= 30:
        return 9
    if ocf > 0:
        return 6
    return 0


def _debt_pts(debt):
    """资产负债率越低越好, 满分 20。"""
    if debt is None:
        return 0
    if debt < 40:
        return 20
    if debt < 50:
        return 16
    if debt < 60:
        return 12
    if debt < 70:
        return 6
    return 0


def quality_score(roe, ocf, debt, market="A"):
    """主营质量分 0-100, 返回 (score, parts, missing)。"""
    roe = _num(roe)
    ocf = _num(ocf)
    debt = _num(debt)
    parts = {
        "roe": _roe_pts(roe, market) if roe is not None else 0,
        "ocf": _ocf_pts(ocf),
        "debt": _debt_pts(debt),
    }
    missing = []
    if roe is None:
        missing.append("ROE")
    if ocf is None:
        missing.append("现金流/净利")
    if debt is None:
        missing.append("负债率")
    return round(parts["roe"] + parts["ocf"] + parts["debt"], 1), parts, missing


def fair_pe(quality, market="A"):
    """质量分 → 合理PE(按市场容忍度)。"""
    q = max(0.0, min(100.0, quality))
    if market == "US":
        return round(15 + q / 100 * 20, 1)   # 15~35
    if market == "HK":
        return round(7 + q / 100 * 10, 1)    # 7~17
    return round(8 + q / 100 * 12, 1)        # A: 8~20


def _intrinsic(price, pe, pb, roe, fair_pe, market):
    """内在价值/股。美股优先 PE 法(成长由盈利承载), A/HK 取 PE/PB 保守小值。"""
    routes = []
    pe = _num(pe)
    pb = _num(pb)
    roe = _num(roe)
    if pe and pe > 0 and price and price > 0:
        routes.append(price / pe * fair_pe)
    if pb and pb > 0 and price and price > 0:
        roe_eff = roe if roe is not None else DEFAULT_ROE
        routes.append(price / pb * fair_pe * roe_eff / 100)
    if not routes:
        return None
    if market == "US":
        return round(routes[0], 2) if routes else None
    return round(min(routes), 2)


def _price_level(mos):
    """安全边际 → 贵不贵。"""
    if mos is None:
        return ("数据不足", "#888")
    if mos >= 30:
        return ("便宜", "#00ff88")
    if mos >= 10:
        return ("合理偏低", "#aaff00")
    if mos >= -10:
        return ("合理", "#ffd166")
    if mos >= -30:
        return ("偏贵", "#ff9f43")
    return ("昂贵", "#ff4444")


def _verdict(quality, mos, dy, market, missing_q=None):
    """质量门 + 安全边际 + 股息 → 值不值得买。"""
    if mos is None or quality is None:
        return "数据不足"
    dy = _num(dy)
    missing_q = missing_q or []
    if quality < 40 and len(missing_q) >= 2:
        return "数据不足：质量分偏低但关键数据缺失，观望"
    if quality < 40:
        return "回避：主营质量差（ROE/现金流/负债不达标）"
    if mos >= 30:
        base = "值得买：安全边际充足"
    elif mos >= 10:
        base = "可以买：价格低于内在价值"
    elif mos >= -10:
        base = "观望：价格合理，等回调再买"
    elif mos >= -30:
        base = "贵了别追：估值已透支"
    else:
        base = "明显高估，回避"
    if dy is not None and dy >= 3 and mos >= 0:
        base += "（股息≥3%，收息垫加分）"
    return base


def _li_view(ocf, debt, dy, mos):
    """李嘉诚视角: 现金/低负债/股息垫/安全边际。"""
    checks = []
    ocf = _num(ocf)
    debt = _num(debt)
    dy = _num(dy)
    if ocf is not None:
        if ocf >= 100:
            checks.append("利润有真金白银支撑（现金流/净利≥100%）")
        elif ocf >= 60:
            checks.append("现金流尚可（现金流/净利≥60%）")
        else:
            checks.append("⚠ 现金流弱于利润，防'纸面富贵'")
    if debt is not None:
        if debt < 40:
            checks.append("低负债，经营稳健")
        elif debt < 60:
            checks.append("杠杆适中")
        else:
            checks.append("⚠ 负债偏高，防守第一")
    if dy is not None:
        if dy >= 4:
            checks.append("股息≥4%，近似收租")
        elif dy >= 3:
            checks.append("股息≥3%，有收息安全垫")
        else:
            checks.append("股息<3%，不能靠收息")
    if mos is not None:
        if mos >= 20:
            checks.append("买价留有安全边际，先求不亏")
        elif mos < 0:
            checks.append("买价已透支，宁可错过")
    missing = []
    if ocf is None:
        missing.append("现金流")
    if debt is None:
        missing.append("负债率")
    warns = [c for c in checks if c.startswith("⚠")]
    if missing:
        conclusion = "数据不足（缺" + "、".join(missing) + "），仅就现有数据评估"
    elif warns:
        conclusion = "谨慎：" + "；".join(warns)
    else:
        conclusion = "稳健：现金实、负债低、有股息垫，符合'先求不亏再求赢'"
    return {"checks": checks, "conclusion": conclusion}


def _munger_view(roe, debt, quality, mos, market):
    """芒格视角: 好生意合理价 / 反过来想。"""
    checks = []
    roe = _num(roe)
    debt = _num(debt)
    roe_high = 20 if market == "US" else 15
    if roe is not None:
        if roe >= roe_high:
            checks.append(f"ROE {roe:.1f}%≥{roe_high}%，生意赚钱能力强")
        elif roe >= 10:
            checks.append(f"ROE {roe:.1f}%，生意质量一般")
        else:
            checks.append(f"⚠ ROE {roe:.1f}%，生意平庸")
    if debt is not None and debt < 40:
        checks.append("低负债，企业韧性好")
    if quality is not None:
        if quality >= 70:
            checks.append("主营质量分≥70，接近'伟大生意'")
        elif quality < 50:
            checks.append("⚠ 主营质量分<50，护城河存疑")
    if mos is not None:
        if mos >= 10:
            checks.append("价格留有安全边际，'合理价买优秀企业'成立")
        elif mos < -30:
            checks.append("⚠ 反过来想：再好的生意，买贵了也是坏投资")
    if quality is None or mos is None:
        conclusion = "数据不足，先补齐 ROE/PE/PB 再看生意与价格"
    elif quality >= 70 and mos >= 0:
        conclusion = "好生意+合理价 → 值得长期持有"
    elif quality >= 70:
        conclusion = "好公司，但当前价格不便宜，等更好的价格"
    elif quality < 50 and mos is not None and mos >= 30:
        conclusion = "便宜有便宜的道理：生意平庸，谨慎抄底"
    else:
        conclusion = "生意质量一般，价格优势不足以弥补，维持观察"
    return {"checks": checks, "conclusion": conclusion}


def valuate(row, market=None):
    """对单只股票行数据做主营业务估值。

    row 需含: price / pe / pb / roe / ocf_ratio / debt / dy
    (eps/bps 可由 price÷pe、price÷pb 推出, 无需额外字段)
    """
    price = _num(row.get("price"))
    pe = _num(row.get("pe"))
    pb = _num(row.get("pb"))
    roe = _num(row.get("roe"))
    ocf = _num(row.get("ocf_ratio"))
    debt = _num(row.get("debt"))
    dy = _num(row.get("dy"))
    mkt = (market or market_of(row)).upper()[:2]
    if mkt not in ("A", "HK", "US"):
        mkt = "A"

    quality, qparts, missing_q = quality_score(roe, ocf, debt, mkt)
    can_price = bool(price and price > 0) and (bool(pe and pe > 0) or bool(pb and pb > 0))
    if not can_price:
        return {
            "code": row.get("code", ""), "name": row.get("name", ""),
            "market": mkt, "available": False, "quality": quality,
            "missing": missing_q + ["PE/PB"],
            "price_level": "数据不足", "price_level_color": "#888",
            "verdict": "数据不足", "margin_of_safety": None,
            "fair_pe": None, "fair_pb": None, "intrinsic": None,
            "li": _li_view(ocf, debt, dy, None),
            "munger": _munger_view(roe, debt, quality, None, mkt),
        }

    fpe = fair_pe(quality, mkt)
    fpb = round(fpe * (roe if roe is not None else DEFAULT_ROE) / 100, 2)
    intrinsic = _intrinsic(price, pe, pb, roe, fpe, mkt)
    mos = round((intrinsic - price) / intrinsic * 100, 1) if intrinsic else None
    level, color = _price_level(mos)
    verdict = _verdict(quality, mos, dy, mkt, missing_q)

    missing = list(missing_q)
    if pe is None and pb is None:
        missing.append("PE/PB")
    if mos is None:
        missing.append("内在价值")

    return {
        "code": row.get("code", ""), "name": row.get("name", ""),
        "market": mkt, "available": True,
        "quality": quality, "quality_parts": qparts,
        "missing": missing,
        "fair_pe": fpe, "fair_pb": fpb, "intrinsic": intrinsic,
        "margin_of_safety": mos,
        "price_level": level, "price_level_color": color,
        "verdict": verdict,
        "li": _li_view(ocf, debt, dy, mos),
        "munger": _munger_view(roe, debt, quality, mos, mkt),
    }


def push_lines(v):
    """推送用估值结论(供 daily_picks 拼接): 贵不贵 + 值不值价值投资 + 李/芒视角。"""
    if not v.get("available"):
        miss = "、".join(v.get("missing") or ["PE/PB"])
        return [f"- 主营业务估值: 数据不足（缺 {miss}）"]
    return [
        f"- 估值贵不贵: {v['price_level']}"
        f"（安全边际 {_fmt(v['margin_of_safety'], 1, '%')}，"
        f"主营质量 {v['quality']:.0f}/100，合理PE {_fmt(v['fair_pe'], 0)}）",
        f"- 值不值价值投资: {v['verdict']}",
        f"- 李嘉诚视角: {v['li']['conclusion']}",
        f"- 芒格视角: {v['munger']['conclusion']}",
    ]