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

## 本次只做"能存、能读"

P2 才会实现提炼管道（从对话里抽出 fact / preference / relationship / event）。
现在把落点建好、读写通，P2 直接往上接，不用再动表。

## 🔴 为什么**故意不提供 delete**

房子的 memories 是技术缓存，删它本身没问题。但第一阶段定过一条更宽的红线：

> **不造一颗会痛苦的心。** 记忆是最不该擅自动的东西。

一旦代码里存在 `delete_memory()`，它就会在某天被某个"清理逻辑"顺手调用。
所以这一版**只提供 add / list / touch**，真要清理走人工（直接操作 SQLite）。
这不是洁癖 —— 是让"删记忆"这件事**在代码层面不存在**。

`salience`（重要度）同样：**内部权重，永不展示给 Lily**（跟"想念度数值不做"
那条红线同源）。
"""

import json
import uuid
from datetime import datetime, timezone

from . import schema as _schema   # 包内相对导入（app_ext 是个包）

KINDS = {"fact", "preference", "relationship", "event"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add(relay, kind: str, text: str, source_msg=None,
        salience: float = 0.5, user_id: str = "u_owner") -> dict:
    """写一条记忆。kind 不在枚举里 → 归到 'fact'（不报错，不让调用方炸在日志里）。"""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        kind = "fact"
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty_text"}

    mid = f"m_{uuid.uuid4().hex[:12]}"
    sal = max(0.0, min(1.0, float(salience)))
    with _schema.connect(relay) as conn:
        conn.execute(
            "INSERT INTO memories "
            "(id, user_id, kind, text, source_msg, salience, created, last_used) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mid, user_id, kind, text, source_msg, sal, now_iso(), None),
        )
        conn.commit()
    return {"ok": True, "id": mid, "kind": kind, "salience": sal}


def list_recent(relay, limit: int = 50, user_id: str = "u_owner") -> list:
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY created DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def top(relay, limit: int = 10, user_id: str = "u_owner") -> list:
    """按重要度取前 N 条 —— P2 组装上下文时会用这个喂给模型。"""
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? "
            "ORDER BY salience DESC, created DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


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
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM memories WHERE user_id = ? GROUP BY kind",
            (user_id,),
        ).fetchall()
    by_kind = {r["kind"]: int(r["n"]) for r in rows}
    return {"total": sum(by_kind.values()), "by_kind": by_kind}


def as_extra_for_prompt(relay, limit: int = 10, user_id: str = "u_owner") -> dict:
    """给"组装上下文"用的一小段（P2）。返回可直接 json 塞进 system 消息的结构。

    ⚠️ 这里**不带 salience 数值** —— 内部权重不出现在给模型看的文本里，
       更不出现在给 Lily 看的界面上。
    """
    items = top(relay, limit, user_id)
    return {
        "items": [{"kind": m["kind"], "text": m["text"]} for m in items],
        "count": len(items),
    }


def _unused():  # pragma: no cover
    return json.dumps({})
