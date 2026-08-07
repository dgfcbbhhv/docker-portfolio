"""组合分层配置校验。

校验结果分两类：
- errors: 配置无法安全计算，必须修复
- warnings: 配置可运行，但会自动归一化或可能影响评估可信度
"""
import os

from modules.config import get_config, LAYERS

# 各告警渠道依赖的环境变量（密钥经 env 注入，不落盘）
ENV_CHANNELS = {
    "email": ("SMTP_SERVER", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "ALERT_TO"),
    "wechat": ("WECHAT_WEBHOOK",),
    "telegram": ("TG_TOKEN", "TG_CHAT_ID"),
    "webhook": ("WEBHOOK_URL",),
    "bark": ("BARK_URL", "BARK_KEY"),
}

# 允许缺失的密钥（部分渠道可匿名/公开接口）
ENV_OPTIONAL_KEYS = {"BARK_KEY"}


def validate_env(env=None):
    """校验环境变量注入的告警渠道配置完整性，返回 warnings。

    仅当某渠道至少注入了一个变量但又不完整时告警，避免静默配置导致的运行期失败；
    完全不配置某渠道（所有变量缺失）则视为未启用，不告警。
    """
    env = env if env is not None else os.environ
    warnings = []
    for channel, keys in ENV_CHANNELS.items():
        present = [k for k in keys if env.get(k)]
        if not present:
            continue
        missing = [
            k for k in keys
            if not env.get(k) and k not in ENV_OPTIONAL_KEYS
        ]
        if missing:
            warnings.append(
                f"渠道 {channel} 已启用但环境变量缺失: {', '.join(missing)}"
            )
    return warnings

LAYER_KEYS = ("A", "HK", "US", "REIT")


def _as_float(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 不是有效数字: {value!r}")


def validate_config(cfg=None):
    cfg = cfg if cfg is not None else get_config()
    errors = []
    warnings = []
    portfolio = cfg.get("portfolio", {}) or {}
    layers = cfg.get("layers") or LAYERS

    for key in LAYER_KEYS:
        stocks = portfolio.get(key, {}) or {}
        if not stocks:
            errors.append(f"{key} 层没有持仓配置")
            continue
        ratios = []
        for code, meta in stocks.items():
            try:
                ratio = _as_float((meta or {}).get("ratio"), f"{key}.{code}.ratio")
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if ratio <= 0:
                errors.append(f"{key}.{code}.ratio 必须大于 0")
            ratios.append(ratio)
        if ratios:
            total = sum(ratios)
            if abs(total - 100) > 0.5:
                warnings.append(f"{key} 层 ratio 合计 {total:.1f}%，将自动归一化为 100%")

    for key, layer in layers.items():
        if key not in LAYER_KEYS:
            warnings.append(f"layers.{key} 未对应 portfolio 层，将忽略")
            continue
        try:
            invested = _as_float(layer.get("invested_cap"), f"layers.{key}.invested_cap")
        except ValueError as exc:
            errors.append(str(exc))
            invested = 80.0
        if not (0 < invested <= 100):
            errors.append(f"layers.{key}.invested_cap 必须在 (0, 100] 之间")
        try:
            max_drawdown = _as_float(layer.get("max_drawdown"), f"layers.{key}.max_drawdown")
        except ValueError as exc:
            errors.append(str(exc))
            max_drawdown = 10.0
        if not (1 <= max_drawdown <= 50):
            errors.append(f"layers.{key}.max_drawdown 必须在 [1, 50] 之间")

    sequences = [
        layer.get("sequence")
        for layer in layers.values()
        if layer.get("sequence") is not None
    ]
    if len(sequences) != len(set(sequences)):
        errors.append("layers.sequence 必须唯一")

    manual_prices = cfg.get("manual_prices", {}) or {}
    manual_growth = cfg.get("manual_us_growth", {}) or {}
    for code in (portfolio.get("US", {}) or {}):
        if code not in manual_prices:
            warnings.append(f"美股 {code} 缺少 manual_prices，层内回撤评估可能不可靠")
        if code not in manual_growth:
            warnings.append(f"美股 {code} 缺少 manual_us_growth，成长评分可能不可靠")

    return errors, warnings
