# -*- coding: utf-8 -*-
"""网络修复：NAS/容器环境 IPv6 路由不通，导致 Python 请求在 IPv6 上等超时才回退 IPv4。

典型代价：每次请求多耗 ~20s（IPv6 超时 + IPv4 再次尝试）。
本模块把所有 getaddrinfo 结果强制为 IPv4 优先（AF_INET），
调用 enable_ipv4_preference() 即全局生效，覆盖 urllib/requests/urllib3/socket。
"""
import socket
import _socket

# 东方财富 push2 系域名的可用 IPv4 网关（宽带对 push2 HTTPS 做 TLS reset，
# 但该 IP 的 HTTP 通道对所有 push2 变体（如 33.push2his）均 200）
_EM_PUSH2_GATEWAY = "101.226.30.136"
# 常见的数字变体：42.push2his、33.push2his 等均由 akshare 动态拼接
_EM_PUSH2_PATTERNS = (
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "push2delay.eastmoney.com",
)


def _em_gateway_ip(host):
    """push2 系子域统一解析到可用网关 IP，其他保持默认。"""
    if any(p in host for p in _EM_PUSH2_PATTERNS):
        return _EM_PUSH2_GATEWAY
    return None


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    # family 未显式指定时强制 IPv4，避免 IPv6 超时拖慢所有请求
    if not family or family == socket.AF_UNSPEC:
        family = socket.AF_INET
    gw = _em_gateway_ip(str(host))
    if gw is not None:
        return [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (gw, port)),
            (_socket.AF_INET, _socket.SOCK_DGRAM, 17, "", (gw, port)),
        ]
    return _socket.getaddrinfo(host, port, family, type, proto, flags)


_installed = False


def enable_ipv4_preference():
    """全局启用 IPv4 优先（幂等，可重复调用）。"""
    global _installed
    if _installed:
        return
    socket.getaddrinfo = _ipv4_first
    _installed = True