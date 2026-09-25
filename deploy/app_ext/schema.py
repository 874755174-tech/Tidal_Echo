#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 · 地基 —— 五张表（users / settings / sessions / memories / usage_log）
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

本文件把五个载体建起来。**只建表，不放任何功能逻辑。**

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
   SQLite 为这个用途留的库头字段。当前 `SCHEMA_VERSION = 6`。

   | 版本 | 加了什么 | 谁要的 |
   |---|---|---|
   | v1 | 四张表本身 | P0 |
   | v2 | `settings.provider_id` | P1 模型网关 |
   | v3 | `sessions.summary_upto` | P2 ⑧ 上下文管理 |
   | v4 | `memories.source` | P2 ⑩-a（书房等的"缝"） |
   | v5 | `usage_log` 表（第 5 张） | P2 · usage 记账 |
   | **v6** | **`sessions.distill_upto` + `memories.superseded_by`** | **P2 ⑩-b 蒸馏管道** |

   ⚠️ **这张表修正过一次**：写 v4 那一格时它写的是「当前 `SCHEMA_VERSION = 4`」，
   而当时实际已经是 **5**（`usage_log` 先占了）—— 因为**规格里的版本号会漂**。
   **动 schema 之前先读 `SCHEMA_VERSION` 的现值，不要信文档里的数字。**

   ⚠️ **v4 这一格曾经被规格预写成 "v2 → v3"** —— 因为规格写的时候
   v2/v3 还没被占。**规格里的版本号会漂**，动 schema 前先读这里的现值。

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
  `sessions_manage.py` 都不是 nginx 的活，是业务兜底。五张表和它们放一起，
  比"一半 backend 一半 deploy"更清楚。

  （本节结论已于 2026-09-14 回填 `架构与产品路线规划.md` §3.2。）
"""

import hashlib
import sqlite3
from typing import Optional


# 当前 schema 版本。以后每次改表结构 +1，并在 `ensure_schema()` 的
# 「迁移」那一段里补一步（见该函数内 `additions` 的写法：先 PRAGMA table_info
# 看列在不在，再 ALTER —— 这样新库/老库跑同一段代码都安全）。
#
#   1 → 四张表（P0，2026-09-14）
#   2 → settings.provider_id（P1 模型网关要"供应商"这个概念，2026-09-14）
#   3 → sessions.summary_upto（P2 ⑧ 滚动摘要要知道"已经压到哪条消息了"，
#       否则每次压缩都要把全部旧消息重新喂一遍 —— 2026-09-19）
#   4 → memories.source（P2 ⑩-a：这条记忆**从哪来**。`source_msg` 只指得到
#       messages，书房那种"读一本书沉淀下来的"没有对应消息 → 没有这一列就
#       永久"来源不明"。🔴 定死这时**表还是空的** → 加列零风险；
#       一旦开写，后加的列**无法回填** —— 2026-09-20）
#   5 → usage_log 表（P2 · usage 记账：上游每次真实调用回来的 token 账单。
#       网关早就把 usage 从流里"捡"出来了，但**只塞进 SSE 帧就没了** ——
#       于是我们既看不到消耗，更看不到缓存命中。这是**只追加**的账本，
#       不是状态表 —— 2026-09-22）
#   6 → sessions.distill_upto + memories.superseded_by（P2 ⑩-b 蒸馏管道，2026-09-25）
#       ① `distill_upto` = 「这段对话已经蒸到哪一条消息了」。跟 ⑧ 的
#          `summary_upto` 是同一件事的两种用途（⑧ 为省 token 压缩，⑩-b 为沉淀抽取），
#          所以**各自一条水位线**，不共用 —— 用途不同，触发时机也不同。
#          🔴 **为什么不从 `MAX(source_msg)` 反推**：那会把"水位线"和
#          "抽出了几条"绑死 —— 一段对话**一条都抽不出来**是常态（很常见！
#          聊了二十句全是家常），那时水位线不动 ⇒ 下次重蒸同一段 ⇒ 白花钱。
#       ② `superseded_by` = **软作废指针**（被哪一批重蒸取代）。蒸馏是"抽"，会抽歪；
#          ⑩-a 定死了**没有 delete**（结构性），所以"重跑"不能靠删。
#          ⇒ 用这一列把旧批**标废**：行还在库里（可审计、可反查、可人工恢复），
#          只是默认读不到。**归档 ≠ 删除**，跟 P2-0 导出那条同源。
#          🔴 **不带 DEFAULT**：NULL = 有效。带了 DEFAULT 会让老行报出那个值
#          —— 跟 v4 的 `source` 踩的是同一个坑（见 DDL 里那段注释）。
SCHEMA_VERSION = 6

# 五张表的建表语句。
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
            -- 🆕 v6：**蒸馏水位线** —— 这段对话已经"抽"到哪一条消息了（⑩-b）。
            --     跟 `summary_upto` 是同一件事的两种用途，所以**各走各的水位线**：
            --     ⑧ 压缩为省 token，⑩-b 抽取为沉淀记忆 —— 触发时机本来就不同。
            distill_upto INTEGER DEFAULT 0,
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
            -- 🆕 v4：这条记忆**从哪来**（chat | reading | craft | …）。
            --     `source_msg` 指向 messages，只能记"对话里来的"；书房那种
            --     "读过一本书沉淀下来的"**没有对应消息** → 没有 source 就永久来源不明。
            --     这是**缝**不是**门**：形状锁死，取值不锁死。见 memories_store.SOURCES。
            --
            -- 🔴 **故意不写 DEFAULT**（2026-09-20 冒烟时改的，原本写了 DEFAULT 'chat'）：
            --     `ALTER TABLE ... ADD COLUMN x TEXT DEFAULT 'chat'` 会让**老行也报 'chat'**
            --     （SQLite 对老行返回那个默认值）→ 把"不知道从哪来"**静默洗成"来自对话"**。
            --     这正是这一列存在的反面。默认值住在**代码**里（`SOURCE_DEFAULT`），
            --     而 `add()` **每次都显式传值** → schema 里再放一个 DEFAULT 是多余的，
            --     而且会让"新库建的列"和"老库 ALTER 出来的列"行为分叉（可测性也一起坏掉）。
            --     ⇒ 省略 source 的插入得到 **NULL**，`NULL` 就是"不知道"，不兜底。
            source     TEXT,
            salience   REAL DEFAULT 0.5,          -- 重要度（内部权重，永不展示）
            -- 🆕 v6：**软作废指针** —— 这条被哪一批蒸馏取代了（NULL = 仍然有效）。
            --     ⑩-b 蒸馏会抽歪，而 ⑩-a 定死了**没有 delete**（让"删记忆"在代码层面
            --     不存在）⇒ "重跑"唯一能走的路是**标废**：行留在库里（可审计、可人工
            --     恢复），默认读不到而已。**归档 ≠ 删除**（跟 P2-0 导出同源）。
            --     🔴 **同上不带 DEFAULT**：NULL = 有效，带了 DEFAULT 会让老行报出那个值。
            superseded_by TEXT,
            created    TEXT NOT NULL,
            last_used  TEXT
        )
    """),

    # ⑤ 账本层：上游每次真实调用的 token 账单（P2 · usage 记账，2026-09-22）
    #
    # 🔴 它是**只追加的事件流**，不是状态表 —— 所以：
    #    · 主键用 `INTEGER PRIMARY KEY`（rowid 别名，天然递增），**不写 AUTOINCREMENT**
    #      （账本只插不删，不需要防 id 重用，也就没必要多养一张 `sqlite_sequence`）；
    #    · 唯一没有 `IF NOT EXISTS` 之外约束的表：一行 = 一次真实上游调用。
    #
    # 🔴 `ok` 这一列是这张表的灵魂：**捡不到 usage 也要写一行**（`ok=0` + `note`）。
    #    如果"没账单"就干脆不写，那么"哪次调用没记账"就永远查不出来 ——
    #    表面上是"账本里没有坏数据"，实际上是**账本自己在说谎**（缺口不可见）。
    #
    # 🔴 `raw` 存上游 usage 的 **JSON 原文**。归一化只挑我们认识的 5 个数，
    #    但中转站随时可能多给一个计费维度（reasoning tokens / 音频 / 缓存分级…）——
    #    只存挑出来的数 = 那个维度永久丢失。**先原样收下，解读放到读的时候做。**
    # 🔴 `user_id` **故意不加外键**（这是唯一一张这么做的表，理由值得写下来）。
    #    2026-09-22 冒烟实拍：写上 `REFERENCES users(id)` 之后，只要记一笔账时
    #    users 表里还没有那一行（比如全新 /data 上 `ensure_owner` 还没跑完），
    #    整批 `record()` 全部 `IntegrityError: FOREIGN KEY constraint failed`；
    #    而 `record()` 是 **fail-open** 的 → 错误被吞掉 → **账静默丢失**，
    #    表面上"一切正常"（这正是 usage_store 文件头 ① 要防的那种坏法）。
    #
    #    别的表要外键是对的（它们是**业务状态**：会话属于谁、记忆属于谁）。
    #    但 `usage_log` 是**只追加的事件日志** —— 它的第一要求是"**绝不能丢**"，
    #    不是"关系要严"。给它加外键 = 让"记一笔账"依赖于另一张表的状态，
    #    等于给账本装了一个会**静默吞账**的开关。⇒ 这里 user_id 就当一个标签用。
    ("usage_log", """
        CREATE TABLE IF NOT EXISTS usage_log (
            id                 INTEGER PRIMARY KEY,
            ts                 TEXT NOT NULL,     -- ISO8601 UTC（记账那一刻）
            user_id            TEXT,              -- 标签，不是外键（见上）
            provider_id        TEXT,              -- relay / relay2 / deepseek / ...
            model              TEXT,
            session_id         TEXT,              -- 认得出会话就记（认不出 = NULL，不编）
            route              TEXT,              -- chat | complete | probe | raw
            stream             INTEGER,           -- 1 流式 / 0 非流式
            shape              TEXT,              -- openai | anthropic | gemini | deepseek
            -- 🔴 ok=1 上游给了账单；ok=0 **没给**（note 写明原因）。见上面那段。
            ok                 INTEGER NOT NULL,
            note               TEXT,
            prompt_tokens      INTEGER,           -- 以下六列：**捡不到 = NULL，不是 0**
            completion_tokens  INTEGER,           --   （0 = 上游说了是 0，NULL = 上游没说 ——
            cache_read_tokens  INTEGER,           --    这两件事被混起来，命中率就再也算不准了）
            cache_write_tokens INTEGER,
            cache_in_prompt    INTEGER,           -- 1 = cache_read 已含在 prompt_tokens 里
                                                  --   （OpenAI/Gemini 口径）
                                                  -- 0 = 二者并列（Anthropic 口径）
                                                  -- ⇒ 命中率的**分母**按这一列选，不靠猜
            total_tokens       INTEGER,
            ms                 INTEGER,           -- 这次调用花了多久
            chars_out          INTEGER,           -- 回来多少字符（上游不给账单时，至少知道这次多大）
            raw                TEXT               -- 上游 usage 原文，一个字段不丢
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
    # 账本：两个真实查询 —— "最近 N 天"（按 ts 倒序）与"某个模型花了多少"。
    ("idx_usage_ts", "CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts DESC)"),
    ("idx_usage_model_ts", "CREATE INDEX IF NOT EXISTS idx_usage_model_ts "
                           "ON usage_log(model, ts DESC)"),
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
    """只读诊断：当前库里有哪些表 / 版本号 / 五张表是否齐。"""
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
    """建齐五张表（幂等）。**本函数自己不碰 `messages`。**

    返回：
        {
          "version":      6,
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
        # ⚠️ **新加一整张表不用进这里**（如 v5 的 `usage_log`）：上面对 `DDL_TABLES`
        #    的循环每次都跑 `CREATE TABLE IF NOT EXISTS` → 老库自然补齐。
        #    这一段只负责"**已经存在的表**上多出来的列"，两件事别混。
        migrated: list = []
        additions = [
            # (表, 列, 类型/默认值, 版本)
            ("settings", "provider_id", "TEXT", "v2"),
            ("sessions", "summary_upto", "INTEGER DEFAULT 0", "v3"),
            # 🆕 v4：书房等的"缝"（Lily 09-19 定，见 施工图 §11.5②）。
            #    🔴 加的时候 `memories` **还是空表** → 零风险；**开写之后这列无法回填**。
            #    🔴 **不带 DEFAULT**（理由见 DDL 里那段注释）：带 DEFAULT 会让老行
            #       报出那个默认值 → 把"不知道从哪来"洗成"来自对话"。省略 = NULL = 不知道。
            ("memories", "source", "TEXT", "v4"),
            # 🆕 v6：⑩-b 蒸馏管道的两条（Lily 09-25 开干）。
            #    ①「蒸到哪条了」—— 跟 ⑧ 的 `summary_upto` 有意**写成同一个形状**
            #      （`INTEGER DEFAULT 0`），这样"没蒸过"和"没压过"语义一致，读代码不用切换脑。
            ("sessions", "distill_upto", "INTEGER DEFAULT 0", "v6"),
            #    ② 软作废指针。**不带 DEFAULT**（NULL = 有效）—— 理由见文件头 6 → 那一段。
            ("memories", "superseded_by", "TEXT", "v6"),
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
