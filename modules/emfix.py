# -*- coding: utf-8 -*-
"""东方财富 (Eastmoney) 网络修复。

现象：NAS 所在宽带对 `*.eastmoney.com` 的 HTTPS API 路径做 IP 层 reset，
     但同 IP 的 HTTP 与 push2delay 主机的 HTTP 均可用。
方案：
  1. entrypoint 已把 push2/push2his/82.push2 等 hosts 指向 delay IPv4 (101.226.30.136)。
  2. 本模块 monkeypatch requests 的 Session.request，把所有东方财富 HTTPS 请求
     降级为 HTTP，避免 TLS 被 reset。覆盖 akshare 所有走 requests 的接口。
调用 enable_em_http() 即全局生效。
"""
import logging

logger = logging.getLogger(__name__)

_EM_DOMAINS = (
    "eastmoney.com",
    "dfcfw.com",
)


def _em_http_url(url: str) -> str:
    low = url.lower()
    if low.startswith("https://") and any(d in low for d in _EM_DOMAINS):
        return "http://" + url[len("https://"):]
    return url


def _wrap_session_request(orig):
    def wrapped(self, method, url, *args, **kwargs):
        u2 = _em_http_url(url)
        if u2 != url:
            logger.debug("em https->http: %s", u2[:90])
            kwargs.setdefault("allow_redirects", False)
        return orig(self, method, u2, *args, **kwargs)
    return wrapped


_installed = False


def enable_em_http():
    """全局把东方财富 HTTPS 请求降级为 HTTP（幂等）。"""
    global _installed
    if _installed:
        return
    try:
        import requests
        if not getattr(requests.sessions.Session, "_em_http_patched", False):
            requests.sessions.Session.request = _wrap_session_request(
                requests.sessions.Session.request)
            requests.sessions.Session._em_http_patched = True
        _installed = True
        logger.info("em http patch installed (Session.request)")
    except Exception as e:
        logger.warning("em http patch failed: %s", e)