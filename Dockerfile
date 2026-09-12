# ============================================================================
# Tidal_Echo · 单服务镜像（方案 A）
# ============================================================================
# 一个容器 = 「房子」+「门」：
#   · relay   （backend/app.py，原样）：消息持久化 / SSE / 鉴权 / Web Push
#             外加把 web/ 这个 PWA 静态目录一起发出去（原本是 nginx 干的）
#   · api_loop（examples/api_loop.py，原样）：官方自带的“临时身体”，
#             第一阶段用它验收链路；第二阶段把 KaelLife 换上这个位置
#
# ⚠️ 本文件与 deploy/ 下的东西都属于【我们的部署适配层】。
#    Tidal_Echo 的原代码（backend/ web/ examples/）没有被修改。
#    原作者假设的是 VPS + nginx + systemd，这里把它换成容器单服务。
# ============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RELAY_BACKEND_DIR=/app/backend \
    RELAY_WEB_DIR=/app/web

WORKDIR /app

# 依赖：relay 与 api_loop 共用同一份（fastapi/uvicorn/httpx/pywebpush）
# 先只拷 requirements，让这一层能被 Docker 缓存住
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 源码按上游原始目录结构摆放，方便和上游对比、以后升级
COPY backend/    /app/backend/
COPY web/        /app/web/
COPY examples/api_loop.py /app/examples/api_loop.py

# 我们的适配层
COPY deploy/     /app/deploy/
RUN sed -i 's/\r$//' /app/deploy/entrypoint.sh \
 && chmod +x /app/deploy/entrypoint.sh

# 持久化数据目录。Zeabur 上必须把持久卷挂到这里，否则重新部署 = 数据全丢。
RUN mkdir -p /data/uploads
VOLUME ["/data"]

# Zeabur 会注入 PORT，入口脚本会读它；这里的 EXPOSE 只是文档作用
EXPOSE 8080

# /healthz 不需要鉴权，适合做探活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=4).read()" || exit 1

ENTRYPOINT ["/bin/bash", "/app/deploy/entrypoint.sh"]
