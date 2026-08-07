#!/bin/sh
# 容器入口：同时启动 alert_worker 后台进程 + Streamlit 前端
set -e

# 以非 root 运行应用进程（业务进程被攻破时无容器 root 权限）
AS_APP() { setpriv --reuid=app --regid=app --init-groups "$@"; }

# 加载 .env 环境变量（若存在）
if [ -f /app/.env ]; then
    set -a
    . /app/.env
    set +a
fi

# 东方财富 DNS 固化：push2 系域名指向 push2delay 可用 IPv4（HTTP 通道）
# 避免宽带对 push2 HTTPS API 的 TLS reset 导致行情拉取失败
if ! grep -q "101.226.30.136" /etc/hosts 2>/dev/null; then
    echo "101.226.30.136 push2.eastmoney.com push2his.eastmoney.com push2delay.eastmoney.com 82.push2.eastmoney.com 83.push2.eastmoney.com 42.push2.eastmoney.com" >> /etc/hosts
    echo "[entrypoint] em DNS override installed"
fi

# bind-mount 的 data/notes 属宿主机目录，降权前先交给 app 用户，保证写缓存/日志
chown -R app:app /app/data /app/notes 2>/dev/null || true
# modules/data 内的运行时状态文件(data_status.json)亦需 app 可写
chown -R app:app /app/modules/data 2>/dev/null || true
# 非 root 下 HOME 需可写：Streamlit 写安装指标到 $HOME/.streamlit，默认 /root 不可写
export HOME=/home/app
mkdir -p "$HOME" && chown -R app:app "$HOME" 2>/dev/null || true

# MODE=alert-worker：仅启动 alert_worker（前台），供独立容器使用
if [ "$MODE" = "alert-worker" ]; then
    echo "[entrypoint] alert-worker mode"
    exec setpriv --reuid=app --regid=app --init-groups python -m modules.alert_worker
fi

# 启动预警后台进程（日志重定向到文件，不阻塞主进程）
echo "[entrypoint] starting alert_worker..."
AS_APP python -m modules.alert_worker >> /app/data/logs/alert_worker.stdout.log 2>&1 &
ALERT_PID=$!
echo "[entrypoint] alert_worker started (pid=$ALERT_PID)"

# 容器退出时清理后台进程
trap "echo '[entrypoint] stopping alert_worker...'; kill $ALERT_PID 2>/dev/null; exit" TERM INT

# 启动 Streamlit 前端（前台进程，容器主进程）
echo "[entrypoint] starting Streamlit..."
exec setpriv --reuid=app --regid=app --init-groups streamlit run app.py --server.port=8501 --server.address=0.0.0.0
