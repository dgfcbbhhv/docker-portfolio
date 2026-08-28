FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements*.txt ./
RUN if [ -f requirements.lock.txt ]; then \
      pip install --no-cache-dir --no-deps -r requirements.lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple; \
    else \
      pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://pypi.org/simple; \
    fi
COPY . .
# 离线装入已从运行容器抽取并 vendored 的 longbridge 4.4.3（PyPI 已下架，无法 pip 安装）
RUN cp -r /app/vendor/longbridge/longbridge /usr/local/lib/python3.12/site-packages/ \
 && cp -r /app/vendor/longbridge/longbridge-4.4.3.dist-info /usr/local/lib/python3.12/site-packages/
# 非 root 运行：创建 app 用户，仅对运行期可写目录授权（.env/密钥集中由 env 注入）
RUN groupadd -r app && useradd -r -g app -m -d /app app \
    && mkdir -p data notes data/logs \
    && chown -R app:app data notes
RUN chmod +x entrypoint.sh
EXPOSE 8501
# 以 root 引导入口（需写 /etc/hosts 固定东财 DNS），随后降权运行应用
CMD ["./entrypoint.sh"]
