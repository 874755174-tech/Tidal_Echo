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
所以这一版**只提供 add / add_many / list / top / touch / stats / mark_superseded**，
真要清理走人工（直接操作 SQLite）。
这不是洁癖 —— 是让"删记忆"这件事**在代码层面不存在**。

🆕 **v6 补一句别误读的**：`mark_superseded()` **不是 delete 的后门**。
它只把 `superseded_by` 写上一个批号，**行一行不删**（`UPDATE`，不是 `DELETE`）——
所以上面那句"删记忆在代码层面不存在"仍然成立，一个字没松。

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

## 蒸馏管道（⑩-b）—— ✅ 已落（2026-09-25，跟在 `distill.py`）

这一版（⑩-a）只有**表 + 缝 + 读写**。从对话里抽 fact / preference / … 的提炼管道
在后面那一站，而且按 ⑧ 立的规矩：**房子不自己在后台调 LLM 花钱** →
蒸馏是**人/动作触发**（`POST /app/ext/distill`），不做定时后台跑。

⑩-b 给这一层补了两样东西，都在本文件里：
  · `add_many()` —— 批量写，**幂等**（同一条不写两遍）
  · `mark_superseded()` —— **软作废**（重蒸时把旧批标废，**一行不删**）

⇒ 于是"默认读什么"这件事多了一条判据：`superseded_by IS NULL`（`_ALIVE`）。
  `list_recent` / `top` / `as_extra_for_prompt` **全都默认只看有效的** ——
  🔴 其中 `top()` 最要紧：被标废的记忆**绝不能喂进上下文**，
  否则"重跑"等于没重跑（旧的那批还在影响他说话）。
"""

import re
import uuid
from datetime import datetime, timezone

from . import schema as _schema   # 包内相对导入（app_ext 是个包）

KINDS = {"fact", "preference", "relationship", "event"}

#: 一条记忆的正文长度上限（**记忆是短的**）。防的是"某个调用方把一整篇文档当记忆塞进来"。
#: `memory.py` 那层 HTTP 有它自己的同名常量（值相同，口径相同）—— 两边都留是因为
#: 它们挡的是**不同入口**：那边挡手滑的 HTTP 调用，这边挡代码里的批量写入。
TEXT_MAX = 2000

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

    🆕 v6：`superseded_by` **故意加进来** —— 它是"这条被哪一批取代了"的
       **审计指针**，不是内部权重。要能看见才谈得上"可审计"（跟 `source_msg`
       同一类东西）。🔴 它跟 `salience` 是两种性质，别类比。
    """
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "text": row.get("text"),
        "source": row.get("source"),
        "source_msg": row.get("source_msg"),
        "superseded_by": row.get("superseded_by"),
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


def add_many(relay, items, *, source=None, user_id: str = "u_owner",
             salience: float = 0.5, text_max: int = TEXT_MAX) -> dict:
    """批量写 —— **⑩-b 蒸馏管道的唯一入口**。幂等：同一批重复跑不会长出重复行。

    `items` = `[{"kind": …, "text": …, "source_msg": …}, …]`。

    ## 🔴 为什么要幂等（这一条决定的不是性能，是"记忆的清晰度"）

    "跑不跑由人说了算"意味着**同一个人会点很多次**（试提示词、试区间、试模型）。
    如果每次都新增一份，库里就会攒下 N 份几乎一样的记忆 ——
    而 `top()` 是按 salience 取前 N 喂给模型的 ⇒ **同一件事占掉好几个名额**，
    "他记得什么"会随时间越来越糊。

    去重键 = **`(source_msg, text)` 在同一 `source` 下**（同一段话抽出的同一句话）。
    🔴 **有意不掺 `kind`**：`kind` 是归类，把 `fact` 改判成 `preference` 不该
       让这条重新长一条出来（那就成了"改个标签 = 多一条记忆"）。
    🔴 **有意不掺 `salience`**：它是权重，不是身份。

    ⚠️ **去重只解决"同一条被写两遍"，不解决"抽歪了"** —— 后者归软作废
       （`mark_superseded`）。两件事别混：前者是"手抖点了两次"，
       后者是"这批本身就不该留"。

    🔴 本函数**只有 INSERT，没有任何清理分支** —— 不提供 `delete` 的旁路。
    """
    src, coerced, src_raw = norm_source(source)
    src_msg_key = []
    for it in (items or []):
        sm = it.get("source_msg")
        if sm is not None:
            src_msg_key.append(int(sm))

    # 先取"该来源下、本次涉及的这些消息号"上已有的文本 —— 不漏查也不全表扫
    existing = set()
    if src_msg_key:
        marks = ",".join("?" * len(set(src_msg_key)))
        with _schema.connect(relay) as conn:
            rows = conn.execute(
                "SELECT source_msg, text FROM memories "
                f"WHERE user_id = ? AND source = ? AND source_msg IN ({marks})",
                tuple([user_id, src] + sorted(set(src_msg_key))),
            ).fetchall()
        existing = {(int(r["source_msg"]), r["text"]) for r in rows}

    added, dup, bad = [], 0, 0
    for it in (items or []):
        kind = str(it.get("kind") or "").strip().lower()
        if kind not in KINDS:
            kind = "fact"
        text = str(it.get("text") or "").strip()
        if not text or len(text) > int(text_max):
            bad += 1
            continue
        sm = it.get("source_msg")
        try:
            sm = int(sm) if sm is not None else None
        except Exception:
            bad += 1
            continue
        if sm is not None and (sm, text) in existing:
            dup += 1
            continue
        mid = f"m_{uuid.uuid4().hex[:12]}"
        sal = max(0.0, min(1.0, float(salience)))
        with _schema.connect(relay) as conn:
            conn.execute(
                "INSERT INTO memories "
                "(id, user_id, kind, text, source_msg, source, salience, created, last_used) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, user_id, kind, text, sm, src, sal, now_iso(), None),
            )
            conn.commit()
        if sm is not None:
            existing.add((sm, text))       # 同一批里重复的项也去重
        added.append({"id": mid, "kind": kind, "text": text, "source_msg": sm})

    out = {"ok": True, "source": src, "added": len(added), "items": added,
           "skipped_duplicate": dup, "skipped_invalid": bad}
    if coerced:
        out["source_coerced"] = True
        out["source_raw"] = src_raw
    return out


def mark_superseded(relay, *, lo: int, hi: int, by: str,
                    source: str = None, user_id: str = "u_owner") -> dict:
    """把 `[lo, hi]` 这段消息号里、这一来源的记忆**标废**（`superseded_by = by`）。

    🔴 **它不是 delete。** 行原样留在库里 —— 只是默认读不到（`_ALIVE`）。
       想回看：`list_recent(..., include_superseded=True)`；想人工恢复：直接把
       `superseded_by` 改回 NULL。**归档 ≠ 删除**（与 P2-0 导出同源）。

    🔴 **UPDATE 一定带 WHERE**（定点更新，不可能"忘了条件就标废全表"）。
    ⚠️ `superseded_by IS NULL` 也在 WHERE 里 ⇒ 已经标废的**不会被改写成新批号**，
       保留"最早是谁废的"这个事实；重复标废是幂等的（第二次 marked=0）。

    `source=None` = 不按来源过滤（**默认就是 None，但蒸馏调用时一定显式传 'chat'**：
       不传的话会把书房那类同区间的记忆一起卷进来 —— 那是别的来源，不该被对话蒸馏废掉）。
    """
    where = ["user_id = ?", "source_msg IS NOT NULL",
             "source_msg >= ?", "source_msg <= ?", "superseded_by IS NULL"]
    # 🔴 参数顺序必须**逐字对应 SQL 里 `?` 的出现顺序**：
    #    `SET superseded_by = ?` 在 `WHERE` **之前** —— 所以 `by` 排第一个。
    #    （第一版把 `by` 写在最后，结果 UPDATE 把 `by` 塞进了 `user_id`、
    #      把 10 塞进了 `superseded_by` ⇒ 一条没匹配上、**静默返回 marked=0**。
    #      这种"静默 0 行"是这里最危险的失败形态：看着像"没有可标的"。）
    args = [by, user_id, int(lo), int(hi)]
    if source:
        where.append("source = ?")
        args.append(source)
    with _schema.connect(relay) as conn:
        cur = conn.execute(
            "UPDATE memories SET superseded_by = ? WHERE " + " AND ".join(where),
            tuple(args),
        )
        n = int(cur.rowcount or 0)
        conn.commit()
    return {"ok": True, "marked": n, "by": by, "range": [int(lo), int(hi)],
            "source": source}


def _rows(relay, sql: str, args: tuple) -> list:
    with _schema.connect(relay) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


#: 🆕 v6：**"这条还有效"** 的判据。`superseded_by IS NULL` = 没被取代过 = 有效。
#:
#: ⚠️ **v4 之前的老行也是 NULL** —— 它们天然算有效，不需要任何兜底。
#:    这一列的字面意思就是"被谁取代了"，没被取代就是有效 —— 语义与数据**同形**，
#:    所以"忘了给老行补值"这件事在这里**不可能发生**（对比 `source` 那一列，
#:    它必须区分"不知道"和"来自对话"，所以那边不能带 DEFAULT）。
_ALIVE = "superseded_by IS NULL"


def _select_sql(*, user_id, source, include_superseded, order, limit) -> tuple:
    """拼 `SELECT * FROM memories` 的 WHERE/ARGS —— `list_recent` 与 `top` 共用。

    🔴 两个查询**必须共用这一段**：将来再加一种过滤条件时，只改一处的机会
       只有这一次；分头写就是"两个接口对'有效'的定义悄悄分叉"，
       而这种分叉的表现是"界面看得见、喂模型看不见"（或反过来）—— 很难查。

    参数顺序**与 SQL 里的 `?` 顺序严格一一对应**：`user_id` → where 各条 → `limit`。
    """
    where, extra = [], []
    if not include_superseded:
        where.append(_ALIVE)
    if source:
        where.append("source = ?")
        extra.append(source)
    sql = ("SELECT * FROM memories WHERE user_id = ?"
           + "".join(" AND " + w for w in where)
           + f" ORDER BY {order} LIMIT ?")
    return sql, tuple([user_id] + extra + [int(limit)])


def list_recent(relay, limit: int = 50, user_id: str = "u_owner",
                source: str = None, include_superseded: bool = False) -> list:
    """按时间倒序。`source` 给了就只取那一种来源（**不归一化**：给 `reading`
    就只匹配 `reading`，不会顺手把 `None` 的老行也捞进来）。

    🆕 `include_superseded=True` → 连**被标废的**一起给（**审计用**）。
       默认不给 —— 被取代的那批读出来只会让"他记得什么"看起来重复且自相矛盾。
    """
    sql, args = _select_sql(user_id=user_id, source=source,
                            include_superseded=include_superseded,
                            order="created DESC", limit=limit)
    return _rows(relay, sql, args)


def top(relay, limit: int = 10, user_id: str = "u_owner", source: str = None,
        include_superseded: bool = False) -> list:
    """按重要度取前 N 条 —— P2 组装上下文时会用这个喂给模型。

    🔴 默认**只取有效的**，这一条比 `list_recent` 更硬：被标废的记忆
       **绝不能喂进上下文** —— 否则"重跑"就等于没重跑（旧的那批还在影响他说话）。
    """
    sql, args = _select_sql(user_id=user_id, source=source,
                            include_superseded=include_superseded,
                            order="salience DESC, created DESC", limit=limit)
    return _rows(relay, sql, args)


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

    🆕 v6：`total` 仍是**全部行数**（含被标废的），另外单列三个数：
      · `alive`      —— 默认读得到的（`superseded_by IS NULL`）
      · `superseded` —— 被重蒸取代掉的
      🔴 **不给"总数减去 alive 就等于 superseded"这种偷懒写法留位置**：
         `total = alive + superseded` 现在恒成立，但将来若多出第三种状态，
         这条等式会静默错掉。所以三个数**各查各的**。
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
        alive = int(conn.execute(
            f"SELECT COUNT(*) AS n FROM memories WHERE user_id = ? AND {_ALIVE}",
            (user_id,),
        ).fetchone()["n"])
        sup = int(conn.execute(
            "SELECT COUNT(*) AS n FROM memories "
            "WHERE user_id = ? AND superseded_by IS NOT NULL",
            (user_id,),
        ).fetchone()["n"])
    return {
        "total": sum(by_kind.values()),
        "alive": alive,
        "superseded": sup,
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
