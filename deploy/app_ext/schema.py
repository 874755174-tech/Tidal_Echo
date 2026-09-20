#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 · 地基 —— 四张表（users / settings / sessions / memories）
==========================================================================

## 这个文件解决什么

Tidal_Echo 原版只有两张表：`messages`（含 `meta` JSON）和 `push_subscriptions`。
聊天能跑，但**"身份 / 设置 / 会话 / 长期记忆"这四类东西没有正式载体**，
于是每加一个功能都只能临时往 `meta` 里塞：

  · 设置（persona / 模型 / 温度 / context 阈值）没有家
    → 只能塞环境变量 → 改一次要重新部署一次
  · 会话标题没有家
    → 第一阶段只能挂在"该会话每条消息"的 meta 上（能用，但不是载体）
  · 长期记忆没有家
    → P2 的滚动摘要、记忆提炼无处落

本文件把四个载体建起来。**只建表，不放任何功能逻辑。**

## 🔴 三条硬约束

1. **`messages` 表：结构不改、行不改、正文不改。**
   `ensure_schema()` 在建表前后各取一次 `messages` 的 DDL 快照，
   **只要发现变化就抛错**。这不是注释里的承诺，是运行时断言
   （见 `_messages_signature` / 调用处那段 `if before != after: raise`）。
   写在注释里的规矩会被后人删掉，跑得起来的断言不会。

   > 🆕 **2026-09-19 收窄（⑨ stop / retry / reroll 需要）**
   > 原话是「`messages` 表一个字不改、一行不动」，现在**收窄**为：
   > **只准写 `meta` 这一列**（`truncated` / `superseded` / `variants` 都住在那）。
   > 约束从「别碰这个表」变成**三条更硬的具体禁令**，由
   > `messages_body_signature()` + `with_meta_guard()` 在运行时守住：
   >   ① **行数不变**（不许 insert / delete）
   >   ② **`id / ts / direction / kind / text` 逐字不变**（不许改正文）
   >   ③ **只允许 `UPDATE … SET meta = ?`**（唯一写入口 = `update_message_meta()`）
   > **为什么敢收窄**：⑨ 在规划里的定位本来就是「改写库逻辑」；而且读那边早就
   > 依赖这个表了（⑧ 整层都靠读 `messages` 工作）。真正危险的是"改正文 / 删行"，
   > 而这两条被上面的断言**结构性挡住** —— 比一句泛泛的"不许碰"更能说明白守什么。
   > ⚠️ 本次**不升 `SCHEMA_VERSION`**：表结构一格没变，变的只是写入策略。
   > `user_version` 记的是**结构**，不是策略。

2. **幂等。**
   全部 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，
   连跑 N 次结果完全一致。老库（只有 2 张表的库）升级时自动补齐，
   **不重建表、不搬数据、不改任何已有行**。

3. **版本号存在 `PRAGMA user_version`。**
   不新增"迁移记录表"——那会变成第 5 张表，而 `user_version` 本来就是
   SQLite 为这个用途留的库头字段。当前 `SCHEMA_VERSION = 3`。

## 为什么落在 `deploy/app_ext/`，而不是 `backend/app_ext/`

`架构与产品路线规划.md` §3.2 写的是 `backend/app_ext/`。实际落在这里，
**只有一个理由**：

    红线自查命令是
        git diff --stat e7c9bf5 -- backend/ examples/ channel/
    它要求输出为 **空**。

  一旦把新目录提交进 `backend/`，这条命令就不再为空，「零改动」这个
  **一眼可验**的信号就消失了 —— 以后每次自查都要人工判断"这是新增还是改动"。
  放在 `deploy/` 下则它永远是空的，红线语义保持干净。

  而且 `deploy/` 事实上**已经**是我们这一层：`sessions_fallback.py`、
  `sessions_manage.py` 都不是 nginx 的活，是业务兜底。四张表和它们放一起，
  比"一半 backend 一半 deploy"更清楚。

  （本节结论已于 2026-09-14 回填 `架构与产品路线规划.md` §3.2。）
"""

import hashlib
import sqlite3
from typing import Optional


# 当前 schema 版本。以后每次改表结构 +1，并在 `ensure_schema()` 的
# 「迁移」那一段里补一步（见该函数内 "v1 → v2" 的写法：先 PRAGMA table_info
# 看列在不在，再 ALTER —— 这样新库/老库跑同一段代码都安全）。
#
#   1 → 四张表（P0，2026-09-14）
#   2 → settings.provider_id（P1 模型网关要"供应商"这个概念，2026-09-14）
#   3 → sessions.summary_upto（P2 ⑧ 滚动摘要要知道"已经压到哪条消息了"，
#       否则每次压缩都要把全部旧消息重新喂一遍 —— 2026-09-19）
SCHEMA_VERSION = 3

# 四张表的建表语句。
# ⚠️ 顺序有依赖：users 先建，其余三张都 REFERENCES users(id)。
# ⚠️ 全部 IF NOT EXISTS —— 重复执行必须无害。
DDL_TABLES = [
    # ① 身份层：解决"设置属于谁 / 数据属于谁"
    ("users", """
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT PRIMARY KEY,        -- 内部 ID（稳定、可读，如 u_owner）
            handle       TEXT UNIQUE,             -- 登录名
            display_name TEXT,                    -- 显示名
            secret_hash  TEXT NOT NULL,           -- 🔴 存 hash，绝不存明文密钥
            role         TEXT DEFAULT 'owner',    -- owner | guest
            created      TEXT NOT NULL
        )
    """),

    # ② 设置层：挂身份，一条身份一行
    ("settings", """
        CREATE TABLE IF NOT EXISTS settings (
            user_id         TEXT PRIMARY KEY REFERENCES users(id),
            persona         TEXT,
            provider_id     TEXT,                 -- P1：供应商 id（deepseek/relay/...）
            model_id        TEXT,                 -- 指向服务端 PROVIDERS 允许列表里的 id
            max_tokens      INTEGER,
            temperature     REAL,
            top_p           REAL,
            context_keep    INTEGER,              -- 保留多少轮原始上下文
            context_trigger INTEGER,              -- 满多少触发压缩
            effort          TEXT,                 -- low | medium | high | max
            extra           TEXT DEFAULT '{}',    -- 预留：以后加参数不用改表
            updated         TEXT NOT NULL
        )
    """),

    # ③ 会话层：标题 / 摘要 / 归档终于有家了
    ("sessions", """
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,          -- 沿用现有 meta.api_session 的值
            user_id    TEXT NOT NULL REFERENCES users(id),
            title      TEXT,
            since_id   INTEGER DEFAULT 0,
            pinned     INTEGER DEFAULT 0,
            summary    TEXT,                      -- 滚动摘要写这里（P2 ⑧）
            summary_upto INTEGER DEFAULT 0,       -- 摘要已覆盖到的最大 message id（v3 加）
            archived   INTEGER DEFAULT 0,
            created    TEXT NOT NULL,
            updated    TEXT NOT NULL
        )
    """),

    # ④ 记忆层：房子本地的"工作记忆"（不是 OB 的替代品，见 §6）
    ("memories", """
        CREATE TABLE IF NOT EXISTS memories (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id),
            kind       TEXT NOT NULL,             -- fact | preference | relationship | event
            text       TEXT NOT NULL,
            source_msg INTEGER,                   -- 从哪条消息提炼的（可回溯、可纠正）
            salience   REAL DEFAULT 0.5,          -- 重要度（内部权重，永不展示）
            created    TEXT NOT NULL,
            last_used  TEXT
        )
    """),
]

# 索引：只建"查询真的会用到"的那几条，不铺张。
DDL_INDEXES = [
    ("idx_sessions_user", "CREATE INDEX IF NOT EXISTS idx_sessions_user "
                          "ON sessions(user_id, updated DESC)"),
    ("idx_memories_user_sal", "CREATE INDEX IF NOT EXISTS idx_memories_user_sal "
                              "ON memories(user_id, salience DESC)"),
    ("idx_memories_source", "CREATE INDEX IF NOT EXISTS idx_memories_source "
                            "ON memories(source_msg)"),
]

TABLE_NAMES = [name for name, _ in DDL_TABLES]


def connect(relay) -> sqlite3.Connection:
    """打开数据库连接。

    与 `backend/app.py` 的 `relay.db()` 唯一的区别：
    这里显式打开外键约束。SQLite 的 `PRAGMA foreign_keys` 是**连接级**开关，
    默认关闭 —— 不打开的话，DDL 里那些 REFERENCES 只是文档，不会被强制。

    ⚠️ PRAGMA 必须在事务开始前执行，所以这里紧跟 connect 就设。
    ⚠️ 既有模块（sessions_manage.py）用自己的 `_connect`，本次不动它。
    """
    conn = sqlite3.connect(relay.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _norm_sql(sql: Optional[str]) -> str:
    """归一化建表语句：折叠空白，便于前后比对。"""
    return " ".join((sql or "").split())


def _messages_signature(conn) -> str:
    """取 `messages` 表的 DDL 快照（空串 = 这张表还不存在）。

    为什么用 DDL 而不是"列名列表"：`ALTER TABLE ... ADD COLUMN` 会改写
    `sqlite_master.sql`，而只看列名会漏掉"改了约束/默认值"这种情况。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    return _norm_sql(row["sql"] if row else "")


def _existing_tables(conn) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r["name"] for r in rows}


def _get_user_version(conn) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)


def schema_report(relay) -> dict:
    """只读诊断：当前库里有哪些表 / 版本号 / 四张表是否齐。"""
    with connect(relay) as conn:
        have = _existing_tables(conn)
        ver = _get_user_version(conn)
        counts = {}
        for t in TABLE_NAMES:
            if t in have:
                counts[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        msg_n = 0
        if "messages" in have:
            msg_n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    return {
        "schema_version": ver,
        "expected_version": SCHEMA_VERSION,
        "up_to_date": ver >= SCHEMA_VERSION,
        "tables_present": {t: (t in have) for t in TABLE_NAMES},
        "tables_ok": all(t in have for t in TABLE_NAMES),
        "row_counts": counts,
        "messages_rows": msg_n,
    }


def ensure_schema(relay) -> dict:
    """建齐四张表（幂等）。**本函数自己不碰 `messages`。**

    返回：
        {
          "version":      3,
          "created":      ["users", ...],   # 本次新建的表
          "already":      [...],            # 本已存在的表
          "indexes":      [...],            # 本次新建的索引
          "migrated":     [...],            # 本次补上的列（如 sessions.summary_upto）
          "messages_rows": N,               # 建表后 messages 的行数（应与建表前一致）
          "messages_untouched": True,
        }

    🔴 建表前后各取一次 `messages` 的 DDL 快照 + 行数，任一不同立即抛错。
       这是"绝不碰 messages"这条红线的**运行时**保证，不是注释承诺。
       ⚠️ 迁移那一段也在快照覆盖范围内 —— 别把迁移写到快照之外去。
    """
    with connect(relay) as conn:
        before_sig = _messages_signature(conn)
        had = _existing_tables(conn)
        msg_before = None
        if "messages" in had:
            msg_before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

        created, already = [], []
        for name, ddl in DDL_TABLES:
            (already if name in had else created).append(name)
            conn.execute(ddl)

        made_indexes = []
        for name, ddl in DDL_INDEXES:
            conn.execute(ddl)
            made_indexes.append(name)

        # ---- 迁移：给老库补上后加的列 ----
        # 🔴 先 `PRAGMA table_info` 看列在不在，再 `ALTER`：
        #    新库已经由 DDL 建好了这些列，上来就 ALTER 会 "duplicate column name"。
        #    **同一段代码要同时伺候新库和老库** —— 这是幂等的关键。
        #    以后每加一列都在这里补一步，并把 SCHEMA_VERSION +1。
        migrated: list = []
        additions = [
            # (表, 列, 类型/默认值, 版本)
            ("settings", "provider_id", "TEXT", "v2"),
            ("sessions", "summary_upto", "INTEGER DEFAULT 0", "v3"),
        ]
        for table, col, decl, _ver in additions:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                migrated.append(f"{table}.{col}")

        # ---- 红线断言 ----
        after_sig = _messages_signature(conn)
        if before_sig != after_sig:
            raise RuntimeError(
                "红线被破坏：ensure_schema 动了 messages 表结构！\n"
                f"  before: {before_sig[:200]}\n"
                f"  after : {after_sig[:200]}"
            )
        msg_after = None
        if "messages" in _existing_tables(conn):
            msg_after = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        if msg_before != msg_after:
            raise RuntimeError(
                f"红线被破坏：messages 行数变了（{msg_before} → {msg_after}）"
            )

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    return {
        "version": SCHEMA_VERSION,
        "created": created,
        "already": already,
        "indexes": made_indexes,
        "migrated": migrated,
        "messages_rows": msg_after,
        "messages_untouched": True,
    }


# ══════════════════════════════════════════════════════════════════════════
# 🔴 messages 红线的「第二道看守」—— 只准写 meta（2026-09-19 · ⑨ 收窄）
# ══════════════════════════════════════════════════════════════════════════
#
# 上面 `ensure_schema()` 里那道 `_messages_signature` 守的是 **表结构**（DDL 快照）。
# ⑨ stop / retry / reroll 要在 `messages.meta` 里记 truncated / superseded / variants
# —— 那是**行数据**，DDL 看守看不见它。所以这里补第二道，两道正交：
#
#     _messages_signature      守「表结构没被改」
#     messages_body_signature  守「行数没变 + 正文没被改」
#
# 唯一被允许的写入口是 `update_message_meta()`；它自带守卫，且**只发一条**
# `UPDATE … SET meta = ?`。想绕过去就得自己开连接 —— 而验收脚本会扫源码盯这件事。

#: 扩展层唯一被允许改的 `messages` 列。只有一个，且必须是 JSON 列。
META_WRITABLE_COLUMNS = ("meta",)

#: 正文列 —— 这些列一个字符都不许动（守卫逐字比对的就是它们）。
MESSAGES_BODY_COLUMNS = ("id", "ts", "direction", "kind", "text")


def messages_body_signature(conn) -> str:
    """取 `messages` 的**正文快照**：`行数 : 每行正文列拼接后的 sha256`。

    🔴 这是收窄后那条红线的运行时保证。任何"改正文 / 删行 / 加行"都会让返回值变化。
    `meta` 列**故意不在**指纹里 —— 它是唯一允许改的那一格。
    （`ORDER BY id` 保证顺序稳定，否则同一份数据两次取会得到不同指纹 —— 那是假红。）
    """
    rows = conn.execute(
        "SELECT id, ts, direction, kind, text FROM messages ORDER BY id"
    ).fetchall()
    h = hashlib.sha256()
    for r in rows:
        for col in MESSAGES_BODY_COLUMNS:
            h.update(str(r[col]).encode("utf-8", "replace"))
            h.update(b"\x1f")
        h.update(b"\x1e")
    return "%d:%s" % (len(rows), h.hexdigest())


def with_meta_guard(relay, fn):
    """在「只准写 meta」的守卫下执行 `fn(conn)`。**越界立即抛错并回滚。**

    `fn(conn)` 只能通过 `update_message_meta` 里那种 `UPDATE … SET meta = ?` 写库；
    写别的列 / 增删行 → 退出时指纹不一致 → 抛 `RuntimeError` + `rollback()`。

    ⚠️ 守卫看的是**整张表**（不只 `fn` 动的那一行）—— 这是有意的：
       将来谁在别处顺手写了 `messages`，这里也会一起报出来。
    """
    with connect(relay) as conn:
        before = messages_body_signature(conn)
        try:
            out = fn(conn)
        except BaseException:
            conn.rollback()
            raise
        after = messages_body_signature(conn)
        if before != after:
            conn.rollback()
            raise RuntimeError(
                "红线被破坏：有人动了 messages 的正文或行数（只准写 meta 列）。\n"
                f"  before: {before}\n"
                f"  after : {after}\n"
                "  允许的写法只有 `UPDATE messages SET meta = ? WHERE id = ?`，"
                "请改用 schema.update_message_meta()。"
            )
        conn.commit()
        return out


def update_message_meta(relay, msg_id: int, patch: Optional[dict] = None,
                        drop=()) -> bool:
    """**唯一被允许的 `messages` 写入口**：只改 `meta` 列。

        patch 里的键合并进原 meta（`None` 值 = 删掉这个键，见下）
        drop 里列出的键从 meta 里移除

    返回 True = 真的写了一行；False = 没这条消息（**不抛错**，调用方决定怎么处理）。
    原 meta 不是合法 JSON / 不是对象 → 当成 `{}`，但**不丢原文**：
    把它挪进 `meta_raw` 保住，免得"修一下 meta"顺手毁掉别人的数据。
    """
    import json as _json

    def _do(conn):
        row = conn.execute("SELECT meta FROM messages WHERE id = ?",
                           (int(msg_id),)).fetchone()
        if row is None:
            return False
        raw = row["meta"]
        try:
            cur = _json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            cur = {}
        if not isinstance(cur, dict):
            cur = {"meta_raw": raw}
        if isinstance(patch, dict):
            for k, v in patch.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
        for k in (drop or ()):
            cur.pop(k, None)
        conn.execute("UPDATE messages SET meta = ? WHERE id = ?",
                     (_json.dumps(cur, ensure_ascii=False), int(msg_id)))
        return True

    return bool(with_meta_guard(relay, _do))


def read_message_meta(relay, msg_id: int) -> dict:
    """只读：拿一条消息的 meta（解析失败 / 不存在 → `{}`）。"""
    import json as _json

    with connect(relay) as conn:
        row = conn.execute("SELECT meta FROM messages WHERE id = ?",
                           (int(msg_id),)).fetchone()
    if row is None:
        return {}
    try:
        out = _json.loads(row["meta"]) if row["meta"] else {}
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def guard_report(relay) -> dict:
    """只读诊断：当前 `messages` 的指纹 + 行数（给验收 / 线上体检用）。"""
    with connect(relay) as conn:
        sig = messages_body_signature(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    return {
        "messages_rows": n,
        "body_signature": sig,
        "meta_only_write": True,
        "writable_columns": list(META_WRITABLE_COLUMNS),
    }
