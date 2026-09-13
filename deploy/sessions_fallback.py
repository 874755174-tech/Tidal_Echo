#!/usr/bin/env python3
"""
会话列表兜底（sessions-fallback）
================================================================================

问题
----
Tidal_Echo 的会话**列表**接口 `/app/sessions` 并不是从数据库读的：

    浏览器 → GET /app/sessions → relay(app.py:992) → 转发 → api_loop 的 /loop/sessions

`api_loop` 是「临时人偶」（第一阶段的身体）。第二阶段把它换成 KaelLife 之后，
这个转发就会失败 → 列表变空 → 前端那个「API 窗口」按钮点开什么都没有，
**新建 / 切换 / 删除会话全部失去入口**。

而前端的容错是**静默**的（catch 住就回落），用户只会觉得"按钮怎么空了"。

本模块做什么
------------
在 `deploy/` 这一层加一道兜底：**api_loop 不在时，直接从 messages 表把会话列表算出来。**

    浏览器 → GET /app/sessions
              ├─ api_loop 在   → 原样透传（行为与从前完全一致，一个字节都不动）
              └─ api_loop 不在 → 本模块接管，查库推导会话列表

会话在数据库里本来就存在：每条消息的 `meta.api_session` 就是它的会话标签
（`backend/app.py` 的 `history_for_session()` 正是按这个字段过滤的）。
所以"列表"完全可以从数据推导，**不需要问 AI 身体**。

边界声明（重要）
----------------
🔴 **本文件不改 Tidal_Echo 的任何一行原代码。**
   - `backend/app.py`：一个字符都没动（会话过滤 `history_for_session` 是它的原生能力）
   - `examples/api_loop.py`：一个字符都没动（api_loop 在时，它说了算）
   - 本层只是**在转发失败时接管**，属于"没有 nginx 时的适配层"的延伸

为什么放在中间件层而不是改路由
------------------------------
改 `app.py` 的路由 = 动原版代码（违反第一阶段红线）。
在中间件里拦截 = 纯新增，且能精确判断"转发是否失败"。
"""

from __future__ import annotations

import json

# 与 api_loop 的 sessions_public() 保持一致的形状（前端按这个结构解析）：
#   {"active_session": "...", "sessions": [{"id","title","since_id","created_at","pinned"}, ...]}
# 详见 examples/api_loop.py:170

# 前端把"没有会话标签的旧消息"归到这一组（index.html 的 LEGACY_SESSION_ID）
LEGACY_SESSION_ID = "__legacy__"


def _db_rows(relay):
    """从 messages 表推导会话列表。

    只做一件事：按 meta.api_session 分组，统计每条会话的消息数与最后活跃时间。
    ⚠️ 拿不到「标题」——因为原版设计里 session 只是 meta 里的一个标签，
       标题从没被存进数据库。所以这里用会话 ID 当标题，保证界面能用。
    """
    try:
        with relay.db() as conn:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(json_extract(meta, '$.api_session'), '') AS sid,
                    COUNT(*)      AS n,
                    MIN(ts)       AS first_ts,
                    MAX(ts)       AS last_ts,
                    MAX(id)       AS last_id
                FROM messages
                GROUP BY sid
                ORDER BY last_id DESC
                """
            ).fetchall()
    except Exception:
        return None

    sessions = []
    legacy_n = 0
    for r in rows:
        sid = (r["sid"] or "").strip()
        n = int(r["n"] or 0)
        if not sid:
            legacy_n = n
            continue
        sessions.append({
            "id": sid,
            "title": sid,                 # 没有真实标题，用 ID 兜底（见上方说明）
            "since_id": 0,
            "created_at": r["first_ts"] or "",
            "pinned": False,
            # 以下是本兜底层额外附带的字段（api_loop 不返回），前端忽略即可
            "_n": n,
            "_last_ts": r["last_ts"] or "",
        })

    # 旧消息（无标签）单独成一组，与前端 history_for_session('__legacy__') 对应
    if legacy_n:
        sessions.append({
            "id": LEGACY_SESSION_ID,
            "title": "更早的消息",
            "since_id": 0,
            "created_at": "",
            "pinned": False,
            "_n": legacy_n,
            "_last_ts": "",
        })

    active = ""
    for s in sessions:
        if s["id"] != LEGACY_SESSION_ID:
            active = s["id"]
            break
    return {"active_session": active, "sessions": sessions, "fallback": True}


def install(relay, public_prefix: str = "/") -> None:
    """把兜底挂到 relay 的 ASGI 应用上。

    做法：再包一层 http 中间件。原中间件负责剥前缀，这一层负责"转发失败时接管"。

    ⚠️⚠️ 中间件顺序（**实测**，Starlette `app.user_middleware` 列表是「最外层在前」）
        实际栈（serve.py 里本模块先注册、_strip_public_prefix 后注册）：
            BaseHTTPMiddleware(_strip_public_prefix)   ← 最外层
            BaseHTTPMiddleware(_sessions_fallback)
            CORSMiddleware
        即：**先注册的反而在外层**，`_strip_public_prefix` 先跑，
        `_sessions_fallback` 在它里面 → **它看到的已经是剥完前缀的 `/app/sessions`**。
        （这一点被实测推翻过一次：我原先以为"后注册的在外层"，
          于是给 _normalized 加了前缀剥离；其实剥不剥都碰得上，属冗余但无害。
          保留它，是为了不管将来谁调整注册顺序，都不会静默失效。）

    public_prefix：与 serve.py 的 PUBLIC_PREFIX 同源，形如 `/relay` 或 `/`。

    🔴 安全红线：**兜底绝不能把「没通过鉴权」当成「上游挂了」来处理。**
       曾经踩过：`call_next` 返回 401（密钥错 / 没带密钥）时也被我们接管，
       于是 401 被换成了 200 + 一份完整会话列表 → **整个鉴权在这一条端点上被绕过**。
       现在的规则是：
         1. 只接管 5xx / 502 / 504（= 网关或上游失败），401/403/404 一律原样返回；
         2. 兜底真正生效前，**自己再独立查一遍密钥**（见 `_auth_ok`），
            这样即使将来有人改错了状态码判断，也不会漏掉鉴权。
"""
    prefix = "/" + (public_prefix or "").strip("/")

    def _normalized(path: str) -> str:
        """把 /relay/app/sessions 归一成 /app/sessions（仅用于比较，不改请求）。"""
        if prefix != "/" and path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    def _auth_ok(request) -> bool:
        """独立复核密钥（不依赖 app.py 的状态码，也不依赖 call_next 的行为）。

        与 backend/app.py 的 check_auth() 同源规则：
        接受 `Authorization: Bearer <secret>`，或 `?token=<secret>`（EventSource 用）。
        """
        secret = (getattr(relay, "SECRET", "") or "")
        if not secret:
            # 后端没配密钥时它自己会拒绝一切，这里不替它做决定
            return False
        auth = request.headers.get("authorization", "") or ""
        token = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token")
        if not token:
            return False
        import hmac
        return hmac.compare_digest(token, secret)

    target = "/app/sessions"

    # 只在这些状态下才认为"是上游/网关挂了"，而不是"这个请求本身不该被服务"
    UPSTREAM_DOWN = {500, 502, 503, 504}

    @relay.app.middleware("http")
    async def _sessions_fallback(request, call_next):
        path = request.scope.get("path", "")
        # 只接管这一个端点，且只接管 GET（POST 是"新建"，语义不同，不兜底）
        if _normalized(path) != target or request.method != "GET":
            return await call_next(request)

        response = await call_next(request)

        # api_loop 在 → 原样透传，行为与从前完全一致
        if response.status_code == 200:
            return response

        # 🔴 401 / 403 / 404 等是"这个请求不该被服务"，**不是**上游挂了 → 原样返回，
        #    绝不能用兜底数据把它盖掉（那等于绕过鉴权）。
        if response.status_code not in UPSTREAM_DOWN:
            return response

        # 上游确实挂了。兜底之前再独立查一遍密钥：没通过就仍然 401。
        if not _auth_ok(request):
            return response

        data = _db_rows(relay)
        if data is None:
            return response  # 连数据库都读不了，那就老实把原错误给前端

        # 把失败原因带上，方便前端给出可理解的提示
        data["fallback_reason"] = f"upstream {response.status_code}"

        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json; charset=utf-8",
                   "content-length": str(len(body))}
        return _JsonResponse(body, headers)


def _JsonResponse(body: bytes, headers):
    """构造一个最小的 Starlette Response（避免与 relay 的命名冲突）。"""
    from starlette.responses import Response
    return Response(content=body, status_code=200, headers=headers)
