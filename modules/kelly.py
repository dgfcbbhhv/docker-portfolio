"""
Fractional Kelly Criterion — 主观胜率版 + 波动率校准

p = 0.50 + 0.30 * min(股息率/5%, 1.0) + 0.20 * min(ROE/15%, 1.0)
b = 预期回报 / 预期亏损
最终仓位 = Kelly × 0.25 × 波动率校准

波动率校准 (AlphaMaster 风格):
  factor = (基准波动率 / 该股波动率)^exponent
  基准波动率 = 18% (高股息红利股典型值)
  exponent = 0.5 (中等折扣力度)
  结果: 高波动打折, 低波动轻微加分

假设:
- p 为基于股息率与 ROE 的主观胜率估计，非统计概率
- b 基于当前估值修复空间和波动率，未来可能大幅偏离
- quarter Kelly (0.25) 应对参数误差 + 肥尾 + 持仓相关性
"""

FRAC = 0.25        # quarter Kelly
CORR_BASE = 0.75   # 红利策略内部基础相关度
MAXPOS = 30.0      # 单只上限
BENCH_VOL = 18.0   # 基准波动率 %（高股息红利股典型值）
VOL_EXP = 0.50     # 波动率校准指数


def run(price, pb, pe, roe, eps, debt, div, vol, frac=FRAC, maxpos=MAXPOS):
    # ── 主观胜率 p ──
    div_yield = (div / price * 100) if price and price > 0 and div else 0.0
    roe_raw = roe if roe else 0.0

    p = 0.50
    p += 0.30 * min(div_yield / 5.0, 1.0)   # 股息率 ≥5% 拿满
    p += 0.20 * min(roe_raw / 15.0, 1.0)     # ROE ≥15% 拿满
    p = max(min(p, 0.95), 0.05)              # 钳位

    # ── 赔率 b ──
    roe_pct = roe_raw / 100
    if pb and pb > 0:
        if pb < 1.0:
            pb_up = (1.0 - pb) / pb * 100
        else:
            pb_up = roe_pct * 100 * min(pb, 2.0)
        er = div_yield + pb_up
        el = pb * (vol * 100) if vol > 0 else 5.0
    else:
        er = div_yield + roe_pct * 100
        el = vol * 100 if vol > 0 else 5.0

    if el <= 0:
        el = 5.0

    bv = max(0.1, min(10.0, er / el))

    # ── Kelly 公式 ──
    q = 1.0 - p
    if bv > 0:
        k_raw = max(0.0, (bv * p - q) / bv)
    else:
        k_raw = 0.0

    k_quarter = round(k_raw * frac * 100, 1)

    # 相关性折扣
    corr_penalty = 1.0 - CORR_BASE * CORR_BASE
    k_final = round(k_quarter * corr_penalty, 1) if k_quarter > 0 else 0.0
    k_final = min(k_final, maxpos)

    # ── 波动率校准 ──
    vol_adj_factor = vol_adjust(vol)
    k_vol_adj = round(k_final * vol_adj_factor, 1)
    k_vol_adj = min(k_vol_adj, maxpos)

    return {
        "p_signal": round(p, 3),
        "b_odds": round(bv, 2),
        "kelly_raw": round(k_raw * 100, 1),
        "kelly_opt": k_quarter,
        "kelly_final": k_final,
        "kelly_vol_adj": k_vol_adj,
        "vol_adj_factor": round(vol_adj_factor, 3),
        "dividend_yield": round(div_yield, 2),
        "_p_components": {
            "base": 0.50,
            "div_contrib": round(0.30 * min(div_yield / 5.0, 1.0), 3),
            "roe_contrib": round(0.20 * min(roe_raw / 15.0, 1.0), 3),
        },
    }


def vol_adjust(vol, bench_vol=BENCH_VOL, exponent=VOL_EXP):
    """波动率校准因子

    输入: vol — 年化波动率 (如 0.18 表示 18%)
    输出: 校准因子, 乘以仓位后得到波动率校准后仓位

    基准波动率 = bench_vol% (默认 18%)
    波动率 > 基准 -> 因子 < 1.0 (打折)
    波动率 < 基准 -> 因子 > 1.0 (奖励, 但最终会被 maxpos 兜住)
    """
    vol_pct = vol * 100  # 转百分比
    if vol_pct <= 0:
        return 1.0
    factor = (bench_vol / vol_pct) ** exponent
    # 上下限保护: 最大 1.3x, 最小 0.3x
    return max(0.3, min(1.3, factor))


def apply_portfolio_cap(rows, max_total=80.0):
    """组合级总仓位约束：Kelly 合计超限时等比压缩。

    Args:
        rows: 持仓行列表，每行含 kelly_factor_adj（因子调整后仓位 %）。
        max_total: 组合 Kelly 总仓位上限 (%)。

    Returns:
        (rows, total, scale)
        - rows: 每行新增 kelly_portfolio_adj 字段（约束后最终仓位 %）
        - total: 约束前 Kelly 合计 (%)
        - scale: 压缩比例 (1.0 表示未超限)
    """
    if not rows:
        return rows, 0.0, 1.0

    total = sum(float(r.get("kelly_factor_adj", 0) or 0) for r in rows)
    scale = max_total / total if total > max_total > 0 else 1.0

    for r in rows:
        adj = float(r.get("kelly_factor_adj", 0) or 0)
        r["kelly_portfolio_adj"] = round(adj * scale, 1)

    return rows, round(total, 1), round(scale, 4)
