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

  3) 会话列表兜底（sessions_fallback.py）
     原版的「会话列表」不是从数据库读的，是转发问 AI 身体（api_loop）要的。
     第二阶段把 api_loop 换成 KaelLife 之后这个转发会失败 → 列表变空。
     这里补一层：转发失败时直接从 messages 表把列表算出来。
     → 详细说明见 deploy/sessions_fallback.py 顶部注释。

  4) 会话归档 / 恢复 / 清空 / 改名（sessions_manage.py）
     原版的「改名」也是转发问身体的；虚拟会话（__legacy__）永远 404。
     这里把这四件事做成数据库直读直写，**完全不走身体**。
     → 详细说明（含"归档 vs 清空"的语义区别）见 deploy/sessions_manage.py 顶部注释。

  5) P0 地基：四张表 + 身份层（app_ext/）
     users / settings / sessions / memories —— 第一阶段一直缺的四个"载体"。
     **只建表与读写，不改 messages、不改鉴权、不改原版任何端点。**
     路由在全新命名空间 /app/ext/* 下。**内部吞掉自己的异常** ——
     这层出问题必须不影响"发消息 → 收回复 → 记录不丢"这条已跑通的路。
     → 详细说明见 deploy/app_ext/__init__.py 与各模块顶部注释。

⚠️ 中间件注册顺序（**实测结论**：Starlette 的 `app.user_middleware` 是「最外层在前」，
   而 `add_middleware` 是往列表头部插 → **先注册的跑在外层**）
   实际栈：
        1. _strip_public_prefix   ← 先注册 → 最外层 → 先把 /relay 剥掉
        2. sessions_fallback      ← 后注册 → 在内层 → 看到的是 /app/sessions
   即：`sessions_fallback.install()` 必须写在 `_strip_public_prefix` 定义**之前**。
   本文件确实是这么排的（install 在上面）。
   （sessions_fallback 内部仍会做一次前缀归一，属防御性冗余：万一将来顺序被调整，
     它也不会静默失效。详见该文件注释。）

启动方式（见 deploy/entrypoint.sh）：
    uvicorn serve:app --host 0.0.0.0 --port $PORT --app-dir /app/deploy
"""
from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path

# ── 日志编码统一成 UTF-8（**必须在 `import app` / `app_ext` 之前**）────────────
# 🔴🔴 为什么要有这三行（2026-09-19 实拍，两个症状同一个根因）：
#   本机 Windows 上，stdout 一旦被重定向（管道 / 文件），Python 就按 locale 编码 =
#   **cp936(gbk)** 输出。于是：
#     ① 日志里的中文按 GBK 落盘，而所有验收脚本一律按 **utf-8** 读日志
#        （`log_path.read_text(encoding="utf-8")`）→ 中文全成乱码 →
#        "日志里说了出来"那一类断言**假红**（context_check D1 / workshop_check E8a~E8c）；
#     ② 日志里只要有一个 GBK 编不出的字符（`⚠️` 这类），print 直接
#        UnicodeEncodeError → **房子根本起不来**。而"某一步失败"恰恰只在
#        **全新 /data** 上发生 —— 于是这个 bug 只在第一次部署时出现，最难查。
#   生产（Zeabur / Linux）本来就是 UTF-8，所以这一行在线上是**空操作**；
#   它做的是"让本地和线上一致"，并让日志这个调试界面在任何地方都读得出来。
#   `errors="replace"` 再加一道保险：宁可打一个 `?`，也不许整个进程死掉。
#   ⚠️ 这**不是**对"print 必须 GBK 安全"那条红线的替代 —— 那条仍然按住（且由
#      generate_check.py 的 D12~D14 扫源码来验）。这里是多一层，不是少一层。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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


# ── 兜底：会话列表不依赖 AI 身体 ────────────────────────────────────────────
# 必须在 _strip_public_prefix **之后**注册：Starlette 后注册的中间件在最外层，
# 因此它会看到带前缀的原始 path（/relay/app/sessions），模块内部自己处理前缀。
# 顺序反过来会让兜底静默失效（它找不到 /app/sessions 就永远只做透传）。
import sessions_fallback  # noqa: E402

sessions_fallback.install(relay, public_prefix=PUBLIC_PREFIX)

# ── 会话归档 / 恢复 / 清空 / 改名（不依赖 AI 身体）──────────────────────────
# 纯新增路由（/app/sessions/manage/*），不覆盖原版任何端点。
# 每个端点自己 check_auth，不依赖中间件顺序（fail-closed）。
import sessions_manage  # noqa: E402

sessions_manage.install(relay, public_prefix=PUBLIC_PREFIX)

# ── P0 地基：四张表 + 身份层（users / settings / sessions / memories）────────
# 只建表与载体：不改 messages、不改鉴权、不覆盖原版任何端点（路由在 /app/ext/*）。
# 🔴 register() 内部吞掉自己的全部异常 —— 这层挂了，房子必须照常营业。
import app_ext  # noqa: E402

app_ext.register(relay, public_prefix=PUBLIC_PREFIX)


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
