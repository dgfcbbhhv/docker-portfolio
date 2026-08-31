"""AI 四维个股分析（技术 / 基本面 / 财务 / 消息面），接入 OpenAI 兼容 LLM。

复用 build_portfolio_df 产出的指标作为上下文，让 LLM 生成可落盘的复盘。
provider 通过环境变量切换（优先级 AI_REVIEW_* > DEEPSEEK_*），缺失时优雅降级。
- AI_REVIEW_BASE_URL / DEEPSEEK_API_KEY 对应的默认端点为 DeepSeek
- AI_REVIEW_API_KEY / OPENROUTER_API_KEY / DEEPSEEK_API_KEY 任一即可
- AI_REVIEW_MODEL / DEEPSEEK_MODEL 指定模型（如 deepseek-chat / deepseek/deepseek-chat）
"""
import datetime
import json
import logging
import os
import re

import requests

try:
    from modules import realtime as _rt
except Exception:
    _rt = None

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "notes")


def _ai_review_url():
    return os.environ.get("AI_REVIEW_BASE_URL",
                           "https://api.deepseek.com/v1/chat/completions")


def _api_key():
    return (os.environ.get("AI_REVIEW_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_KEY"))


def _ai_review_model():
    return os.environ.get("AI_REVIEW_MODEL") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _ai_review_model_b():
    """交叉校验用第二模型；未配置则返回 None（表示不做双模型）。"""
    return (os.environ.get("AI_REVIEW_MODEL_B") or os.environ.get("DEEPSEEK_MODEL_B")
            or os.environ.get("AI_REVIEW_MODEL_ALT") or None)


def _call_llm(model, prompt, temperature=0.1):
    key = _api_key()
    if not key:
        return None
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if int(os.environ.get("AI_REVIEW_MAX_TOKENS", "0") or 0) > 0:
        payload["max_tokens"] = int(os.environ["AI_REVIEW_MAX_TOKENS"])
    resp = requests.post(
        _ai_review_url(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _schedule_temp(progress):
    """动态温度调度：长文本主体用略高温度(保叙事多样性)，收敛到低温度(保稳定)。

    progress ∈ [0,1] 表示生成进度；单次 API 请求无法流式分段，故按用途取值：
      - progress>=1  : 复盘正文(长文本)，用 AI_REVIEW_TEMP_MAIN(默认0.2)
      - progress<1   : 结论/复核(短文本)，用 0.0 求确定性
    """
    if progress >= 1:
        try:
            return float(os.environ.get("AI_REVIEW_TEMP_MAIN", "0.2"))
        except (TypeError, ValueError):
            return 0.2
    return 0.0


def _summarize_announcements(ann_list, n=30):
    recent = (ann_list or [])[:n]
    cats = {
        "分红": ["分红", "派息", "利润分配", "送转", "派发"],
        "业绩": ["业绩", "季报", "半年", "年报", "预告", "经营"],
        "增减持": ["减持", "增持", "回购"],
        "风险": ["违规", "处罚", "警示", "停牌", "风险", "立案", "问询"],
    }
    lines = []
    for a in recent:
        t = a.get("title", "")
        cat = "其他"
        for c, kws in cats.items():
            if any(k in t for k in kws):
                cat = c
                break
        lines.append(f"  - {a.get('date', '????-??-??')} [{cat}] {t}")
    if not lines:
        return f"近{n}日公告: 无"
    return f"近期公告清单（共{len(lines)}条，均来自交易所公开披露）:\n" + "\n".join(lines)


def _portfolio_context():
    """构建『真实持仓与组合构成』文本块, 供 AI 结合上下文(封闭视野缺陷 #4)。

    真实持仓以 broker_trades.json(真实成交) 为权威源(get_real_holdings_view),
    而非看板目标仓位。返回 (text_block_or_None, held_weight_map)。
    """
    try:
        from modules.daily_picks import get_real_holdings_view
        view = get_real_holdings_view()
    except Exception as e:
        logger.warning("[ai_analysis] 持仓上下文获取失败: %s", e)
        return None, {}
    if not view:
        return None, {}
    held = {}
    lines = ["【我的真实持仓与组合构成（真实成交口径，仅供结合上下文，勿外传）】:"]
    by_group = {}
    for h in view:
        code = str(h.get("code"))
        name = h.get("name", code)
        rw = h.get("real_weight")
        target = h.get("target")
        grp = h.get("group", "") or ""
        if rw is not None:
            try:
                held[code] = float(rw)
            except (TypeError, ValueError):
                pass
            by_group.setdefault(grp, 0.0)
            by_group[grp] += float(rw)
        seg = [f"{code} {name}"]
        if grp:
            seg.append(f"[{grp}]")
        if target is not None:
            seg.append(f"目标{target}%")
        if rw is not None:
            seg.append(f"真实占比{rw:.1f}%")
        if h.get("shares") is not None:
            seg.append(f"{h['shares']}股")
        seg.append(f"价{h.get('price')} PE{h.get('pe')} PB{h.get('pb')} 股息{h.get('dy')}% "
                   f"ROE{h.get('roe')}% 负债{h.get('debt')}%")
        lines.append("  - " + " ".join(str(x) for x in seg))
    if by_group:
        alloc = "，".join(f"{g}:{v:.1f}%" for g, v in by_group.items() if v)
        lines.append(f"组合权重分布(真实): {alloc}")
    return "\n".join(lines), held


def _analyst_forecast(code):
    """机构一致预期(同花顺): 覆盖机构数 + 预期EPS同比%。返回 dict 或 None。"""
    if not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return None
    try:
        import akshare as ak
        df = ak.stock_profit_forecast_ths(symbol=code)
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        n = None
        if "预测机构数" in df.columns:
            try:
                n = int(float(last["预测机构数"]))
            except (TypeError, ValueError):
                n = None
        growth = None
        if "均值" in df.columns and len(df) >= 2:
            try:
                v0, v1 = float(df.iloc[-2]["均值"]), float(df.iloc[-1]["均值"])
                if v0 and v0 > 0:
                    growth = (v1 - v0) / v0 * 100
            except (TypeError, ValueError):
                growth = None
        if n is None and growth is None:
            return None
        return {"n": n, "eps_growth": growth}
    except Exception as e:
        logger.warning("[ai_analysis] 机构预期 %s 失败: %s", code, e)
        return None


def _northbound_net_flow():
    """北向资金(陆股通)当日净流向(亿元)。返回 dict 或 None。"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            return None
        sub = df[df.get("资金方向") == "北向"]
        if sub.empty:
            return None
        col = "资金净流入" if "资金净流入" in df.columns else "成交净买额"
        total = float(sub[col].fillna(0).sum())
        dt = None
        try:
            dt = str(sub.iloc[0].get("交易日"))
        except Exception:
            dt = None
        return {"net_yi": total, "date": dt}
    except Exception as e:
        logger.warning("[ai_analysis] 北向资金获取失败: %s", e)
        return None


def _extra_info_block(code, market, northbound):
    """扩展信息面文本块(机构预期 + 北向资金)。无数据返回 None。"""
    parts = []
    af = _analyst_forecast(code)
    if af:
        s = []
        if af.get("n"):
            s.append(f"{af['n']}家机构覆盖")
        if af.get("eps_growth") is not None:
            s.append(f"一致预期EPS同比约{af['eps_growth']:.0f}%")
        if s:
            parts.append("机构一致预期: " + "，".join(s))
    if northbound:
        d = northbound.get("net_yi")
        if d is not None:
            direction = "净流入" if d >= 0 else "净流出"
            dt = northbound.get("date") or ""
            parts.append(f"北向资金({dt})当日{direction}约{abs(d):.1f}亿元")
    if not parts:
        return None
    return "；".join(parts) + "。"


def build_prompt(name, row, rt=None, portfolio_ctx=None, held_info=None, extra_info=None):
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
            f"技术指标：MACD(DIF/DEA/HIST)={ind.get('MACD_DIF')}/{ind.get('MACD_DEA')}/{ind.get('MACD_HIST')}"
            f"，KDJ(K/D/J)={ind.get('KDJ_K')}/{ind.get('KDJ_D')}/{ind.get('KDJ_J')}"
            f"，RSI={ind.get('RSI')}，乖离率BIAS={ind.get('BIAS1')}%。"
        )
        ann = rt.get("announcements")
        if isinstance(ann, list) and ann:
            ctx += "\n" + _summarize_announcements(ann)
        sec = rt.get("sector")
        if isinstance(sec, list) and sec:
            top = "，".join(
                f"{s.get('name')}(主力+{s.get('main_net_yi')}亿,涨{s.get('change_pct')}%)"
                for s in sec[:5]
            )
            ctx += f"\n行业资金风向（主力净流入前5）: {top}。"
    extra = ""
    if portfolio_ctx:
        extra += portfolio_ctx + "\n\n"
    if held_info:
        extra += f"【该标的与我的持仓关系】{held_info}\n\n"
    if extra_info:
        extra += f"【扩展信息面（机构预期/北向资金，来源公开数据）】{extra_info}\n\n"
    prompt = (
        extra + ctx + "\n\n请从以下六个维度，用中文、每条 2-3 句、务实可操作地分析该红利股：\n"
        "1. 技术面：结合上述实时 MACD/KDJ/RSI/乖离率与趋势、支撑压力、量价，判断当前位置\n"
        "2. 基本面：分红持续性 / 护城河 / 行业位置\n"
        "3. 财务面：ROE / 负债 / 现金流质量与风险点\n"
        "4. 资金面：结合上述主力/散户实时净流入与行业资金风向，判断资金态度与持续性\n"
        "5. 消息面：结合下方『近期公告清单』，提示需关注的事件与风险；只能引用清单内已列明的内容\n"
        "6. 信息面(扩展)：若上方【扩展信息面】提供了机构一致预期与北向资金，请结合判断预期差与聪明钱方向；未提供则跳过。\n"
        "\n【数据纪律（必须严格遵守，全流程硬性强制）】\n"
        "- 只能依据上面『当前指标』『实时盘面』『扩展信息面』字段与『近期公告清单』作答，不得编造任何数字。\n"
        "- 【数值/定性必须开箱分离】凡讨论未在输入中提供的具体数值（资本充足率、不良率、拨备覆盖率、"
        "除权除息日、每股分红金额、EPS/BPS、净资产负债、现金流占比等），一律不得写出具体数字，"
        "只能写『数据未提供，无法量化』或极简定性词（如『较高/稳定』），并把它归入『定性判断』栏。\n"
        "- 除输入『当前指标』『实时盘面』给出的数字外，禁止凭空输出任何百分数、金额、币值、日期、股本、"
        "分红金额。若你认为某数字确有必要而它又不在输入中，必须明确写『（未提供，存疑）』。\n"
        "- 技术面的支撑/压力价位若给出，必须标注『（模型估计，非成交数据）』；若输入未提供则只写方向不做价。\n"
        "- 公告只能引用清单内的日期与标题，不得推断清单外的日期或金额。\n"
        "- 【输出格式硬性要求】先以『**硬数字**』小节：仅复述输入已给出的数字（股价/PE/PB/股息/ROE/资金流/"
        "机构预期/北向，可附单位），每项后标 `(依据输入)`；再以『**定性判断**』小节做分析叙述，"
        "这两栏必须严格分开，禁止在叙述中夹带未提供的数字。\n"
        "最后另起一行给一句结论：当前是否适合按 Kelly 仓位建 / 加仓，理由一句话。"
    )
    return prompt


_AUDIT = []


def _reset_audit():
    _AUDIT.clear()


def analyze(name, row, model=None, portfolio_ctx=None, held_info=None, extra_info=None,
            cross_check=False, self_consistency=0):
    """对单只股票生成四维复盘。temperature 固定 0.1 降低发散。

    cross_check=True      双模型交叉：主模型 + 第二模型(AI_REVIEW_MODEL_B)独立结论，
                          分歧则在文末标注。
    self_consistency=N    (N>1 生效)同主模型多次采样取多数结论，降低单次采样噪声。
    """
    key = _api_key()
    if not key:
        logger.warning("[ai_analysis] AI_REVIEW_API_KEY / DEEPSEEK_API_KEY 未配置，跳过")
        return None
    model = model or _ai_review_model()
    prompt = build_prompt(name, row, rt=row.get("_rt"),
                          portfolio_ctx=portfolio_ctx, held_info=held_info,
                          extra_info=extra_info)
    audit = {"code": str(row.get("code")), "name": name,
             "model": model, "cross_check": bool(cross_check),
             "self_consistency": int(self_consistency), "flags": []}
    try:
        # ---- 同模型多次采样 Self-Consistency ----
        n_samples = int(self_consistency)
        if n_samples > 1:
            texts, verdicts = [], []
            for _ in range(n_samples):
                t = _call_llm(model, prompt, temperature=_schedule_temp(1.0))
                if t:
                    texts.append(t)
                    verdicts.append(_first_verdict(t) or "")
            _AUDIT.append(audit)
            if not texts:
                return None
            text = _majority_text(texts, verdicts)
            audit["self_flagged"] = _majority_disagreed(verdicts)
            audit["self_verdicts"] = [v for v in verdicts if v]
            return text

        text = _call_llm(model, prompt, temperature=_schedule_temp(1.0))
        if not text:
            _AUDIT.append(audit)
            return None
        if not cross_check:
            _AUDIT.append(audit)
            return text
        model_b = _ai_review_model_b()
        if not model_b:
            _AUDIT.append(audit)
            return text
        try:
            verdict_b = _call_llm(model_b, prompt + "\n\n只输出一句话结论（是否适合按 Kelly 建/加仓）", temperature=0.0)
        except Exception as e:
            logger.warning("[ai_analysis] 副模型交叉核验失败(%s)，回退仅用主模型: %s", model_b, e)
            audit["flags"].append(f"副模型({model_b})限流/失败，仅用主模型")
            _AUDIT.append(audit)
            return text
        if not verdict_b:
            _AUDIT.append(audit)
            return text
        verdict_a = _first_verdict(text)
        audit["cross_verdict_a"] = verdict_a
        audit["cross_verdict_b"] = (verdict_b or "").strip()
        if verdict_a is None:
            _AUDIT.append(audit)
            return text
        verdict_b = _first_verdict(verdict_b) or verdict_b.strip()
        if _verdict_agree(verdict_a, verdict_b):
            _AUDIT.append(audit)
            return text
        audit["flags"].append(f"双模型结论不一致({model} vs {model_b})")
        _AUDIT.append(audit)
        return text + f"\n\n⚠️【双模型交叉核验】主模型({model})与次模型({model_b})结论不一致：\n" \
                      f"- 主：{verdict_a}\n- 次：{verdict_b}\n建议人工复核后再决策。"
    except Exception as e:
        logger.warning("[ai_analysis] call failed: %s", e)
        _AUDIT.append(audit)
        return None


def _majority_text(texts, verdicts):
    """Self-Consistency：多数结论对应的文本优先；无多数则返回第一个。"""
    votes = {}
    for i, v in enumerate(verdicts):
        tone = _tone_of(v)
        votes.setdefault(tone, []).append(i)
    best = max(votes.values(), key=len, default=[])
    if len(best) >= 2 and len(best) > len([i for i in range(len(texts)) if i not in best]):
        return texts[best[0]]
    return texts[0]


def _majority_disagreed(verdicts):
    tones = [_tone_of(v) for v in verdicts if v]
    pos = sum(1 for t in tones if t == 1)
    neg = sum(1 for t in tones if t == -1)
    return len(tones) >= 2 and pos > 0 and neg > 0


def _tone_of(s):
    if not s:
        return 0
    hard_neg = ("不建仓", "不加仓", "不适合", "不建议", "不宜", "回避", "减仓")
    if any(k in s for k in hard_neg):
        return -1
    hard_pos = ("建仓", "加仓", "适合", "建议建", "建议加")
    if any(k in s for k in hard_pos):
        return 1
    return 0


def _first_verdict(text):
    text = (text or "").strip()
    for line in reversed(text.splitlines()):
        s = line.strip()
        if not s:
            continue
        if any(k in s for k in ("建仓", "加仓", "减仓", "观望", "不建仓", "不加仓", "适合", "不适合", "建议")):
            return s[:120]
    return None


def _verdict_agree(a, b):
    if not a or not b:
        return True
    ta, tb = _tone_of(a), _tone_of(b)
    if ta == 0 or tb == 0:
        return True
    return ta == tb


_NUM_PAT = re.compile(r"-?\d+(?:\.\d+)?")
_DIV_YI_PAT = re.compile(r"(\d+(?:\.\d+)?)\s*(万亿|亿|万|元|万元|亿元|%|倍)")
_DATE_PAT = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?")


def _ground_truth_numbers(code, row, rt, extra_info):
    """收集喂给 LLM 的真实硬数字，作为反幻觉校验的地基。"""
    known = set()
    known |= {
        ch for ch in (str(row.get("price")), str(row.get("pe")), str(row.get("pb")),
                      str(row.get("dy")), str(row.get("lishing")), str(row.get("kelly_final")),
                      str(row.get("roe")), str(row.get("vol")), str(row.get("mcap")),
                      str(row.get("earn_np_yoy")))
        if ch and ch not in ("None", "nan", "0", "0.0", "0.00", "")
    }
    if isinstance(extra_info, str) and extra_info:
        for m in _NUM_PAT.finditer(extra_info):
            v = float(m.group())
            known.add(_fmt(v))
    if rt and not rt.get("error"):
        known |= {
            ch for ch in (str(rt.get("price")), str(rt.get("pct")), str(rt.get("turnover")),
                          str(rt.get("vol_ratio")), str(rt.get("speed_pct")),
                          str((rt.get("main_net") or 0) / 1e4))
            if ch and ch not in ("None", "nan", "0", "0.0", "")
        }
        ind = rt.get("indicators") or {}
        for k in ("MACD_DIF", "MACD_DEA", "MACD_HIST", "KDJ_K", "KDJ_D", "KDJ_J", "RSI", "BIAS1"):
            if ind.get(k) is not None:
                known.add(_fmt(float(ind[k])))
        cap = rt.get("capital") or {}
        if cap.get("small_net") is not None:
            known.add(_fmt((cap["small_net"] or 0) / 1e4))
    return known


def _fmt(x):
    return ("%.4f" % float(x)).rstrip("0").rstrip(".")


def _validate_output(code, name, text, row, rt, extra_info):
    """对 LLM 输出做程序化反幻觉核对：数值追源 + 公告标题匹配 + 存疑标记。

    返回 (clean_text, issues)：
      - clean_text: 在可疑对象后追加『⚠️存疑』标记的文本
      - issues:     存疑项列表（可写入复盘末尾【数据校验】节）
    """
    if not text:
        return text, []
    issues = []
    ground = _ground_truth_numbers(code, row, rt, extra_info)

    # 1) 数值追源：带单位/百分号的『具体数字』若不在输入地基则标存疑
    suspect_tokens = []
    for m in _DIV_YI_PAT.finditer(text):
        tok = m.group(0).strip()
        rounded = _fmt(float(m.group(1)))
        if rounded in ground:
            continue
        if tok not in suspect_tokens:
            suspect_tokens.append(tok)
    # 1b) 对存疑数值做二次触发核对（轻量 RAG）：资金流等可廉价重拉真实值比对
    suspect_tokens = _recheck_suspect_tokens(suspect_tokens, code, rt)
    for tok in suspect_tokens:
        issues.append(f"数值 '{tok}' 未在输入数据中且复核未通过（可能 AI 编造或估计），请人工核实。")

    # 2) 公告日期/标题二次核验：只能引用输入公告清单
    if rt and not rt.get("error"):
        ann_list = rt.get("announcements") or []
        known_titles = {str(a.get("title", "")).strip() for a in ann_list}
        known_dates = {str(a.get("date", ""))[:10] for a in ann_list}
        for m in _DATE_PAT.finditer(text):
            d = "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d in known_dates:
                continue
            issues.append(f"日期 {d} 不在公告清单中（可能 AI 推断），请人工核实。")
            # 日期二次触发核对：尝试从公告流重新拉取（轻量 RAG）
            if _recheck_date(d, code, rt):
                issues.pop()  # 复核命中，移除该存疑项
        # 标题核验：抓取文本中『《XX》』或『XX公告/报告』样式的疑似公告名，比对清单。
        # 限制：必须以 CJK 开头、不含"将/已/于/在"等动词语气词，降低对普通句子的误报。
        for m in re.finditer(r"[《““]?([\u4e00-\u9fa5A-Za-z0-9]{4,30}?(?:公告|报告|预案|方案|分配|减持|回购|提示|函))[》””]?", text):
            title = m.group(1).strip()
            if not title or title[0] not in "《““":
                if any(v in title for v in ("将", "已", "于", "在", "披露", "发布", "公布", "出具", "召开", "相关", "以及", "以及")):
                    continue
            if any(t == title or title in t or t in title for t in known_titles):
                continue
            issues.append(f"标题『{title}』不在公告清单中（可能 AI 编造/推断），请人工核实。")

    # 3) 在可疑对象处打标（正文追加标记行，不侵入原句）
    flags = []
    if suspect_tokens:
        flags.append("⚠️ AI 输出含无法从输入追源的数值：" + "、".join(suspect_tokens[:8]) +
                     ("…" if len(suspect_tokens) > 8 else "") + "，已列于文末【数据校验】。")
    if flags:
        text = text + "\n\n" + "\n".join(flags)
    return text, issues


def _recheck_suspect_tokens(suspect_tokens, code, rt):
    """关键数值二次触发核对（轻量 RAG）。

    对疑似存疑数值，若属【资金流】类（净流入/流出 + 万/亿），重拉真实实时资金流比对；
    命中则在容差内判定为『已复核』并从存疑清单移除。其余数值无法廉价核对，保持存疑。
    任何异常一律静默降级（不阻断主流程）。
    """
    if not suspect_tokens:
        return suspect_tokens
    try:
        if _rt is None:
            return suspect_tokens
        prof = _rt.get_realtime_profile(code, with_indicators=False, with_announcements=False)
        if not prof or prof.get("error"):
            return suspect_tokens
        main_wan = (prof.get("main_net") or 0) / 1e4
        cap = prof.get("capital") or {}
        small_wan = (cap.get("small_net") or 0) / 1e4
        real_flows = [abs(main_wan), abs(small_wan)]
        kept = []
        for tok in suspect_tokens:
            head = tok.split("万")[0].split("亿")[0]
            m = _NUM_PAT.search(head)
            if m is None:
                kept.append(tok)
                continue
            val = abs(float(m.group()))
            # 容差匹配：AI 四舍五入的金额视为命中（相对误差 <= 5%）
            if any(_close_enough(val, r) for r in real_flows if r > 0):
                continue  # 复核命中，移除
            kept.append(tok)
        return kept
    except Exception as e:
        logger.warning("[ai_analysis] 数值二次复核失败(降级): %s", e)
        return suspect_tokens


def _close_enough(a, b, tol=0.05):
    """相对容差比较：|a-b| <= tol*max(|a|,|b|)。"""
    if not a and not b:
        return True
    base = max(abs(a), abs(b))
    if base == 0:
        return a == b
    return abs(a - b) <= tol * base


def _recheck_date(date_str, code, rt):
    """公告日期二次触发核对（轻量 RAG）：重拉公告流，若命中该日期则判为已复核。"""
    try:
        if _rt is None:
            return False
        prof = _rt.get_realtime_profile(code, with_indicators=False, with_announcements=True)
        if not prof or prof.get("error"):
            return False
        ann_list = prof.get("announcements") or []
        return any(str(a.get("date", ""))[:10] == str(date_str)[:10] for a in ann_list)
    except Exception as e:
        logger.warning("[ai_analysis] 日期二次复核失败(降级): %s", e)
        return False


def _write_metrics(validation):
    """幻觉率统计 + 审计日志落盘（notes/hallucination_metrics*.json + 审计 trace）。

    - hallucination_metrics_<date>.json : 当日明细
    - hallucination_metrics.json         : 滚动历史（追加每日摘要）
    """
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        run_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        # 给 audit 记录并入 validation 存疑项
        for rec in _AUDIT:
            code = str(rec.get("code"))
            rec["suspect_count"] = len(validation.get(code) or [])
            rec["suspect_items"] = (validation.get(code) or [])[:20]
        n_total = len(_AUDIT)
        n_flagged = sum(1 for r in _AUDIT if (r.get("suspect_count") or 0) > 0 or r.get("flags"))
        n_self_disagree = sum(1 for r in _AUDIT if r.get("self_flagged"))
        n_cross_disagree = sum(1 for r in _AUDIT if any("不一致" in f for f in (r.get("flags") or [])))
        daily = {
            "run_id": run_id,
            "date": today,
            "stocks_total": n_total,
            "stocks_flagged": n_flagged,
            "flagged_rate": round(n_flagged / n_total, 4) if n_total else 0.0,
            "self_consistency_disagree": n_self_disagree,
            "cross_check_disagree": n_cross_disagree,
            "records": _AUDIT,
        }
        mdir = _NOTES_DIR
        os.makedirs(mdir, exist_ok=True)
        with open(os.path.join(mdir, "hallucination_metrics_%s.json" % today.replace("-", "")), "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False, indent=2)
        # 滚动历史
        hist_path = os.path.join(mdir, "hallucination_metrics.json")
        hist = {}
        if os.path.exists(hist_path):
            try:
                with open(hist_path, encoding="utf-8") as f:
                    hist = json.load(f)
            except Exception:
                hist = {}
        hist[today] = {
            "total": n_total,
            "flagged": n_flagged,
            "flagged_rate": daily["flagged_rate"],
            "self_disagree": n_self_disagree,
            "cross_disagree": n_cross_disagree,
        }
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
        logger.info("[ai_analysis] metrics: %d stocks, %d flagged (rate=%.1f%%)",
                    n_total, n_flagged, daily["flagged_rate"] * 100)
    except Exception as e:
        logger.warning("[ai_analysis] write metrics failed: %s", e)


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
    sector = None
    if _rt is not None:
        try:
            sector = _rt.get_sector_sentiment(5)
        except Exception as e:
            logger.warning("[ai_analysis] sector sentiment 失败: %s", e)
    pctx, held_map = _portfolio_context()
    northbound = _northbound_net_flow()
    blocks = [f"# AI 四维复盘 {datetime.datetime.now().strftime('%Y-%m-%d')}\n"]
    validation = {}
    _reset_audit()
    for i, c in enumerate(cands):
        if _rt is not None:
            try:
                prof = _rt.get_realtime_profile(c["code"], with_indicators=True)
                if isinstance(sector, list):
                    prof["sector"] = sector
                c["_rt"] = prof
            except Exception as e:
                logger.warning("[ai_analysis] realtime %s 失败: %s", c.get("code"), e)
        held_code = str(c.get("code"))
        held_info = None
        if held_code in held_map:
            held_info = (f"该标的已在我的持仓中，当前实仓权重约 {held_map[held_code]:.1f}%，"
                         f"属组合一部分；分析加/减仓时请结合整体仓位与行业集中度。")
        extra_info = _extra_info_block(held_code, c.get("market"), northbound)
        sc = int(os.environ.get("AI_REVIEW_SELF_SAMPLE", "2")) if i == 0 else 0
        text = analyze(c["name"], c, portfolio_ctx=pctx, held_info=held_info,
                       extra_info=extra_info, cross_check=(i < int(os.environ.get("AI_REVIEW_CROSS_TOP", "2"))),
                       self_consistency=sc)
        rt = c.get("_rt")
        if text and ("未配置" not in text) and ("调用失败" not in text):
            text, issues = _validate_output(held_code, c.get("name"), text, c, rt, extra_info)
            validation[c["code"]] = issues
        blocks.append(
            f"## {c['code']} {c['name']}\n\n{c.get('reason', '')}\n\n"
            + (text or "_（未配置 DEEPSEEK_API_KEY 或调用失败）_")
        )
    md = "\n\n".join(blocks)
    if validation:
        md += "\n\n---\n\n# 数据校验（程序自动，需人工复核）\n"
        for code, issues in validation.items():
            if issues:
                md += f"\n**{code}** 存疑项 {len(issues)} 条：\n" + "\n".join(f"- {i}" for i in issues)
            else:
                md += f"\n**{code}** 所有数值均可从输入追源，未发现编造硬数字。✅\n"
    _write_metrics(validation)
    os.makedirs(os.path.join(_NOTES_DIR, "ai复盘"), exist_ok=True)
    out = os.path.join(_NOTES_DIR, "ai复盘", "ai_review_%s.md" % datetime.datetime.now().strftime("%Y%m%d"))
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
