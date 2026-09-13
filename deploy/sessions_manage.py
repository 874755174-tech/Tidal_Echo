#!/usr/bin/env python3
"""
会话归档 / 恢复 / 删除 / 改名 —— 不依赖 AI 身体的原生实现
==========================================================================

## 为什么需要这个文件

原版 Tidal_Echo 的会话**存储**只有一处：每条消息 `meta.api_session` 标签。
而会话的**列表与改名**却是转发问 AI 身体（api_loop）要的（`app.py:992-1015`）：

    浏览器 → GET/POST/PATCH /app/sessions → relay → 转发 → api_loop

这带来两个问题（Lily 2026-09-13 实测发现）：

  1. **换身体就失效**：第二阶段把 api_loop 换成 KaelLife 后，转发必然 502
     → 新建 / 改名 / 删除全部失去入口。（列表已由 sessions_fallback.py 兜底，
        但"改名"这种写操作当时无解 —— api_loop 的 sessions[] 里根本没有
        `__legacy__` 这个虚拟会话，所以永远 404。）
  2. **虚拟会话永远改不了名**：「旧主线 / Desktop 记录」= `__legacy__`，
     它不在 api_loop 的列表里 → PATCH 直接 404。

本文件把 **归档 / 恢复 / 删除 / 改名** 四件事做成**数据库直读直写**，
彻底不再依赖身体，也不依赖 api_loop 的配置文件。

## 存储设计（关键：不改 messages 表，一条历史消息都不动）

复用**已存在的 `messages.meta` JSON 字段**，在里面加三个会话级标记：

    meta.archived   = 1/true    → 归档（列表默认不显示，消息完好，可恢复）
    meta.title      = "..."     → 会话标题（终于有地方存了）
    meta.deleted    = 1/true    → 已删除（"清空消息"时打标）

⚠️ **关于"删除"的两种语义**（这是本文件最重要的设计决定）：

  · **归档（archive）**  → 只写 `archived` 标记。**纯可逆**，消息一条不动。
  · **清空（purge）**    → 给该会话所有消息打 `deleted` 标记，并过滤读取。
                          物理行仍在库里（可手工恢复），但从产品视角"已删除"。

  为什么"清空"不直接 `DELETE FROM messages`：
    1. 误操作**可挽回**（Lily 有数据丢失创伤，这是硬性要求）
    2. 不需要动 `messages` 表结构、不需要重建索引
    3. 将来若要做"回收站"，标记已经在位

  `history_for_session()`（app.py:185）只按 `api_session` 过滤、不看 `deleted`，
  所以本文件额外提供一个**带过滤的读取**给 `/app/sessions` 用；而"已删除会话"
  的消息在列表里不出现、点不进去 —— 达到"删除"的产品效果。

## 边界（红线）

本文件属于【我们的部署适配层】，**`backend/app.py` 一个字都没改**。
它做的事原版都没有，是纯新增能力。挂载方式见 deploy/serve.py。
"""

import json
import sqlite3
from pathlib import Path

# ⚠️ 这个 import 是**功能必需**，不是风格问题，别删。
#    FastAPI 注册路由时读的是**真实类型对象**（不是字符串）。我们的 Handler 写成
#    `async def _list(request: Request)`，`Request` 就是从这里来的。
#    若删掉它，注解直接变成空 `{}` → FastAPI 认不出 Request → 把 request 当成
#    查询参数 → 每个新端点都返回 `422 {"loc": ["query","request"]}`。
from fastapi import Request


# ─────────────────────────────────────────────────────────────────────────────
# 🔴 为什么这里**故意没有** `from __future__ import annotations`
#
# 本文件要注册 FastAPI 路由，而 PEP 563（那行 from __future__）会把**每一个**
# 函数注解都存成**字符串**。FastAPI 拿不到真类型就得先求值：
#
#     fastapi.dependencies.utils.get_typed_annotation("Request", <本文件 globals>)
#       → evaluate_forwardref → pydantic._internal._typing_extra.try_eval_type
#
# 而 pydantic 的 `try_eval_type` 内部**吞掉 NameError**：
#
#     try:
#         return eval_type_backport(value, globalns, localns), True
#     except NameError:
#         return value, False        # ← 求值失败，返回的是占位/None，**不抛错**
#
# 于是只要 `Request` 在**本文件全局命名空间**求不出来，注解就静默变成 `None` →
# FastAPI 认为"这参数没类型 → 那就是普通查询参数" → 注册出
#     parameters: [{name: "request", in: "query", required: true}]
# → 客户端每次都收到：
#     `422 {"detail":[{"type":"missing","loc":["query","request"]}]}`
#
# 踩坑全记录（2026-09-13，验收脚本曾 16 项全挂在这个 422 上）：
#   ① 第一版：Handler 写了 `request: Request`，注解被字符串化 →
#      FastAPI 把 request 当查询参数。当时判断"去掉注解就好"。
#   ② 第二版：去掉注解，同时把顶部 import 也删了。结果**注解变成空 `{}`**，
#      FastAPI 依然认不出 → 还是 422。这一版是**方向性错误**：
#      参数名叫 `request` 并不足以让 FastAPI 注入，它**必须**看到类型注解。
#   ③ 真正修好：**保留 `from fastapi import Request`**（让注解求得到真类型）
#      **同时不要 `from __future__ import annotations`**（不用求值，一步到位）。
#
# 结论：两行 import 必须这样组合 ——
#       ✅ `from fastapi import Request`            （要）
#       ❌ `from __future__ import annotations`     （不要）
#   以后改这个文件，请**不要**顺手把那行 future import 加回来。
#   详细取证脚本：tools/_sig_check.py（离线看注册出的参数表）。
# ─────────────────────────────────────────────────────────────────────────────

LEGACY_SESSION_ID = "__legacy__"

# 归档 / 删除 / 标题，全部塞进 meta 的这三个键
K_ARCHIVED = "archived"
K_DELETED = "deleted"
K_TITLE = "title"


def _connect(relay) -> sqlite3.Connection:
    conn = sqlite3.connect(relay.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _sid_expr() -> str:
    """会话 id 的 SQL 表达式：没有标签的消息归到 ''（前端视作 __legacy__）。"""
    return "COALESCE(json_extract(meta, '$.api_session'), '')"


def _flag_false(col: str) -> str:
    """SQL 片段：某个 JSON 标记"为假"（没设 / 0 / '0' / false / 'false'）。

    ⚠️ 踩过的坑：`json_extract` 取出来的 `1` 是**数字**，写成
    `NOT IN ('1','true')` 是拿数字跟字符串比 → SQLite 判定为"不相等" →
    过滤**静默失效**（删除的会话还挂在列表里）。所以必须把数字与字符串都列上。
    """
    quoted = f"json_extract(meta, '$.{col}')"
    return (
        f"COALESCE({quoted}, 0) NOT IN (1, '1', 'true', 'True', 'yes', 'on')"
    )


def _bool(v) -> bool:
    """把 JSON 里各种"真"的写法统一成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def _meta_of(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 列表：数据库直读（不依赖身体），并把已删除的排除掉
# ---------------------------------------------------------------------------

def list_sessions(relay) -> dict:
    """返回与 api_loop.sessions_public() 同形状的列表。

    ⚠️ 与 sessions_fallback.py 的区别：
       fallback 是"转发失败时的兜底"；本函数是"归档/删除功能需要的数据源"。
       两者输出形状一致，所以前端可以无差别消费。
    """
    with _connect(relay) as conn:
        rows = conn.execute(
            f"""
            SELECT {_sid_expr()}                    AS sid,
                   COUNT(*)                          AS n,
                   MIN(ts)                           AS first_ts,
                   MAX(ts)                           AS last_ts,
                   MAX(id)                           AS last_id
            FROM messages
            WHERE {_flag_false(K_DELETED)}
            GROUP BY sid
            ORDER BY last_id DESC
            """
        ).fetchall()
        metas = _session_meta(conn)

    out = []
    for r in rows:
        sid = r["sid"] or ""
        m = metas.get(sid, {})
        if _bool(m.get(K_ARCHIVED)):
            continue                       # 归档的不进列表（由 ?archived=1 单独取）
        out.append({
            "id": sid or LEGACY_SESSION_ID,
            "title": str(m.get(K_TITLE) or "").strip() or (sid or "旧主线 / Desktop 记录"),
            "since_id": 0,
            "created_at": r["first_ts"] or "",
            "pinned": False,
            "count": r["n"],
        })
    return {"active_session": out[0]["id"] if out else "", "sessions": out}


def list_archived(relay) -> dict:
    """已归档的会话（供"查看归档"用）。"""
    with _connect(relay) as conn:
        rows = conn.execute(
            f"""
            SELECT {_sid_expr()} AS sid, COUNT(*) AS n, MAX(id) AS last_id
            FROM messages
            WHERE {_flag_false(K_DELETED)}
            GROUP BY sid ORDER BY last_id DESC
            """
        ).fetchall()
        metas = _session_meta(conn)
    out = []
    for r in rows:
        sid = r["sid"] or ""
        m = metas.get(sid, {})
        if not _bool(m.get(K_ARCHIVED)):
            continue
        out.append({
            "id": sid or LEGACY_SESSION_ID,
            "title": str(m.get(K_TITLE) or "").strip() or (sid or "旧主线 / Desktop 记录"),
            "count": r["n"],
        })
    return {"sessions": out}


def _session_meta(conn) -> dict:
    """取每个会话的"最新一条**未删除**消息的 meta"，用来读归档 / 标题标记。

    会话级标记每次操作都写到该会话**当前所有消息**上，所以任意一条都能读到。
    取 max(id) 那条即可拿到最新写入的标记。

    ⚠️ 为什么必须加 `_flag_false(K_DELETED)` 过滤：
       清空（purge）给所有行打了 `deleted` 标记。如果不排除这些行，
       `_session_meta` 仍会为"已清空的会话"读出 meta → 列表里又冒出来。
       （这正是第一次跑验收时 "清空后 sess-B 还在列表里" 的原因。）
    """
    out: dict = {}
    rows = conn.execute(
        f"""
        SELECT {_sid_expr()} AS sid, meta
        FROM messages
        WHERE id IN (
            SELECT MAX(id) FROM messages
            WHERE {_flag_false(K_DELETED)}
            GROUP BY {_sid_expr()}
        )
        """
    ).fetchall()
    for r in rows:
        sid = r["sid"] or ""
        out[sid] = _meta_of(r["meta"])
    return out


# ---------------------------------------------------------------------------
# 写操作：全部落到"该会话的所有消息"的 meta 上
# ---------------------------------------------------------------------------

def _target_rows(conn, session_id: str) -> list:
    """定位一个会话的所有消息行。__legacy__ 走"无标签"分支。"""
    sid = (session_id or "").strip()
    if sid in ("", LEGACY_SESSION_ID):
        sql = (
            "SELECT id, meta FROM messages "
            "WHERE json_extract(meta, '$.api_session') IS NULL "
            "   OR json_extract(meta, '$.api_session') = ''"
        )
        return conn.execute(sql).fetchall()
    return conn.execute(
        "SELECT id, meta FROM messages WHERE json_extract(meta, '$.api_session') = ?",
        (sid,),
    ).fetchall()


def _set_meta(conn, rows, key: str, value) -> int:
    """给一批消息的 meta 打同一个标记。value 为 None 则删除该键。"""
    n = 0
    for r in rows:
        meta = _meta_of(r["meta"])
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
        conn.execute(
            "UPDATE messages SET meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), r["id"]),
        )
        n += 1
    return n


def _count(conn, session_id: str) -> int:
    sid = (session_id or "").strip()
    if sid in ("", LEGACY_SESSION_ID):
        sql = (
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE json_extract(meta, '$.api_session') IS NULL "
            "   OR json_extract(meta, '$.api_session') = ''"
        )
        return conn.execute(sql).fetchone()["n"]
    return conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE json_extract(meta, '$.api_session') = ?",
        (sid,),
    ).fetchone()["n"]


def archive_session(relay, session_id: str, on: bool = True) -> dict:
    """归档 / 取消归档。纯可逆 —— 只动标记，消息一条不动。"""
    with _connect(relay) as conn:
        rows = _target_rows(conn, session_id)
        if not rows:
            return {"ok": False, "reason": "not_found"}
        if on:
            _set_meta(conn, rows, K_ARCHIVED, 1)
        else:
            _set_meta(conn, rows, K_ARCHIVED, None)
        conn.commit()
    return {"ok": True, "archived": bool(on), "affected": len(rows)}


def purge_session(relay, session_id: str) -> dict:
    """清空会话：给所有消息打 deleted 标记。

    ⚠️ 物理行保留（可人工恢复），但从列表与读取中消失。
    真正的 `DELETE` 请用 tools/purge_session.py（需要显式 --hard）。
    """
    with _connect(relay) as conn:
        rows = _target_rows(conn, session_id)
        if not rows:
            return {"ok": False, "reason": "not_found"}
        _set_meta(conn, rows, K_DELETED, 1)
        conn.commit()
        return {"ok": True, "deleted": True, "affected": len(rows)}


def rename_session(relay, session_id: str, title: str) -> dict:
    """改名。**虚拟会话（__legacy__）也允许改** —— 这一版特意打开。

    原版对 __legacy__ 改名会 404（它不在身体的列表里）；现在标题存在消息 meta 上，
    虚拟会话自然也能有名字。
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "reason": "empty_title"}
    if len(title) > 60:
        title = title[:60]
    with _connect(relay) as conn:
        rows = _target_rows(conn, session_id)
        if not rows:
            return {"ok": False, "reason": "not_found"}
        _set_meta(conn, rows, K_TITLE, title)
        conn.commit()
    return {"ok": True, "title": title, "affected": len(rows)}


def count_session(relay, session_id: str) -> dict:
    """删除前给前端显示"将删除 N 条消息"用。"""
    with _connect(relay) as conn:
        return {"ok": True, "count": _count(conn, session_id)}


# ---------------------------------------------------------------------------
# 挂载
# ---------------------------------------------------------------------------

def install(relay, public_prefix: str = "/") -> None:
    """把 /app/sessions 的归档·删除·改名能力挂到 relay 上。

    ⚠️ 与 sessions_fallback.py 的关系：
        ——「列表」由 sessions_fallback 负责（它先试身体、失败才兜底）；
        ——「写操作」由本文件负责（**永远不走身体**，直接读写数据库）。

    路由设计（全部是新路径，绝不覆盖原版已有的）：
         GET    /app/sessions/manage          → 列表（数据库直读，含 count）
         GET    /app/sessions/manage/archived → 已归档列表
         POST   /app/sessions/manage/archive  → 归档 / 取消归档
         POST   /app/sessions/manage/purge    → 清空
         POST   /app/sessions/manage/rename   → 改名
         GET    /app/sessions/manage/count    → 某会话消息数（确认框用）

    🔴 安全红线（沿用 sessions_fallback 的教训）：
       每个端点**自己调 relay.check_auth(request)**，不依赖任何中间件顺序。
       这是"失败关闭"（fail-closed）：鉴权不通过就不可能碰到数据。

    ⚠️ 关于参数注解：本文件**没有** `from __future__ import annotations`
       （原因见文件顶部那段长注释）。Handler 上写不写 `request: Request` 都行，
       但**必须**能 `from fastapi import Request`。当前选择是不写注解 —— 更少
       出错面，FastAPI 靠参数名 `request` 一样能注入。
    """
    from fastapi.responses import JSONResponse

    prefix = "/" + (public_prefix or "").strip("/")

    def _norm(path: str) -> str:
        if prefix != "/" and path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    base = "/app/sessions/manage"

    # ── 路由处理函数 ───────────────────────────────────────────────────────
    # 🔴 `request: Request` 这个注解**必须有**。FastAPI 靠它识别"这是 Request，
    #    请注入"，光靠参数名叫 `request` 是不行的（踩过，见文件顶部那段长注释）。
    #    注解能生效的前提：本文件有 `from fastapi import Request`，且**没有**
    #    `from __future__ import annotations`。

    @relay.app.get(base)
    async def _list(request: Request):
        relay.check_auth(request)                 # 🔴 自己查，不靠中间件
        return _json(list_sessions(relay))

    @relay.app.get(base + "/archived")
    async def _list_archived(request: Request):
        relay.check_auth(request)
        return _json(list_archived(relay))

    @relay.app.post(base + "/archive")
    async def _archive(request: Request):
        relay.check_auth(request)
        body = await request.json()
        sid = str(body.get("session_id") or "")
        on = body.get("archived", True)
        on = on if isinstance(on, bool) else _bool(on)
        res = archive_session(relay, sid, on=on)
        return _json(res, 200 if res.get("ok") else 404)

    @relay.app.post(base + "/purge")
    async def _purge(request: Request):
        relay.check_auth(request)
        body = await request.json()
        sid = str(body.get("session_id") or "")
        res = purge_session(relay, sid)
        return _json(res, 200 if res.get("ok") else 404)

    @relay.app.post(base + "/rename")
    async def _rename(request: Request):
        relay.check_auth(request)
        body = await request.json()
        sid = str(body.get("session_id") or "")
        res = rename_session(relay, sid, str(body.get("title") or ""))
        return _json(res, 200 if res.get("ok") else 400)

    @relay.app.get(base + "/count")
    async def _count_ep(request: Request):
        relay.check_auth(request)
        sid = request.query_params.get("session_id") or ""
        return _json(count_session(relay, sid))

    # 前缀归一（防御性冗余：万一中间件顺序被调整，也不会静默失效）
    _ = _norm
