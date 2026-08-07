"""多通道预警系统

通道: 邮件 / 企业微信 / Telegram / Bark / 通用 Webhook
触发: 股价跌幅 / 股息率变动 / Kelly变化 / 李氏评分下降 / 论点破坏者

Bark 加密推送: 配置 bark_key(16字节)与 bark_iv(16字符)后启用 AES-128-CBC,
推送内容加密为 ciphertext, 服务器与 Apple APNs 均只见密文。
"""
import json, os, smtplib, datetime, logging, time
from email.mime.text import MIMEText
import pandas as pd

_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(_DIR, "alert_config.json")
LOG_PATH = os.path.join(_DIR, "alerts_log.csv")
FAILURE_LOG_PATH = os.path.join(_DIR, "alert_failures.log")

logger = logging.getLogger(__name__)

# Cloudflare 等反代/边缘节点会拦截无 UA 或 python-urllib 的请求
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

# 环境变量优先于本地配置，避免密钥落盘
_ENV_MAP = {
    "smtp_server": "SMTP_SERVER",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_pass": "SMTP_PASS",
    "alert_to": "ALERT_TO",
    "wechat_webhook": "WECHAT_WEBHOOK",
    "tg_token": "TG_TOKEN",
    "tg_chat_id": "TG_CHAT_ID",
    "webhook_url": "WEBHOOK_URL",
    "bark_url": "BARK_URL",
    "bark_key": "BARK_KEY",
    "bark_iv": "BARK_IV",
}


class AlertSender:
    def __init__(self, cfg=None):
        self.cfg = cfg or self._load()

    def _load(self):
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        # 环境变量优先，密钥无需写入 alert_config.json
        for key, env in _ENV_MAP.items():
            if os.environ.get(env):
                cfg[key] = os.environ[env]
        return cfg

    def _save_cfg(self):
        os.makedirs(_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def update_cfg(self, **kwargs):
        self.cfg.update(kwargs)
        self._save_cfg()

    def _send_once(self, channel, title, content):
        fn = {
            "email": self._email,
            "wechat": self._wechat,
            "telegram": self._telegram,
            "webhook": self._webhook,
            "bark": self._bark,
        }
        return fn.get(channel, lambda a, b: False)(title, content)

    def send(self, title, content, channel="email", retries=3, delay=2.0, meta=None):
        """带指数退避重试的发送，全部失败时写入失败日志与推送审计。"""
        start = time.monotonic()
        last_err = None
        attempts = max(1, int(retries))
        for i in range(attempts):
            try:
                if self._send_once(channel, title, content):
                    latency_ms = round((time.monotonic() - start) * 1000)
                    self._log_audit(title, content, channel, "sent", i, None, latency_ms, meta)
                    return True
                last_err = "channel returned False"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if i < attempts - 1:
                time.sleep(delay * (2 ** i))
        latency_ms = round((time.monotonic() - start) * 1000)
        self._log_audit(title, content, channel, "failed", attempts - 1, last_err, latency_ms, meta)
        self._log_failure(title, content, channel, last_err)
        return False

    def send_multi(self, title, content, channels=None, retries=3):
        """按顺序尝试多个通道，任一成功即返回 (True, channel)。"""
        for ch in (channels or ["email", "wechat", "telegram", "webhook"]):
            if self.send(title, content, channel=ch, retries=retries):
                return True, ch
        return False, None

    def _log_audit(self, title, content, channel, status, retries, err, latency_ms, meta=None):
        """写入推送审计日志；审计失败不影响发送结果。"""
        try:
            from modules.push_audit import log_push
            log_push({
                "title": title,
                "content": content,
                "channel": channel,
                "status": status,
                "retries": retries,
                "error": err,
                "latency_ms": latency_ms,
                "meta": meta or {},
            })
        except Exception:
            logger.warning("push audit log failed", exc_info=True)
    def _log_failure(self, title, content, channel, err):
        try:
            with open(FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().isoformat()} | {channel} | {title} | {err}\n")
        except Exception:
            pass

    def _email(self, t, c):
        try:
            cfg = self.cfg
            msg = MIMEText(c, "plain", "utf-8")
            msg["Subject"] = t
            msg["From"] = cfg.get("smtp_user", "")
            msg["To"] = cfg.get("alert_to", "")
            with smtplib.SMTP_SSL(cfg.get("smtp_server", ""), int(cfg.get("smtp_port", 465))) as s:
                s.login(cfg.get("smtp_user", ""), cfg.get("smtp_pass", ""))
                s.send_message(msg)
            return True
        except Exception:
            return False

    def _wechat(self, t, c):
        try:
            import urllib.request
            url = self.cfg.get("wechat_webhook", "")
            if not url:
                return False
            data = json.dumps({"msgtype": "text", "text": {"content": f"{t}\n{c}"}})
            req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False

    def _telegram(self, t, c):
        try:
            import urllib.request
            tok = self.cfg.get("tg_token", "")
            cid = self.cfg.get("tg_chat_id", "")
            if not tok or not cid:
                return False
            url = f"https://api.telegram.org/bot{tok}/sendMessage"
            data = json.dumps({"chat_id": cid, "text": f"*{t}*\n{c}", "parse_mode": "Markdown"})
            req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception:
            return False

    def _webhook(self, t, c):
        try:
            import urllib.request
            from urllib.parse import urlparse, parse_qs
            url = self.cfg.get("webhook_url", "")
            if not url:
                return False
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            payload = {k: v[0] for k, v in qs.items()}
            payload.update({"title": t, "content": c, "body": c,
                            "time": datetime.datetime.now().isoformat()})
            base = parsed._replace(query="").geturl()
            data = json.dumps(payload)
            req = urllib.request.Request(base, data=data.encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status >= 400:
                    return False
                try:
                    j = json.loads(body)
                except Exception:
                    return True
                if isinstance(j, dict) and "code" in j:
                    return int(j["code"]) in (0, 200, 1000)
                return True
        except Exception:
            return False

    def _bark(self, t, c):
        """Bark (iOS 推送) 通道。

        支持两种服务器形态：
          - 公共服务器: https://api.day.app/DEVICE_KEY/
          - 自建服务器: http://192.168.31.24:8080/DEVICE_KEY/
        支持两种发送模式：
          - 明文: 标题/内容拼入 URL 路径 (/DEVICE_KEY/标题/内容)
          - 加密: 配置 bark_key 后启用 AES-128-CBC，将 {"title","body"} json
            加密为 ciphertext，带 iv 发送到 /DEVICE_KEY，App 用同一密钥解密
        """
        try:
            import urllib.request
            from urllib.parse import urlparse, quote
            base_url = self.cfg.get("bark_url", "") or self.cfg.get("webhook_url", "")
            if not base_url:
                return False
            parsed = urlparse(base_url)
            path = parsed.path.rstrip("/")
            if not path:
                return False
            key = self.cfg.get("bark_key", "")
            if key:
                return self._bark_encrypted(t, c, parsed, path, key)
            # 明文模式: /DEVICE_KEY/标题/内容，内容 URL 编码
            full_path = f"{path}/{quote(t)}/{quote(c)}"
            url = f"{parsed.scheme}://{parsed.netloc}{full_path}"
            if parsed.query:
                url += "?" + parsed.query
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status >= 400:
                    return False
                try:
                    j = json.loads(body)
                    if isinstance(j, dict) and "code" in j:
                        return int(j["code"]) == 200
                except Exception:
                    pass
                return True
        except Exception:
            return False

    def _bark_encrypted(self, t, c, parsed, path, key):
        """AES-128-CBC 加密推送。

        key 为 16 字节(手机 App 推送加密设置里填同样的 KEY)。
        iv 为 16 字符 ASCII; 配置了 bark_iv 则使用固定值(与 App 端设置一致),
        否则随机生成。发送时传原始 iv 字符串, App 端解密。
        """
        import base64
        import random
        import string
        import subprocess
        from urllib.parse import urlencode
        import urllib.request

        if len(key.encode("utf-8")) != 16:
            return False
        iv_str = self.cfg.get("bark_iv", "") or "".join(
            random.choices(string.ascii_letters + string.digits, k=16))
        payload = json.dumps({"title": t, "body": c}, ensure_ascii=False)
        try:
            p = subprocess.run(
                ["openssl", "enc", "-aes-128-cbc", "-K", key.encode().hex(),
                 "-iv", iv_str.encode().hex(), "-nosalt"],
                input=payload.encode("utf-8"), capture_output=True, check=True)
        except Exception:
            return False
        ciphertext = base64.b64encode(p.stdout).decode("ascii")
        url = f"{parsed.scheme}://{parsed.netloc}{path}"
        data = urlencode({"ciphertext": ciphertext, "iv": iv_str}).encode("ascii")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status >= 400:
                    return False
                try:
                    j = json.loads(body)
                    if isinstance(j, dict) and "code" in j:
                        return int(j["code"]) == 200
                except Exception:
                    pass
                return True
        except Exception:
            return False


def check_alerts(portfolio, cur_data, prev_data, lishing_scores, thresholds):
    """检查所有预警条件, 返回 list of dict"""
    alerts = []
    for code, cfg in portfolio.items():
        name = cfg.get("name", code)
        cur = cur_data.get(code, {})
        prev = prev_data.get(code, {})

        # 1. 股价单日跌幅（跳过异常数据：价格为0或跌幅>=100%通常是数据源问题）
        chg = cur.get("chg", 0)
        price = cur.get("price", 0)
        if price > 0 and chg < -thresholds.get("price_drop", 8) and chg > -100:
            alerts.append({"code": code, "name": name, "type": "股价跌幅",
                           "msg": f"{name} 单日跌幅 {chg:.2f}%"})

        # 2. 股息率变动
        cur_dy = cur.get("dy", 0)
        prev_dy = prev.get("dy", cur_dy)
        if prev_dy > 0:
            dy_chg = abs(cur_dy - prev_dy)
            if dy_chg > thresholds.get("div_change", 0.5):
                alerts.append({"code": code, "name": name, "type": "股息率变动",
                               "msg": f"{name} 股息率变动 {dy_chg:.1f}%"})

        # 3. Kelly 仓位变化
        ck = cur.get("kelly_final", 0)
        pk = prev.get("kelly_final", ck)
        if abs(ck - pk) > thresholds.get("kelly_change", 5):
            alerts.append({"code": code, "name": name, "type": "Kelly变化",
                           "msg": f"{name} Kelly变化 {abs(ck-pk):.1f}%"})

        # 4. 李氏评分下降
        cs = lishing_scores.get(code, {}).get("total", 100)
        ps = prev.get("lishing_score", cs)
        if ps - cs > thresholds.get("lishing_drop", 10):
            alerts.append({"code": code, "name": name, "type": "评分下降",
                           "msg": f"{name} 李氏评分下降 {ps-cs:.0f}分"})

        # 5. 盈利恶化（最新一期净利润同比大幅下滑）
        cur_ey = cur.get("earn_np_yoy")
        if cur_ey is not None:
            try:
                cur_ey = float(cur_ey)
            except (TypeError, ValueError):
                cur_ey = None
        if cur_ey is not None and cur_ey < -thresholds.get("earn_drop", 15):
            alerts.append({"code": code, "name": name, "type": "盈利恶化",
                           "msg": f"{name} 最新净利同比 {cur_ey:.1f}%"})

    return alerts


def log_alerts(alerts):
    """记录预警历史到 CSV"""
    if not alerts:
        return
    df = pd.DataFrame(alerts)
    df["timestamp"] = datetime.datetime.now().isoformat()
    if os.path.exists(LOG_PATH):
        try:
            existing = pd.read_csv(LOG_PATH)
            df = pd.concat([existing, df], ignore_index=True)
        except Exception:
            pass
    df.to_csv(LOG_PATH, index=False, encoding="utf-8-sig")


def load_alert_history():
    """读取预警历史"""
    if not os.path.exists(LOG_PATH):
        return pd.DataFrame()
    try:
        return pd.read_csv(LOG_PATH)
    except Exception:
        return pd.DataFrame()
