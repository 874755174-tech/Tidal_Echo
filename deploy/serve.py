#!/usr/bin/env python3
"""
部署适配层（方案 A：一个 Zeabur 服务里同时提供「后端 API」和「PWA 网页」）
==========================================================================

边界声明
--------
本文件**不是** Tidal_Echo 的原有能力，是 bunny 为「这台机器上没有 nginx」
这个部署环境新加的薄薄一层。原则：**不修改 Tidal_Echo 的任何一行原代码。**

backend/app.py 原样使用（`import app`），原来的行为一个字都没变。
这里只在它外面补两件原本由 nginx 干的活：

  1) 前缀剥离
     Tidal_Echo 前端把 API 基址写成同源相对路径 `/relay`
     （= 后端 RELAY_PUBLIC_PREFIX，也 = nginx 里的 location /relay/）。
     原作者靠 `proxy_pass http://127.0.0.1:3011/;` 那行末尾的斜杠去掉前缀。
     Zeabur 上没有 nginx，所以这里用一层 ASGI 中间件做同一件事。
     → 好处：前端 index.html / sw.js / album.html 一个字符都不用改，
       而且 sw.js 里「不拦截 /relay/」那条规则仍然成立（它依赖这个前缀）。

  2) 静态托管
     原本 nginx 的 `location /chat/ { alias .../web/; }` 把 web/ 交给浏览器。
     这里把 web/ 挂到根路径 `/`。
     挂载放在最后，所以 /app/* /channel/* /healthz /uploads/* 这些 API 路由
     先匹配，静态目录不会把它们吃掉。

启动方式（见 deploy/entrypoint.sh）：
    uvicorn serve:app --host 0.0.0.0 --port $PORT --app-dir /app/deploy
"""
from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

BACKEND_DIR = Path(os.environ.get("RELAY_BACKEND_DIR") or (REPO / "backend")).resolve()
WEB_DIR = Path(os.environ.get("RELAY_WEB_DIR") or (REPO / "web")).resolve()

# 让 `import app` 找到 backend/app.py（先于 uvicorn 的 --app-dir 生效）
sys.path.insert(0, str(BACKEND_DIR))

import app as relay  # noqa: E402  ← Tidal_Echo 原生后端，原样引入，不做任何 monkey patch
from starlette.staticfiles import StaticFiles  # noqa: E402

# .webmanifest 在部分 Python/系统里没有 MIME 映射，会当成 octet-stream 发出去。
# 原作者的 nginx 配置里有一行 `default_type application/manifest+json;` 专门管这个，
# Zeabur 上没有 nginx，所以这里补上。
mimetypes.add_type("application/manifest+json", ".webmanifest")

PUBLIC_PREFIX = "/" + (relay.PUBLIC_PREFIX or "").strip("/")


@relay.app.middleware("http")
async def _strip_public_prefix(request, call_next):
    """把 /relay/app/history 还原成 /app/history（= nginx proxy_pass 末尾斜杠的效果）。"""
    if PUBLIC_PREFIX != "/":
        path = request.scope.get("path", "")
        if path == PUBLIC_PREFIX:
            request.scope["path"] = "/"
        elif path.startswith(PUBLIC_PREFIX + "/"):
            request.scope["path"] = path[len(PUBLIC_PREFIX):]
    return await call_next(request)


if WEB_DIR.is_dir():
    # html=True → 目录请求返回 index.html（`/` 就是 PWA 入口）
    relay.app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="pwa")
else:
    print(f"[serve] 警告：没找到前端目录 {WEB_DIR}，只提供 API，不提供网页。")

print(
    f"[serve] backend={BACKEND_DIR}  web={WEB_DIR}  "
    f"public_prefix={PUBLIC_PREFIX}  db={relay.DB_PATH}"
)

app = relay.app
