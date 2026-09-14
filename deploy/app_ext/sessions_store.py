#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 · 会话层 —— `sessions` 表
==========================================================================

## 这一层的状态：**可重建的投影**，不是真相的搬家

必须先说清楚，否则会踩坑。

第一阶段的事实是：

    messages 表 ──(meta.api_session)──▶ 会话归属
    messages.meta.title / .archived / .deleted ──▶ 标题 / 归档 / 删除标记
    （由 deploy/sessions_manage.py 直接读写 meta，40/40 + 40/40 已验收通过）

也就是说 **标题和归档的真相现在在 `messages.meta` 里**。本文件新加的
`sessions` 表**没有抢走这个真相**，它做的是：

    把 messages 里已经存在的东西，**投影**成一份正式载体。
    投影可以随时重跑（幂等，`sync_from_messages`）。

### 为什么这次不搬真相

搬真相（让 sessions 表成为唯一真相、改写 sessions_manage）会同时动：
    · 已经 40/40 验收通过的归档/删除/改名链路
    · 前端读取路径
    · 历史数据的解释方式

**一次改三处已经验过的东西**，换来的是"更纯粹"的架构 —— 不划算，
而且一旦出问题分不清是哪一层。所以本次：

    ✅ 建表 + 投影 + 可重跑
    ⬜ 搬真相（等 P1/P2 真正需要 `summary` 与 `pinned` 的时候再做，
        那时会连同 sessions_manage 一起改，有明确的验收清单）

### 🔴 投影的规则（最重要的一条）

`sync_from_messages()` **只更新这四个字段**：
    title · pinned · archived · updated  （+ 首次建档时的 created / user_id）

**绝不碰 `summary`。** `summary` 是这张表**独有**的字段（meta 里没有对应物），
它是 P2 滚动摘要的落点。如果哪天 sync 把 summary 覆盖了，等于把摘要洗掉
—— 那会是一个非常难查的静默数据丢失。所以它不在 DO UPDATE 列表里。

## 🔴 仍然不动 messages

`sync_from_messages()` **只读** messages（一条 SELECT），不写、不改结构。
`created_at` / `updated` 都从 messages 的 `ts` 派生，不新增列。
"""

from datetime import datetime, timezone

from . import schema as _schema   # 包内相对导入（app_ext 是个包）

# 与 sessions_fallback.py / sessions_manage.py 保持一致
LEGACY_SESSION_ID = "__legacy__"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truthy(v) -> bool:
    """meta 里的标记可能是 1 / '1' / true / 'true'（第一阶段的坑：数字 vs 字符串）。"""
    return v in (1, "1", True, "true", "True")


def _scan_messages(conn) -> list:
    """一次扫描，把 messages 按会话分组统计出来。

    只读。返回 [{sid, n, first_ts, last_ts, last_id, title, archived}, ...]
    """
    rows = conn.execute(
        """
        SELECT
            COALESCE(json_extract(meta, '$.api_session'), '') AS sid,
            COUNT(*)  AS n,
            MIN(ts)   AS first_ts,
            MAX(ts)   AS last_ts,
            MAX(id)   AS last_id
        FROM messages
        GROUP BY sid
        ORDER BY last_id DESC
        """
    ).fetchall()

    out = []
    for r in rows:
        sid = (r["sid"] or "").strip() or LEGACY_SESSION_ID
        # 标题/归档取该会话"最后一条消息"上的标记 —— 整个会话每条消息
        # 都带同一份标记（sessions_manage 是整批打的），所以取最后一条最稳。
        mrow = conn.execute(
            "SELECT meta FROM messages WHERE id = ?", (r["last_id"],)
        ).fetchone()
        title, archived, pinned = None, 0, 0
        try:
            import json
            meta = json.loads((mrow["meta"] if mrow else "") or "{}")
            t = meta.get("title")
            title = str(t).strip() if t else None
            archived = 1 if _truthy(meta.get("archived")) else 0
            pinned = 1 if _truthy(meta.get("pinned")) else 0
        except Exception:
            pass
        out.append({
            "sid": sid,
            "n": int(r["n"] or 0),
            "first_ts": r["first_ts"] or now_iso(),
            "last_ts": r["last_ts"] or now_iso(),
            "last_id": int(r["last_id"] or 0),
            "title": title,
            "archived": archived,
            "pinned": pinned,
        })
    return out


def sync_from_messages(relay, user_id: str = "u_owner") -> dict:
    """把 messages 里已有的会话投影进 `sessions` 表。幂等、可随时重跑。

    🔴 只更新 title / pinned / archived / updated —— **绝不动 summary**。

    返回 {"scanned": N, "inserted": N, "updated": N, "untouched_summary": N}
    """
    import json

    with _schema.connect(relay) as conn:
        found = _scan_messages(conn)
        inserted = updated = kept = 0

        for s in found:
            row = conn.execute(
                "SELECT id, summary FROM sessions WHERE id = ?", (s["sid"],)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sessions "
                    "(id, user_id, title, since_id, pinned, summary, archived, created, updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["sid"], user_id, s["title"], 0, s["pinned"], None,
                     s["archived"], s["first_ts"], s["last_ts"]),
                )
                inserted += 1
            else:
                if row["summary"]:
                    kept += 1
                conn.execute(
                    # ⚠️ 注意这里没有 summary —— 这是有意的，别顺手加回来（见文件顶部）
                    "UPDATE sessions SET title = ?, pinned = ?, archived = ?, updated = ? "
                    "WHERE id = ?",
                    (s["title"], s["pinned"], s["archived"], s["last_ts"], s["sid"]),
                )
                updated += 1
        conn.commit()

    return {"scanned": len(found), "inserted": inserted,
            "updated": updated, "untouched_summary": kept}


def list_sessions(relay, include_archived: bool = False) -> list:
    """读 `sessions` 表（**投影**，不含消息条数）。

    ⚠️ 前端要的"消息条数 / 最后活跃"请用 `sessions_manage.list_sessions()`，
       那份是直接从 messages 算的，永远最新。这里返回的是载体里的记录。
    """
    sql = "SELECT * FROM sessions"
    if not include_archived:
        sql += " WHERE COALESCE(archived, 0) = 0"
    sql += " ORDER BY updated DESC"
    with _schema.connect(relay) as conn:
        rows = conn.execute(sql).fetchall()
        counts = {}
        for r in conn.execute(
            "SELECT COALESCE(json_extract(meta,'$.api_session'),'') AS sid, "
            "COUNT(*) AS n FROM messages GROUP BY sid"
        ).fetchall():
            counts[(r["sid"] or "").strip() or LEGACY_SESSION_ID] = int(r["n"] or 0)
    out = []
    for r in rows:
        d = dict(r)
        d["message_count"] = counts.get(d["id"], 0)
        out.append(d)
    return out


def get_session(relay, session_id: str):
    """读单条。**输出形状与 list_sessions 一致**（也带 message_count）。"""
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        # 库里 __legacy__ 对应的是"没有 api_session 标签"那一批（空字符串）
        sql_sid = "" if session_id == LEGACY_SESSION_ID else session_id
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE COALESCE(json_extract(meta, '$.api_session'), '') = ?",
            (sql_sid,),
        ).fetchone()["n"]
    d["message_count"] = int(n or 0)
    return d


def set_summary(relay, session_id: str, summary: str) -> dict:
    """写滚动摘要（P2 用）。这是 `sessions` 表**独有**的字段，sync 不会覆盖它。

    现在就把入口留好：P2 实现滚动摘要时，直接调这个，不用再改表。
    """
    with _schema.connect(relay) as conn:
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        conn.execute(
            "UPDATE sessions SET summary = ?, updated = ? WHERE id = ?",
            (summary, now_iso(), session_id),
        )
        conn.commit()
    return {"ok": True, "session_id": session_id, "chars": len(summary or "")}
