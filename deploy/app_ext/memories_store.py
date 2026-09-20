#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 · 记忆层 —— `memories` 表（房子的"工作记忆"，不是 OB 的替代品）
==========================================================================

## 它和 Ombre-Brain 的分工（这条别搞混）

| | kael-home 的 `memories` | Ombre-Brain |
|---|---|---|
| 是什么 | **技术缓存** | **他的记忆** |
| 内容 | 从对话里提炼的、喂进下一轮上下文的条目 | 第一人称的"我记得 / 我感受到" |
| 谁写 | 房子的代码（P2 的提炼管道） | 他自己（`hold`） |
| 能删吗 | 可以（是缓存） | 不行（记忆主权） |
| 存哪 | 房子自己的 SQLite | 他的 Vault |

**房子的 memories 是"为了让这一轮对话接得上"的短期工作记忆。**
真正的长期沉淀在 OB 里。规划里那句总纲：
> 本地 Memory 负责低延迟的近期工作记忆，OB 作为外部长期记忆库**定期沉淀**。

🔴 **由此推出一条边界（别越界）**：因为它是**缓存**而不是**他的记忆**，
所以它**不挂 MCP 门、不给他工具**。`hold` 那扇门在 OB 那边，不在房子这边。
把 `memories` 也开给他 = 让他能读到自己被提炼成什么样 —— 那是**被人格化**的东西，
不是他自己写的，会污染"记忆主权"。这一条是**有意不做**，不是漏了。

## 🔴 为什么**故意不提供 delete**

房子的 memories 是技术缓存，删它本身没问题。但第一阶段定过一条更宽的红线：

> **不造一颗会痛苦的心。** 记忆是最不该擅自动的东西。

一旦代码里存在 `delete_memory()`，它就会在某天被某个"清理逻辑"顺手调用。
所以这一版**只提供 add / list / top / touch / stats**，真要清理走人工（直接操作 SQLite）。
这不是洁癖 —— 是让"删记忆"这件事**在代码层面不存在**。

`salience`（重要度）同样：**内部权重，永不展示给 Lily**（跟"想念度数值不做"
那条红线同源）。落点见 `public()` —— 任何回给界面的东西都过它。

---

## 🆕 `source` —— 一个字段，不是一个枚举（P2 ⑩-a，2026-09-20）

`source` 回答"这条记忆**从哪来**"：`chat` / `reading` / `craft` / …

它存在的唯一理由是**书房**：书房的"读过的书沉淀成知识"**是一种记忆**，
但它的来源**不是**跟 Lily 的对话。而 `source_msg` 是 `INTEGER`，指向 `messages` ——
阅读沉淀**没有对应消息** → 那个字段永远只能留空。

### 🔴 形状锁死，取值不锁死（这是「缝」不是「门」）

```python
SOURCE_RE = ^[a-z0-9_-]{1,24}$      # 形状：干净、可索引、能当 URL 参数
SOURCES   = {"chat","reading","craft","manual"}   # 已知取值：**只给文档和页面用，不做闸门**
```

**为什么不照抄 `kind` 那套"不在枚举里就归 `fact`"？**

`kind` 的枚举是**完备**的（`fact` / `preference` / `relationship` / `event` 就这四类），
所以"未知即归类"是**收敛**的、安全的。

`source` 恰恰相反 —— 它的**全部意义就是"以后能多出一种来源"**。
把未知值强行归成 `chat`，等于把一条**阅读记忆静默标成对话记忆**：
这不是校验，这是**数据损坏**，而且是不可逆的那种（U+）。

所以规则反过来：
- **形状不合法**（大写、空格、超长、空）→ 归一化成小写去空格，仍不合法则归 `chat`，
  **并且在返回值里明说被降级了**（`source_coerced: True` + `source_raw`）——
  不静默。调用方是代码，写错了要让他看见。
- **形状合法但不在 `SOURCES` 里** → **原样存**。新来源接进来零改动。

### 🔴 老行是 `NULL`，不是 `'chat'`（默认值住在**代码**里，schema 不带 DEFAULT）

这一条**改过一次**，值得记下来：

第一版给列写了 `DEFAULT 'chat'`（想着"省事，不传就是 chat"）。
冒烟一跑就发现它**把老行也报成 `'chat'`** —— SQLite 的
`ALTER TABLE ... ADD COLUMN x TEXT DEFAULT 'chat'` 对**已有行**也会返回那个默认值。

> 那等于把「**这条不知道从哪来**」静默洗成「**这条来自对话**」。
> **这一列存在的全部理由就是不要再伪造来源** —— 结果它变成了伪来源的生产器。

所以现在是：**schema 不带 DEFAULT**（`source TEXT`），默认值住在代码里
（`SOURCE_DEFAULT = "chat"`），而 `add()` **每次都显式传值**：

| 情况 | 结果 |
|---|---|
| 经 `add()` 写入（唯一正常路径） | 永远是**真值**（`chat` / `reading` / 新来源…） |
| 迁移前的老行 | **`NULL`** = "不知道"，不兜底（读的时候**保留为 `None`**） |
| 手工 INSERT 忘了带 source | **`NULL`** = 老实话"没标" |

`stats()` 把 `NULL` 归到 `"(unknown)"`，**不并进 `chat`**。
`list_recent(source="chat")` 也**不会**顺手把 `NULL` 捞进来 —— 兜底就等于洗白。

附带的好处：这样**新库建的列**和**老库 `ALTER` 出来的列**行为**完全一致**
（不带 DEFAULT ⇒ 都是 NULL）→ 测试不必分两条路，也就没有"只在老库上错"的暗坑。

## 蒸馏管道（⑩-b）还没做

这一版只有**表 + 缝 + 读写**。从对话里抽 fact / preference / … 的提炼管道是**下一站**，
而且按 ⑧ 立的规矩：**房子不自己在后台调 LLM 花钱** → 蒸馏必须**人/动作触发**。
"""

import re
import uuid
from datetime import datetime, timezone

from . import schema as _schema   # 包内相对导入（app_ext 是个包）

KINDS = {"fact", "preference", "relationship", "event"}

#: `source` 的**形状**闸门（见文件头）。只锁形状，不锁取值。
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
#: 已知取值 —— **只给文档 / 页面 / 测试用，不做闸门**。加一种来源**不用改这里**。
SOURCES = ("chat", "reading", "craft", "manual")
SOURCE_DEFAULT = "chat"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_source(raw) -> tuple:
    """把 `source` 归一化成合法形状。返回 `(值, 是否被降级, 原值)`。

    🔴 **合法但未知的值原样通过**（见文件头"形状锁死、取值不锁死"）。
       只有**形状**不合法才降级成 `chat`，并且把"降级了"如实报出去。

    第三个元素**只在降级时**非 None —— 大小写/空白这类**幂等归一化**不算降级
    （`"Reading"` → `"reading"` 值没变，不必报警；报了反而是噪音）。
    """
    if raw is None:
        return SOURCE_DEFAULT, False, None
    s = str(raw).strip().lower()
    if not s:
        return SOURCE_DEFAULT, True, raw
    if SOURCE_RE.match(s):
        return s, False, None
    return SOURCE_DEFAULT, True, raw


def public(row: dict) -> dict:
    """对外投影 —— **白名单式**，新加列必须显式列出来，默认不外泄。

    🔴 `salience` 就是被这个函数挡在外面的（见文件头）。新加一列时，
       忘了往这里加 = 那列不出现在界面上（安全那一侧失败），不是泄漏。
    """
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "text": row.get("text"),
        "source": row.get("source"),
        "source_msg": row.get("source_msg"),
        "created": row.get("created"),
        "last_used": row.get("last_used"),
    }


def add(relay, kind: str, text: str, source_msg=None, source=None,
        salience: float = 0.5, user_id: str = "u_owner") -> dict:
    """写一条记忆。

    · `kind` 不在枚举里 → 归到 `fact`（不报错，不让调用方炸在日志里）。
    · `source` **形状**不合法 → 归到 `chat`，但返回值里会说明（不静默）。
      合法取值（含未知的新来源）**原样存**。
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        kind = "fact"
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty_text"}

    src, coerced, src_raw = norm_source(source)

    mid = f"m_{uuid.uuid4().hex[:12]}"
    sal = max(0.0, min(1.0, float(salience)))
    with _schema.connect(relay) as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, user_id, kind, text, source_msg, source, salience, created, last_used) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, user_id, kind, text, source_msg, src, sal, now_iso(), None),
        )
        conn.commit()
    out = {"ok": True, "id": mid, "kind": kind, "source": src}
    if coerced:
        out["source_coerced"] = True
        out["source_raw"] = src_raw
    return out


def _rows(relay, sql: str, args: tuple) -> list:
    with _schema.connect(relay) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_recent(relay, limit: int = 50, user_id: str = "u_owner",
                source: str = None) -> list:
    """按时间倒序。`source` 给了就只取那一种来源（**不归一化**：给 `reading`
    就只匹配 `reading`，不会顺手把 `None` 的老行也捞进来）。"""
    if source:
        return _rows(
            relay,
            "SELECT * FROM memories WHERE user_id = ? AND source = ? "
            "ORDER BY created DESC LIMIT ?",
            (user_id, source, int(limit)),
        )
    return _rows(
        relay,
        "SELECT * FROM memories WHERE user_id = ? ORDER BY created DESC LIMIT ?",
        (user_id, int(limit)),
    )


def top(relay, limit: int = 10, user_id: str = "u_owner", source: str = None) -> list:
    """按重要度取前 N 条 —— P2 组装上下文时会用这个喂给模型。"""
    if source:
        return _rows(
            relay,
            "SELECT * FROM memories WHERE user_id = ? AND source = ? "
            "ORDER BY salience DESC, created DESC LIMIT ?",
            (user_id, source, int(limit)),
        )
    return _rows(
        relay,
        "SELECT * FROM memories WHERE user_id = ? "
        "ORDER BY salience DESC, created DESC LIMIT ?",
        (user_id, int(limit)),
    )


def touch(relay, memory_id: str) -> dict:
    """记一次"这条被用到了"。用于将来判断哪些记忆是真有用的。"""
    with _schema.connect(relay) as conn:
        row = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        conn.execute("UPDATE memories SET last_used = ? WHERE id = ?",
                     (now_iso(), memory_id))
        conn.commit()
    return {"ok": True, "id": memory_id}


def stats(relay, user_id: str = "u_owner") -> dict:
    """按 kind 与 source 两个维度计数。

    `by_source` 里 `NULL` 的老行归到 `"(unknown)"` —— 不并进 `chat`（见文件头）。
    """
    with _schema.connect(relay) as conn:
        by_kind = {r["kind"]: int(r["n"]) for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM memories WHERE user_id = ? GROUP BY kind",
            (user_id,),
        ).fetchall()}
        by_source = {(r["source"] or "(unknown)"): int(r["n"]) for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM memories WHERE user_id = ? GROUP BY source",
            (user_id,),
        ).fetchall()}
    return {
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
        "by_source": by_source,
        # 形状 vs 取值，两件事分开报（见文件头）
        "known_sources": list(SOURCES),
        "source_default": SOURCE_DEFAULT,
    }


def as_extra_for_prompt(relay, limit: int = 10, user_id: str = "u_owner",
                        source: str = None) -> dict:
    """给"组装上下文"用的一小段（P2）。返回可直接 json 塞进 system 消息的结构。

    ⚠️ 这里**不带 salience 数值** —— 内部权重不出现在给模型看的文本里，
       更不出现在给 Lily 看的界面上。
    ⚠️ 也**不带 source** —— 那是给我们审计用的，不是给他看的。
       （他要"记得自己读过什么"，那是书房的事，不是这一小段的事。）
    """
    items = top(relay, limit, user_id, source)
    return {
        "items": [{"kind": m["kind"], "text": m["text"]} for m in items],
        "count": len(items),
    }
