FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements*.txt ./
RUN if [ -f requirements.lock.txt ]; then \
      pip install --no-cache-dir -r requirements.lock.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi
COPY . .
# 非 root 运行：创建 app 用户，仅对运行期可写目录授权（.env/密钥集中由 env 注入）
RUN groupadd -r app && useradd -r -g app -m -d /app app \
    && mkdir -p data notes data/logs \
    && chown -R app:app data notes
RUN chmod +x entrypoint.sh
EXPOSE 8501
# 以 root 引导入口（需写 /etc/hosts 固定东财 DNS），随后降权运行应用
CMD ["./entrypoint.sh"]
