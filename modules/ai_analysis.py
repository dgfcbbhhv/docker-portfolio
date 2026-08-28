"""AI 四维个股分析（技术 / 基本面 / 财务 / 消息面），接入 DeepSeek（OpenAI 兼容）。

复用 build_portfolio_df 产出的指标作为上下文，让 LLM 生成可落盘的复盘。
需要环境变量 DEEPSEEK_API_KEY；缺失时优雅降级（返回 None，不阻断主流程）。
"""
import datetime
import json
import logging
import os

import requests

try:
    from modules import realtime as _rt
except Exception:
    _rt = None

logger = logging.getLogger(__name__)

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "notes")


def _api_key():
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_KEY")


def build_prompt(name, row, rt=None):
    ctx = (
        f"股票 {row.get('code')} {name} 当前指标：\n"
        f"股价 {row.get('price')}，市盈率 {row.get('pe')}，市净率 {row.get('pb')}，\n"
        f"股息率 {row.get('dy')}%，李氏评分 {row.get('lishing')}，Kelly 建议仓位 {row.get('kelly_final')}%，\n"
        f"ROE {row.get('roe')}%，年化波动率 {row.get('vol')}%，市值 {row.get('mcap')}亿，\n"
        f"盈利同比 {row.get('earn_np_yoy')}，评分标记 {row.get('lishing_flags')}。"
    )
    if rt and not rt.get("error"):
        cap = rt.get("capital") or {}
        ind = rt.get("indicators") or {}
        main_wan = round((rt.get("main_net") or 0) / 1e4, 1)
        small_wan = round((cap.get("small_net") or 0) / 1e4, 1)
        ctx += (
            f"\n实时盘面（通达信直连）：现价 {rt.get('price')}（日内 {rt.get('pct')}%）"
            f"，换手率 {rt.get('turnover')}%，量比 {rt.get('vol_ratio')}，涨速 {rt.get('speed_pct')}%；\n"
            f"主力资金：净流入 {main_wan} 万元（散户净 {'流入' if small_wan >= 0 else '流出'} {abs(small_wan)} 万）；\n"
            f"技术指标：MACD(DIF/DEA/HIST)={ind.get('macd_dif')}/{ind.get('macd_dea')}/{ind.get('macd_hist')}"
            f"，KDJ(K/D/J)={ind.get('kdj_k')}/{ind.get('kdj_d')}/{ind.get('kdj_j')}"
            f"，RSI={ind.get('rsi')}，乖离率BIAS={ind.get('bias')}%。"
        )
    prompt = (
        ctx + "\n\n请从以下四个维度，用中文、每条 2-3 句、务实可操作地分析该红利股：\n"
        "1. 技术面：结合上述实时 MACD/KDJ/RSI/乖离率与趋势、支撑压力、量价，判断当前位置\n"
        "2. 基本面：分红持续性 / 护城河 / 行业位置\n"
        "3. 财务面：ROE / 负债 / 现金流质量与风险点\n"
        "4. 资金面：结合上述主力/散户实时净流入，判断资金态度与持续性\n"
        "最后给一句结论：当前是否适合按 Kelly 仓位建 / 加仓，理由一句话。"
    )
    return prompt


def analyze(name, row, model=None):
    key = _api_key()
    if not key:
        logger.warning("[ai_analysis] DEEPSEEK_API_KEY 未配置，跳过")
        return None
    model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    prompt = build_prompt(name, row, rt=row.get("_rt"))
    try:
        resp = requests.post(
            _DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("[ai_analysis] call failed: %s", e)
        return None


def ai_review_candidates(payload=None, top_n=5, worker=None):
    """读取扫描结果，对 Top 候选做 AI 四维分析，落盘 notes/ai_review_<date>.md。"""
    if payload is None:
        p = os.path.join(_DATA_DIR, "dividend_candidates.json")
        if not os.path.exists(p):
            logger.info("[ai_analysis] no candidate file")
            return None
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
    cands = (payload or {}).get("candidates", [])[: int(top_n)]
    if not cands:
        return None
    blocks = [f"# AI 四维复盘 {datetime.datetime.now().strftime('%Y-%m-%d')}\n"]
    for c in cands:
        if _rt is not None:
            try:
                c["_rt"] = _rt.get_realtime_profile(c["code"], with_indicators=True)
            except Exception as e:
                logger.warning("[ai_analysis] realtime %s 失败: %s", c.get("code"), e)
        text = analyze(c["name"], c)
        blocks.append(
            f"## {c['code']} {c['name']}\n\n{c.get('reason', '')}\n\n"
            + (text or "_（未配置 DEEPSEEK_API_KEY 或调用失败）_")
        )
    md = "\n\n".join(blocks)
    os.makedirs(_NOTES_DIR, exist_ok=True)
    out = os.path.join(_NOTES_DIR, "ai_review_%s.md" % datetime.datetime.now().strftime("%Y%m%d"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info("[ai_analysis] wrote %s", out)
    if worker is not None:
        try:
            worker.sender.send_multi("[AI复盘] 红利候选", f"已生成 {len(cands)} 只候选四维分析：{out}")
        except Exception:
            pass
    return out


def run_ai_review(worker=None):
    from modules.config import AI_REVIEW
    return ai_review_candidates(top_n=int(AI_REVIEW.get("top_n", 5)), worker=worker)
