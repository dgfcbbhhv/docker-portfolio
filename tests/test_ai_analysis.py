"""AI 复盘反幻觉加固的单元测试（正常 / 缺失数据 两套场景）。

覆盖：数值追源、公告日期/标题核验、资金流容差复核、低置信度待审、
Self-Consistency 多数/分歧、verdict 判定、幻觉率指标落盘、用户反馈回流。
"""
import json
import os

import pytest

import modules.ai_analysis as A


class FakeRT:
    """可控的实时行情/公告 stub。"""

    def __init__(self, main_net=-85453000, small_net=85491000, announcements=None):
        self.main_net = main_net
        self.small_net = small_net
        self.announcements = announcements or [
            {"date": "2026-08-29", "title": "2026年半年度报告"},
            {"date": "2026-07-29", "title": "行长任职资格核准公告"},
        ]

    def get_realtime_profile(self, code, with_indicators=False, with_announcements=False):
        ind = {"MACD_DIF": 0.2, "MACD_DEA": 0.1, "MACD_HIST": 0.3, "KDJ_K": 80,
               "KDJ_D": 75, "KDJ_J": 92, "RSI": 55, "BIAS1": 0.5}
        return {
            "code": code, "error": None,
            "price": "39.35", "pct": "0.55", "turnover": "0.4", "vol_ratio": "0.8",
            "main_net": self.main_net, "capital": {"small_net": self.small_net},
            "indicators": ind,
            "announcements": self.announcements,
        }


def _row():
    return {"code": "600036", "price": "39.35", "pe": "6.5", "pb": "0.94",
            "dy": "5.12", "lishing": "82", "kelly_final": "9.5", "roe": "13.4",
            "vol": "19", "mcap": "9800"}


def _rt_dict(fake):
    return fake.get_realtime_profile("600036", with_indicators=True)


# ---------- 1. 数值追源：正常（全部可追源） vs 编造 ----------

def test_validate_normal_no_issues(monkeypatch):
    fake = FakeRT()
    monkeypatch.setattr(A, "_rt", fake)
    text = "**硬数字** 股价39.35，股息率5.12%。\n**定性判断** 现金流质量稳定。"
    clean, issues = A._validate_output("600036", "招行", text, _row(), _rt_dict(fake), "机构一致预期EPS同比约6%。")
    # 商定：文本内 39.35/5.12 均在输入地基；6% 来自 extra_info → 全可追源
    assert issues == []
    assert "存疑" not in clean


def test_validate_fabricated_number_flagged(monkeypatch):
    fake = FakeRT()
    monkeypatch.setattr(A, "_rt", fake)
    text = "**硬数字** 股价39.35。\n**定性判断** 资本充足率13.7%，不良率1.2%。"
    clean, issues = A._validate_output("600036", "招行", text, _row(), _rt_dict(fake), None)
    numbers = [i for i in issues if "数值" in i]
    assert any("13.7%" in n or "1.2%" in n for n in numbers)
    assert any("⚠️" in f and "无法从输入追源" in f for f in clean.split("\n"))


# ---------- 2. 公告日期核验 ----------

def test_validate_valid_date_passes(monkeypatch):
    fake = FakeRT()
    monkeypatch.setattr(A, "_rt", fake)
    text = "**定性判断** 公司于2026-08-29披露半年报。"
    clean, issues = A._validate_output("600036", "招行", text, _row(), _rt_dict(fake), None)
    assert all("日期" not in i for i in issues)


def test_validate_fake_date_flagged(monkeypatch):
    fake = FakeRT()
    monkeypatch.setattr(A, "_rt", fake)
    text = "**定性判断** 公司于2026-09-15披露新报告。"
    clean, issues = A._validate_output("600036", "招行", text, _row(), _rt_dict(fake), None)
    assert any("2026-09-15" in i for i in issues)


# ---------- 3. 资金流容差复核 ----------

def test_recheck_flow_tolerance_hits(monkeypatch):
    # 8545万 vs 真实 8545.3万 → 容差命中 → 从存疑清单移除
    # 注：_recheck_suspect_tokens 走全局 _rt 重拉，故 monkeypatch A._rt 为 FakeRT
    monkeypatch.setattr(A, "_rt", FakeRT())
    kept = A._recheck_suspect_tokens(["8545万", "130亿", "净流出9000万"], "600036", _rt_dict(FakeRT()))
    assert "8545万" not in kept
    assert "130亿" in kept
    assert any("9000万" in k for k in kept)


def test_recheck_flow_exact_mismatch_kept(monkeypatch):
    monkeypatch.setattr(A, "_rt", FakeRT())
    kept = A._recheck_suspect_tokens(["9000万"], "600036", _rt_dict(FakeRT()))
    assert "9000万" in kept


def test_close_enough():
    assert A._close_enough(8545, 8545.3)
    assert not A._close_enough(8545, 9000)


# ---------- 4. 低置信度待审 ----------

def test_low_confidence_pending_marker(monkeypatch):
    monkeypatch.setenv("AI_REVIEW_LOW_CONF", "2")
    fake = FakeRT()
    monkeypatch.setattr(A, "_rt", fake)
    text = "**定性判断** 资本充足率13.7%，不良率1.2%，拨备300%，净息差2.0%。"
    clean, issues = A._validate_output("600036", "招行", text, _row(), _rt_dict(fake), None)
    assert len(issues) >= 2
    assert "待审" in clean


# ---------- 5. Self-Consistency / verdict ----------

def test_majority_text_prefers_majority():
    texts = ["结论：适合加仓。", "结论：不适合加仓。", "结论：适合加仓。"]
    verdicts = ["适合加仓", "不适合加仓", "适合加仓"]
    assert A._majority_text(texts, verdicts) == texts[0]


def test_majority_disagreed_true_in_tie():
    verdicts = ["适合加仓", "不适合加仓"]
    assert A._majority_disagreed(verdicts) is True


def test_verdict_agree():
    assert A._verdict_agree("适合加仓", "建议加仓") is True
    assert A._verdict_agree("适合加仓", "不适合建仓") is False


# ---------- 6. 幻觉率指标落盘 ----------

def test_write_metrics_files(tmp_path, monkeypatch):
    A._reset_audit()
    A._AUDIT.append({"code": "600036", "name": "招行", "model": "m", "flags": [],
                     "suspect_count": 0})
    A._AUDIT.append({"code": "600519", "name": "茅台", "model": "m", "flags": [],
                     "suspect_count": 2, "self_flagged": True})
    monkeypatch.setattr(A, "_NOTES_DIR", str(tmp_path))
    validation = {"600036": [], "600519": ["数值 '13.7%' 未追源"]}
    A._write_metrics(validation)
    files = sorted(os.listdir(tmp_path))
    assert "hallucination_metrics.json" in files
    daily = [f for f in files if f.startswith("hallucination_metrics_")]
    assert len(daily) == 1
    hist = json.load(open(os.path.join(str(tmp_path), "hallucination_metrics.json"), encoding="utf-8"))
    day = list(hist.values())[0]
    assert day["total"] == 2
    assert day["flagged"] == 1


def test_write_metrics_no_alert_within_threshold(tmp_path, monkeypatch):
    A._reset_audit()
    A._AUDIT.append({"code": "600036", "model": "m", "flags": [], "suspect_count": 0})
    monkeypatch.setattr(A, "_NOTES_DIR", str(tmp_path))
    # 阈值 1.0，存疑率 0 → 不应触发
    monkeypatch.setenv("AI_REVIEW_ALERT_RATE", "1.0")
    A._write_metrics({"600036": []}, worker=None)


# ---------- 7. 用户反馈回流 ----------

def test_record_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_NOTES_DIR", str(tmp_path))
    rec = A.record_feedback("600036", verdict="适合加仓", flagged=True, note="编造了不良率")
    assert rec["code"] == "600036"
    assert rec["flagged"] is True
    arr = json.load(open(os.path.join(str(tmp_path), "feedback.json"), encoding="utf-8"))
    assert len(arr) == 1
    # 二次写入追加不覆盖
    A.record_feedback("600036", verdict="适合加仓", flagged=False)
    arr = json.load(open(os.path.join(str(tmp_path), "feedback.json"), encoding="utf-8"))
    assert len(arr) == 2


def test_record_feedback_no_code(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_NOTES_DIR", str(tmp_path))
    assert A.record_feedback(None) is None
    assert not os.path.exists(os.path.join(str(tmp_path), "feedback.json"))


# ---------- 8. 二次复核 / 决策辅助 ----------

def test_recheck_date_hit_and_miss(monkeypatch):
    fake = FakeRT(announcements=[{"date": "2026-08-29", "title": "2026年半年度报告"}])
    monkeypatch.setattr(A, "_rt", fake)
    assert A._recheck_date("2026-08-29", "600036", None) is True
    assert A._recheck_date("2026-09-15", "600036", None) is False


def test_recheck_date_degrades_on_no_rt():
    orig = A._rt
    A._rt = None
    try:
        assert A._recheck_date("2026-08-29", "600036", None) is False
    finally:
        A._rt = orig


def test_schedule_temp_values(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_TEMP_MAIN", raising=False)
    assert A._schedule_temp(1.0) == 0.2
    assert A._schedule_temp(0.0) == 0.0
    monkeypatch.setenv("AI_REVIEW_TEMP_MAIN", "0.7")
    assert A._schedule_temp(1.0) == 0.7


def test_first_verdict_and_tone():
    assert A._first_verdict("分析...\n\n结论：适合按Kelly加仓。") == "结论：适合按Kelly加仓。"
    assert A._tone_of("不适合建仓") == -1
    assert A._tone_of("建议加仓") == 1
    assert A._tone_of("中性观察") == 0


def test_ground_truth_includes_row_and_extra(monkeypatch):
    fake = FakeRT()
    r = _row()
    g = A._ground_truth_numbers("600036", r, _rt_dict(fake), "机构一致预期EPS同比约6%。")
    assert "39.35" in g  # 股价
    assert "5.12" in g    # 股息率
    assert "6" in g      # extra_info 的 6


# ---------- 9. analyze 自洽/交叉（mock LLM，不联网） ----------

def _fake_call_results(samples):
    """把 _call_llm 替换为顺序返回 samples 中的内容。"""
    it = iter(samples)

    def fake(model, prompt, temperature=0.1):
        return next(it)

    return fake


def test_analyze_self_consistency_majority(monkeypatch):
    monkeypatch.setattr(A, "_api_key", lambda: "k")
    monkeypatch.setattr(A, "_ai_review_model", lambda: "m")
    calls = iter(["结论：适合加仓。", "结论：适合加仓。", "结论：不适合加仓。"])

    def fake(model, prompt, temperature=0.1):
        return next(calls)

    monkeypatch.setattr(A, "_call_llm", fake)
    row = _row()
    row["_rt"] = _rt_dict(FakeRT())
    text = A.analyze("招行", row, self_consistency=3)
    assert text.startswith("结论：适合加仓")
    assert A._AUDIT[-1]["self_flagged"] is True  # 有分歧


def test_analyze_cross_check_disagree_flags(monkeypatch):
    monkeypatch.setattr(A, "_api_key", lambda: "k")
    monkeypatch.setattr(A, "_ai_review_model", lambda: "primary")
    monkeypatch.setattr(A, "_ai_review_model_b", lambda: "secondary")
    seq = iter(["结论：适合加仓。", "结论：不适合加仓。"])

    def fake(model, prompt, temperature=0.1):
        return next(seq)

    monkeypatch.setattr(A, "_call_llm", fake)
    row = _row()
    row["_rt"] = _rt_dict(FakeRT())
    text = A.analyze("招行", row, cross_check=True)
    assert "双模型交叉核验" in text
    assert any("不一致" in f for f in A._AUDIT[-1]["flags"])
