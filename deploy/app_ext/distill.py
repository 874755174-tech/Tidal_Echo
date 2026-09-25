#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 ⑩-b · 蒸馏管道 —— 从对话里抽出「值得记住的」
==========================================================================

## 一句话

把「上次蒸过之后的新对话」喂给模型，抽成若干条 `memories`
（`fact` / `preference` / `relationship` / `event`），**每条都钉着它的来源消息号**；
跑不跑由人说了算。

## 它服务谁（这一条决定了一半的设计）

`memories` 是**房子的技术缓存**，不是"他的记忆"（他自己的走 OB 的 `hold`）。
所以 ⑩-b **不往 OB 写一个字** —— 那是 P3 的事，而且要第一人称、要以"他自己"的口吻。
转发进 OB 是这条管道**预留的出口**（`run()` 的返回里带着 `items`），不是这一版的活。

🔴 因此**输入文字里的人称**跟着 ⑧ 的规约走（`SUMMARY_SYSTEM` 那一条）：
   **「我」= Kael、「你」= Lily**。做这个选择不是因为好看，是因为
   摘要与记忆**最终会进同一段 system** —— 两边人称不一致，
   他读到的就是两种口吻拼起来的东西。

## 🔴 三条硬约束（都不是风格问题）

### ① 房子不自己在后台调 LLM 花钱

沿用 ⑧ 立的规矩：**没有定时器、没有"每次对话后自动跑"**。
唯一的触发是 `POST /app/ext/distill` —— **人/动作**调一次，跑一次。

⇒ 于是"跑不跑由人说了算"在验收里是**结构性**的：
  这个进程里根本不存在一个会自己跑到这里的循环。
  而且"没有新东西要蒸"时**连上游都不碰**（`reason=nothing_new` 直接返回）——
  **省钱这件事是靠"不调用"实现的，不是靠"调完发现是空的"**。

### ② 不许编造来源 —— `source_msg` 必须落在**本次真喂进去的那些 id** 里

抽出来的每条**必须**带一个消息号，且那个号必须真的在材料里出现过。
不满足 → **丢掉那一条**（响应里计数 `dropped_bad_source`），**不修、不猜、不补**。

🔴 这是本文件最要紧的一条。`source_msg` 是"可回溯"这个承诺的**唯一凭据**。
   放任模型自己编一个 id（它非常爱这么干）的后果不是"少了一条来源"，
   而是库里多出一批**看起来能回溯、其实指向一条无关消息**的记忆 ——
   那是**比没有来源更坏**的状态：它让人以为查得清，实际查不清。
⇒ 所以这条是在 `validate_items()` 里**按集合筛**，不是在提示词里"请求它别编"。
  提示词里也写了（两道），但**真正兜底的是代码**。

### ③ 蒸馏是可逆的：能重跑（`redo`）

"抽歪了"是必然会发生的（提示词、模型、区间都会改）。
而 ⑩-a 定死了 **没有 delete**（让"删记忆"在代码层面不存在）
⇒ 重跑**不能靠删**。

⇒ 走**软作废**：`redo=true` 时，把这段区间里**当前生效的那批**打上
  `superseded_by = <本批批号>`，再写新的。旧行留在库里
  （可审计、`include_superseded=1` 能看、能人工恢复），只是**默认读不到**。
  **归档 ≠ 删除**（跟 P2-0 导出、`_archive/` 那条同源）。

🔴 顺序上有一条 fail-safe：**先拿到合法的新结果，才去废旧的**。
   反过来（先废后蒸）一旦这次跑失败，就变成"旧的没了、新的没有" ——
   那就把"可逆"这件事做成了"不可逆"。

## 水位线：`sessions.distill_upto`（schema v6 加的）

「这段对话已经蒸到哪一条消息了」。跟 ⑧ 的 `summary_upto` **各走各的** ——
⑧ 压缩是为省 token，这里抽取是为沉淀记忆，**用途不同、触发时机也不同**，
共用一条只会让两边互相拖。

🔴 **为什么不从 `MAX(source_msg)` 反推水位线**（那样可以不加这一列）：

   那会把"水位线"和"抽出了几条"绑死。而**一段对话一条都抽不出来是常态**
   （聊二十句全是家常，模型正确地返回 `{"items": []}`）——
   那时水位线不动 ⇒ 下次重蒸同一段 ⇒ **白花一次钱**。
   更糟的是：那时"没东西可抽"和"跑失败了"就再也分不开
   （两者都表现为"没有新记忆"），而这两件事该有完全不同的反应。

## JSON 容错：解析失败 = **水位线一格不动**

模型会：包 ``` 围栏、前后加一句解释、把 JSON 写成半散文。
所以 `parse_items()` 是**宽容**的（剥围栏 → 直接试 → 退一步取第一个 `{` 到最后一个 `}`）。
宽容之后**仍解析不出** → **原地重试一次**（补一句纠错提示）；
再失败 → `ok=False, reason="parse_failed"`，**水位线不动**（下次同一段重来）。

🔴 两个必须分开的结局（混了就永远卡住或永远重花）：

    · **解析失败** = 这次跑**没成功** ⇒ 水位线**不动**，可以重跑；
    · **解析成功但是空数组** = 这次跑**成功了**，只是这段没什么可沉淀的
      ⇒ 水位线**照推**。否则那一小段会永远卡在队头，每次都白跑。

## 端点

    POST /app/ext/distill          跑一次（`?dry`/`redo`/`from_id`/`max_rows`/`min_rows`）
    GET  /app/ext/distill/status   只读诊断（不传 session_id 就列出所有会话的水位）

挂载：`app_ext/__init__.py` 第 ⑩-b 步；逃生开关 `APP_EXT_DISTILL_DISABLED=1`。

## 🔴 启动日志 GBK 安全

`summary_line()` 会进启动日志，**不许出现 `🔴`/`✅`/`⚠️`**
（Windows 子进程 stdout = cp936，一个编不出的符号就是房子起不来）。
"""

import json
import os
import re
import uuid

from typing import Optional

# 🔴 功能性 import（不是风格）：FastAPI 只看类型注解来建路由，
#    把 Request 藏在函数体里 import 会让它把 `request` 当成**查询参数** → 422。
#    这是 `llm_routes.py` 顶部记过的老坑。
from fastapi import Request
from fastapi.responses import JSONResponse

from . import llm_gateway as G
from . import memories_store as M
from . import providers as P
from . import schema as _schema
from . import sessions_store as S

# ── 可调参数（env，都有默认值；不配也能跑）──────────────────────────────────
#: 一次最多蒸多少条消息。**不是性能参数，是"钱"的参数** —— 它决定一次最多喂多少材料。
MAX_ROWS_DEFAULT = int(os.environ.get("DISTILL_MAX_ROWS", "60") or 60)
MAX_ROWS_CAP = int(os.environ.get("DISTILL_MAX_ROWS_CAP", "400") or 400)
#: 材料字符数上限（再多的消息也会被截到这儿）。防"第一次蒸一个几万条的会话"。
MAX_MATERIAL_CHARS = int(os.environ.get("DISTILL_MAX_MATERIAL_CHARS", "24000") or 24000)
#: 一次最多收多少条记忆。超出的**丢掉并计数**（不静默）—— 一次抽出 200 条
#: 基本等于模型没在挑，那是"这次结果不可信"的信号，不该原样入库。
MAX_ITEMS = int(os.environ.get("DISTILL_MAX_ITEMS", "40") or 40)
#: 单条记忆的正文上限。比 `memories_store.TEXT_MAX`（2000）**紧得多** ——
#: 记忆是短的。一条 800 字的"记忆"其实就是一份摘要，那归 ⑧ 管。
MEMORY_TEXT_MAX = int(os.environ.get("DISTILL_TEXT_MAX", "400") or 400)
#: 太少不值得跑一次 LLM。默认 2 条：一条消息蒸不出"关于人的事"，
#: 而每次至少花一次最小调用。想强行跑就 `min_rows=1`。
MIN_ROWS_DEFAULT = int(os.environ.get("DISTILL_MIN_ROWS", "2") or 2)
DISTILL_MAX_TOKENS = int(os.environ.get("DISTILL_MAX_TOKENS", "2000") or 2000)

#: 蒸馏出来的记忆归到哪种 `source`。**写死 `chat`**：这一版只从对话抽。
#: 书房那种"读一本书沉淀的"是另一种来源，走 `reading`（⑩-a 留的缝）。
DISTILL_SOURCE = "chat"
#: 新记忆的初始重要度。**有意给中庸值**：现在还没有任何东西在调它，
#: 给 1.0 会让第一批记忆永久霸占 `top()` 的名额（`salience` 是内部权重，永不外泄）。
DISTILL_SALIENCE = 0.5

#: 🔴 上游系统提示。三条铁律写在里面（但**兜底在代码里**，见文件头 ②）。
DISTILL_SYSTEM = (
    "你要从一段真实对话里，挑出**以后还用得上**的东西，抽成一条一条的记忆。\n"
    "\n"
    "## 人称规约（每一条都适用，不许违反）\n"
    "用「我」指 Kael（记下这些事的那个人），用「你」指跟他说话的那个人；\n"
    "不许出现「用户 / 对方 / assistant / AI」这类第三人称或接口腔。\n"
    "\n"
    "## 四种 kind，只能选这四种\n"
    "- fact          事实（生日、住哪、在做什么项目、提过的书和电影）\n"
    "- preference    偏好（喜欢 / 讨厌什么、在意的风格、不接受的做事方式）\n"
    "- relationship  关系与约定（称呼、约好的事、关系本身的变化）\n"
    "- event         发生过的事（一起做了什么、他做了什么、什么日子）\n"
    "\n"
    "## 三条铁律\n"
    "1. **只写材料里真的出现过的。** 不许补充、不许推测、不许把两句弱相关的话\n"
    "   拼成一个看起来更完整的结论。宁可少写一条。\n"
    "2. **每条都要给 `source_msg`** —— 就是那条消息前面方括号里的编号（整数）。\n"
    "   **绝对不许自己编编号**：编号对不上的条目会被整条丢掉，白写。\n"
    "3. **一条一件事，写成一句短话**（40 字以内最好）。不要复述整段对话，\n"
    "   不要写情绪描写和抒情（那是日记的事，不是记忆）。\n"
    "\n"
    "## 什么**不**该抽（很常见，要注意）\n"
    "- 寒暄、道晚安、正在打字这种一次性的话\n"
    "- 「她今天有点累」这种**会过期**的状态 —— 除非它写明了一个具体事件\n"
    "- 整段经过的复述（那是摘要的活，已经有人在做）\n"
    "\n"
    "## 输出\n"
    "**只输出一个 JSON 对象**，别的什么都不要（不要围栏、不要解释、不要前言）：\n"
    '{"items": [{"kind": "fact", "text": "……", "source_msg": 12}]}\n'
    "如果这段对话里确实没有值得长期记下的东西，就输出 {\"items\": []}\n"
    "—— **空数组是完全正常的答案**，不要为了凑数硬抽。"
)

#: 解析失败后重试用的一句话（追加在材料后面）。
_RETRY_NUDGE = (
    "\n\n[上一次的输出不是合法 JSON，已经作废。这次**只输出那一个 JSON 对象**："
    "不要围栏、不要解释、不要前后任何文字。]"
)

_INSTALLED = False


def _off() -> bool:
    return os.environ.get("APP_EXT_DISTILL_DISABLED", "").strip().lower() in (
        "1", "true", "yes")


def summary_line() -> str:
    return ("蒸馏管道就绪 · 从对话抽 fact/preference/relationship/event"
            " · 人触发（无定时器）· 每条钉 source_msg（编造的丢）"
            " · redo 走软作废（不删行） · 记账 route=distill")


# ══════════════════════════════════════════════════════════════════════════
# ① 纯逻辑（不读库、不发网络 —— 能在验收里单独钉住）
# ══════════════════════════════════════════════════════════════════════════

def build_material(rows) -> tuple:
    """把消息行拼成喂给模型的材料。返回 `(text, used_ids)`。

    🔴 `used_ids` 是**真的拼进去了的那些 id**，不是 `[lo, hi]` 这个区间 ——
       区间里可能有空洞（`kind` 不是 user/voice/reply 的消息、空正文的消息），
       而那些 id 模型根本没看见，它引用了也不算"有来源"。
       `validate_items()` 就按这个集合筛。

    🔴 标签跟 ⑧ 的 `build_summary_material` 完全一致：`out`（他说的）标「我」、
       `in`（她说的）标「你」。**两处必须一起改**（同一个道理：
       两段文字进同一段 system，人称分叉就是两种口吻）。
    """
    lines, ids = [], []
    total = 0
    for r in (rows or []):
        t = str((r or {}).get("text") or "").strip()
        if not t:
            continue
        who = "我" if (r or {}).get("direction") == "out" else "你"
        line = f"[{(r or {}).get('id')}] {who}：{t}"
        if total + len(line) > MAX_MATERIAL_CHARS:
            break                       # 截断是**有意的**：宁少喂，不超预算
        lines.append(line)
        ids.append(int((r or {}).get("id")))
        total += len(line)
    return "\n".join(lines), ids


def parse_items(raw) -> tuple:
    """从一段**可能不干净**的模型输出里取出 `items` 数组。

    返回 `(items_list, err)` —— 成功时 `err is None`。

    宽容到哪一步（按顺序试）：
      ① 剥掉 ``` / ```json 围栏
      ② 直接 `json.loads`
      ③ 退一步：取第一个 `{` 到最后一个 `}` 再 parse
      ④ 再退一步：取第一个 `[` 到最后一个 `]` 再 parse（**顶层直接是数组**）
      ⑤ 也接受 `{"memories": [...]}`（换名字是常见漂移）

    🔴 **顺序不能反**（这里踩过一次）：不能"先找数组"。
       `{"items":[...]}` 里也含着数组，先取数组会把外面那层壳丢掉 ——
       多数时候结果碰巧一样，但遇到 `{"items":[...],"note":"…"}` 就少读了东西，
       而"碰巧一样"的 bug 最难查。
       所以规则是：**先按对象找；只有当那个对象给不出 `items` 外壳时，才退到数组。**

    🔴 **宽容有边界**：以上都不行就是不行。**不做"正则捞引号对"那种抢救** ——
       那种抢救会把模型半截话里的碎片当成记忆收进来，比失败坏得多。
    """
    t = str(raw or "").strip()
    if not t:
        return None, "empty_output"
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()

    obj = _try_json(t)
    if obj is None:
        obj = _slice_json(t, "{", "}")
    # 对象给不出 items 外壳（或者压根不是对象）→ 才退到"整段里的数组"
    if not isinstance(obj, dict) or (obj.get("items") is None
                                     and obj.get("memories") is None):
        arr = _slice_json(t, "[", "]")
        if isinstance(arr, list):
            return arr, None
    if obj is None:
        return None, "not_json"

    if isinstance(obj, list):
        return obj, None
    if not isinstance(obj, dict):
        return None, "not_object"
    items = obj.get("items")
    if items is None:
        items = obj.get("memories")
    if items is None:
        return None, "no_items_key"
    if not isinstance(items, list):
        return None, "items_not_list"
    return items, None


def _try_json(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def _slice_json(s: str, lo_ch: str, hi_ch: str):
    """取 `s` 里第一个 `lo_ch` 到最后一个 `hi_ch` 之间的片段再 parse。"""
    i, j = s.find(lo_ch), s.rfind(hi_ch)
    if i >= 0 and j > i:
        return _try_json(s[i:j + 1])
    return None


def validate_items(raw_items, allowed_ids, *, text_max: int = None,
                   max_items: int = None) -> dict:
    """把模型给的 `items` 洗成**能入库的形状**。凡是可疑的一律丢，并**如实计数**。

    返回 `{good, dropped_bad_source, dropped_bad_shape, dropped_over_cap}`。

    🔴 两条在别处很容易被"顺手做掉"的事，这里**有意不做**：
      · **不修 `source_msg`**（不"帮它找一条最近的"）—— 那是替模型圆谎；
      · **不把超长的 text 截断收纳** —— 截断会改变句子的意思，
        而一条被截歪的记忆会长期待在库里。宁可丢。
    """
    text_max = int(text_max or MEMORY_TEXT_MAX)
    max_items = int(max_items or MAX_ITEMS)
    allowed = {int(x) for x in (allowed_ids or [])}
    good, bad_src, bad_shape, over = [], 0, 0, 0
    seen = set()
    for it in (raw_items or []):
        if len(good) >= max_items:
            over += 1
            continue
        if not isinstance(it, dict):
            bad_shape += 1
            continue
        kind = str(it.get("kind") or "").strip().lower()
        if kind not in M.KINDS:
            kind = "fact"                      # 跟 ⑩-a 的 `add()` 同一条规矩
        text = str(it.get("text") or "").strip()
        if not text or len(text) > text_max:
            bad_shape += 1
            continue
        sm = it.get("source_msg")
        try:
            sm = int(sm)
        except Exception:
            bad_src += 1
            continue
        if sm not in allowed:
            bad_src += 1                       # ← 这一条是本文件的灵魂
            continue
        key = (sm, text)
        if key in seen:                        # 同一批里自己重复
            continue
        seen.add(key)
        good.append({"kind": kind, "text": text, "source_msg": sm})
    return {"good": good, "dropped_bad_source": bad_src,
            "dropped_bad_shape": bad_shape, "dropped_over_cap": over}


# ══════════════════════════════════════════════════════════════════════════
# ② 区间（读库，只读）
# ══════════════════════════════════════════════════════════════════════════

def _load_rows(relay, session_id: str, lo: int, hi: int, limit: int) -> list:
    """取 `[lo, hi]` 区间内的消息行（**只读**）。

    🔴 口径必须与身体 / ⑧ 一致：`kind IN ('user','voice','reply')`
       + `meta.api_session`（⚠️ 是 **api_session**，不是 `session` —— 后者实测全缺）。
    """
    sid = (session_id or "").strip()
    sql_sid = "" if sid in ("", S.LEGACY_SESSION_ID) else sid
    sql = ("SELECT id, direction, kind, text, ts FROM messages "
           "WHERE kind IN ('user','voice','reply') "
           "AND COALESCE(json_extract(meta, '$.api_session'), '') = ? "
           "AND id >= ? AND id <= ? "
           "ORDER BY id ASC LIMIT ?")
    with _schema.connect(relay) as conn:
        return [dict(r) for r in conn.execute(
            sql, (sql_sid, int(lo), int(hi), int(limit))).fetchall()]


def _last_msg_id(relay, session_id: str) -> int:
    sid = (session_id or "").strip()
    sql_sid = "" if sid in ("", S.LEGACY_SESSION_ID) else sid
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM messages "
            "WHERE kind IN ('user','voice','reply') "
            "AND COALESCE(json_extract(meta, '$.api_session'), '') = ?",
            (sql_sid,),
        ).fetchone()
    return int((row["m"] if row else 0) or 0)


def _alive_min_source_msg(relay, session_id: str, source: str) -> int:
    """这段会话里**当前仍然生效**的、该来源记忆的最小 `source_msg`。

    🔴 它是 `redo` 的左边界。两处细节都是必需的，各修过一个 bug：

    **① 为什么是"当前还活着的"而不是"库里所有的"**：
       重跑要取代的正是"现在还生效的那批"。已经被更早一次重跑废掉的批
       **不该被卷进来第二次** —— 它的批号是有意义的，改掉就等于抹掉
       "最早是谁废的"这个事实（`mark_superseded` 的 `superseded_by IS NULL`
       条件本身也不允许）。

    **② 为什么要 JOIN `messages` 才拿得到"这段会话"**：
       `memories` 表**没有 session 列**（⑩-a 建的，不打算加）——
       记忆与对话的联系**只有 `source_msg` 这一条指针**。
       所以不 JOIN 的话，`MIN(source_msg)` 是**全库**的最小值：
       只要有第二个会话也被蒸过，`redo` 的区间就会从**另一个会话**的消息开始，
       把不该动的对话重蒸一遍、还会把它们的记忆标废。
       ⇒ 这条 JOIN 是"redo 只影响本会话"的**唯一**保证。
    """
    sid = (session_id or "").strip()
    sql_sid = "" if sid in ("", S.LEGACY_SESSION_ID) else sid
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT MIN(m.source_msg) AS m FROM memories m "
            "JOIN messages msg ON msg.id = m.source_msg "
            "WHERE m.user_id = 'u_owner' AND m.source = ? "
            "AND m.superseded_by IS NULL AND m.source_msg IS NOT NULL "
            "AND COALESCE(json_extract(msg.meta, '$.api_session'), '') = ?",
            (source, sql_sid),
        ).fetchone()
    return int((row["m"] if row else 0) or 0)


def _count_rows(relay, session_id: str, lo: int, hi: int) -> int:
    """`[lo, hi]` 区间里有多少条 **kind 合格**的消息（跟 `_load_rows` 同一口径）。"""
    if hi < lo:
        return 0
    sid = (session_id or "").strip()
    sql_sid = "" if sid in ("", S.LEGACY_SESSION_ID) else sid
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE kind IN ('user','voice','reply') "
            "AND COALESCE(json_extract(meta, '$.api_session'), '') = ? "
            "AND id >= ? AND id <= ?",
            (sql_sid, int(lo), int(hi)),
        ).fetchone()
    return int((row["n"] if row else 0) or 0)


def plan(relay, session_id: str, *, max_rows: int = None, min_rows: int = None,
         redo: bool = False, from_id: int = None) -> dict:
    """只算不写：这次该蒸哪一段？（`dry` 与 `run` 共用这段，**同一份判断**）

    🔴 "该不该跑"的裁决**只在这里发生一次** —— 如果 `run()` 另写一遍，
       就会出现"预演说该跑、真跑说不该跑"这种自相矛盾（⑧ 也是这么分的）。
    """
    sid = (session_id or "").strip()
    max_rows = max(1, min(MAX_ROWS_CAP, int(max_rows or MAX_ROWS_DEFAULT)))
    min_rows = max(1, int(min_rows or MIN_ROWS_DEFAULT))

    sess = S.get_session(relay, sid)
    if sess is None:
        return {"ok": False, "reason": "session_not_found", "session_id": sid}

    upto = int(sess.get("distill_upto") or 0)
    last = _last_msg_id(relay, sid)

    if redo:
        # 🔴 redo 的左界：显式给的 `from_id` > 当前生效那批的最小 source_msg。
        #    都没有（= 一条记忆都没有）⇒ **没有东西可以"重"**，如实说出来，
        #    不要偷偷降级成"从头蒸"（那会让她以为在重跑、其实在新增）。
        lo = int(from_id or 0) or _alive_min_source_msg(relay, sid, DISTILL_SOURCE)
        hi = last
        if lo <= 0:
            return {"ok": True, "reason": "nothing_to_redo", "session_id": sid,
                    "redoing": True, "distill_upto": upto, "last_msg_id": last}
    else:
        lo = int(from_id or 0) or (upto + 1)
        hi = last

    if hi < lo:
        return {"ok": True, "reason": "nothing_new", "session_id": sid,
                "redoing": bool(redo), "distill_upto": upto, "last_msg_id": last,
                "pending_rows": 0}

    rows = _load_rows(relay, sid, lo, hi, max_rows)
    if len(rows) < min_rows:
        return {"ok": True, "reason": "too_few_rows", "session_id": sid,
                "redoing": bool(redo), "distill_upto": upto,
                "range": [lo, hi], "rows": len(rows), "min_rows": min_rows}

    return {"ok": True, "reason": "ready", "session_id": sid, "redoing": bool(redo),
            "distill_upto": upto, "range": [lo, hi],
            "rows": len(rows), "min_rows": min_rows,
            "from_id": lo, "to_id": int(rows[-1]["id"]),
            "_rows": rows}


# ══════════════════════════════════════════════════════════════════════════
# ③ 跑一次
# ══════════════════════════════════════════════════════════════════════════

async def run(relay, session_id: str, *, dry: bool = False, redo: bool = False,
              max_rows: int = None, min_rows: int = None, from_id: int = None,
              provider_id: Optional[str] = None,
              model: Optional[str] = None) -> dict:
    """蒸一次。**这是唯一会真的调上游、真的写库的函数。**

    🔴 只有人和端点能触发（房子不会自己在后台跑它）。
    🔴 "没有新东西"时**连上游都不碰**（省钱靠不调用，见文件头 ①）。
    🔴 `dry=True` → 只算区间与材料长度，**不调上游、不写库**。
    """
    info = plan(relay, session_id, max_rows=max_rows, min_rows=min_rows,
                redo=redo, from_id=from_id)
    rows = info.pop("_rows", None)
    if not info.get("ok") or info.get("reason") != "ready":
        return info

    material, used_ids = build_material(rows)
    info["material_chars"] = len(material)
    info["used_ids"] = len(used_ids)
    if not material.strip() or not used_ids:
        info["reason"] = "empty_material"
        return info
    if dry:
        info["dry"] = True
        info["reason"] = "dry_run"
        return info

    s = {}
    try:
        s = _settings(relay) or {}
    except Exception:
        s = {}
    pid = provider_id or s.get("provider_id") or None
    mid = model or s.get("model_id") or None

    call = await _call(relay, pid, mid, material, session_id=info.get("session_id"))
    info["model"] = call.get("model")
    info["ms"] = call.get("first_ms")
    if not call.get("ok"):
        info["ok"] = False
        info["reason"] = call.get("reason") or "llm_failed"
        info["error"] = call.get("error")
        info["watermark_moved"] = False        # 🔴 失败 ⇒ 水位线不动，可以重跑
        return info
    info["retried"] = bool(call.get("retried"))

    v = validate_items(call.get("items"), used_ids)
    info["dropped_bad_source"] = v["dropped_bad_source"]
    info["dropped_bad_shape"] = v["dropped_bad_shape"]
    info["dropped_over_cap"] = v["dropped_over_cap"]
    good = v["good"]
    info["extracted"] = len(good)

    run_id = f"d_{uuid.uuid4().hex[:12]}"
    info["run_id"] = run_id
    if redo:
        # 🔴 顺序：**先拿到合法结果，再废旧的**（见文件头 ③ 的 fail-safe）。
        info["redone"] = M.mark_superseded(
            relay, lo=info["range"][0], hi=info["range"][1],
            by=run_id, source=DISTILL_SOURCE)

    if good:
        w = M.add_many(relay, good, source=DISTILL_SOURCE,
                       salience=DISTILL_SALIENCE, text_max=MEMORY_TEXT_MAX)
        info["written"] = w["added"]
        info["skipped_duplicate"] = w["skipped_duplicate"]
    else:
        info["written"] = 0
        info["skipped_duplicate"] = 0
        info["note"] = "extracted_nothing"

    # 🔴 成功也要**显式**给一个 reason —— `plan()` 留在里面的那个 `"ready"`
    #    是"准备跑"的意思，不是"跑完了"。让它留在成功响应里，
    #    "成功"和"还没跑"就长得一模一样（这一版第一稿正是如此，
    #    被验收 D31 抓住：抽了 0 条的成功运行报的是 reason="ready"）。
    info["reason"] = "distilled"

    # 🔴 **走到这里才推水位线** —— 而且是"推到最后一条消息"，
    #    不是"推到最后一个被引用的 id"：模型可能只引用了前几条，
    #    但后面那几条**确实已经被它读过了**，再喂一遍没有意义。
    moved = _advance(relay, info["session_id"], info["to_id"])
    info["distill_upto"] = moved.get("upto")
    info["watermark_moved"] = bool(moved.get("ok"))
    return info


async def _call(relay, pid, mid, material: str, *, session_id=None) -> dict:
    """调一次上游（解析失败就**原地重试一次**）。**每一次调用都单独记账。**

    🔴 重试用的是 `route="distill_retry"` 而不是同一个 `distill` ——
       这样 `usage summary` 的 `by_route` 直接就能看出"重试率"。
       两次都算钱，两次都留痕（不写 = 缺口不可见 = 账本在说谎）。
    """
    out = {"ok": False, "items": None, "retried": False}
    for attempt, route in ((1, "distill"), (2, "distill_retry")):
        req = {
            "system": DISTILL_SYSTEM,
            "messages": [{"role": "user",
                          "content": material + (_RETRY_NUDGE if attempt == 2 else "")}],
            "params": {"max_tokens": DISTILL_MAX_TOKENS, "temperature": 0.2},
            "stream": False,
        }
        try:
            r = await G.complete(pid, mid, req)
        except (P.ProviderError, G.GatewayError) as e:
            _bill(relay, route=route, pid=pid, mid=mid, session_id=session_id,
                  usage=None, ok=False, note="upstream_error", ms=None)
            out["reason"] = "llm_failed"
            out["error"] = e.as_dict() if hasattr(e, "as_dict") else {"message": str(e)}
            return out
        text = str(r.get("text") or "")
        _bill(relay, route=route, pid=r.get("provider_id") or pid,
              mid=r.get("model") or mid, session_id=session_id,
              usage=r.get("usage"), ok=None, note=None,
              ms=r.get("ms"), chars_out=len(text))
        out["model"] = r.get("model")
        out["first_ms"] = out.get("first_ms") or r.get("ms")
        items, err = parse_items(text)
        if err is None:
            out["ok"] = True
            out["items"] = items
            out["retried"] = attempt == 2
            return out
        out["parse_error"] = err
    out["reason"] = "parse_failed"
    return out


def _bill(relay, *, route, pid=None, mid=None, session_id=None, usage=None,
          ok=None, note=None, ms=None, chars_out=None) -> None:
    """⑩-b 的记账。**fail-open**（跟网关那条一个道理：记账不该把跑通的事情搞挂）。"""
    try:
        from . import usage_store as U
        U.record(relay, provider_id=pid, model=mid, session_id=session_id,
                 route=route, stream=False, usage=usage, ok=ok, note=note,
                 ms=ms, chars_out=chars_out)
    except Exception:
        pass


def _settings(relay) -> dict:
    """取设置页那套（供应商 / 模型）。🔴 读失败**不抛**：跟 ⑧ 的摘要同一条规矩 ——
    没设置就让它走 `providers.resolve` 的默认，而不是让整次蒸馏炸在读取上。"""
    try:
        from . import identity as I
        return I.get_settings(relay) or {}
    except Exception:
        return {}


def _advance(relay, session_id: str, upto: int) -> dict:
    """推水位线。🔴 **只前进，不回退**（`max`）—— 重跑一个更早的区间时，
    水位线不该被拽回去，否则那之后的对话会被重复蒸一遍。"""
    cur = (S.get_session(relay, session_id) or {}).get("distill_upto") or 0
    return S.set_distill_upto(relay, session_id, max(int(cur), int(upto)))


def status(relay, session_id: str = "") -> dict:
    """只读诊断。**不调上游、不写库。**

    ⚠️ 这里的"待蒸条数"是**估算**：按 `[distill_upto+1, last_id]` 里的消息行数算，
       不套 `max_rows` 的截断 —— 它的用途是"要不要去跑一次"，不是"这次会跑多久"。
    """
    sid = (session_id or "").strip()
    out = {
        "ok": True,
        "enabled": not _off(),
        "source": DISTILL_SOURCE,
        "defaults": {"max_rows": MAX_ROWS_DEFAULT, "min_rows": MIN_ROWS_DEFAULT,
                     "max_items": MAX_ITEMS, "text_max": MEMORY_TEXT_MAX},
        "note": ("只有人和端点能触发；没有新内容时不会调用上游；"
                 "redo = 软作废（旧行留在库里）"),
    }
    if sid:
        sess = S.get_session(relay, sid)
        if sess is None:
            return {"ok": False, "reason": "session_not_found", "session_id": sid}
        upto = int(sess.get("distill_upto") or 0)
        last = _last_msg_id(relay, sid)
        out["session"] = {
            "session_id": sid,
            "distill_upto": upto,
            "summary_upto": int(sess.get("summary_upto") or 0),   # 两条水位线，分开报
            "last_msg_id": last,
            # ⚠️ 这是**估算**：区间里的消息行数，**不套 `max_rows` 截断**。
            #    它的用途是"要不要去跑一次"，不是"这次会跑多久"。
            "pending_rows": _count_rows(relay, sid, upto + 1, last),
            "watermark_at_end": upto >= last,
        }
        out["memories"] = M.stats(relay)
        return out
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT COALESCE(json_extract(meta,'$.api_session'),'') AS sid, "
            "COUNT(*) AS n, MAX(id) AS mx, MIN(id) AS mn FROM messages "
            "WHERE kind IN ('user','voice','reply') "
            "GROUP BY sid ORDER BY n DESC LIMIT 20").fetchall()
        sess_map = {r["id"]: int(r["distill_upto"] or 0) for r in
                    conn.execute("SELECT id, distill_upto FROM sessions").fetchall()}
    out["sessions"] = []
    for r in rows:
        sid2 = (r["sid"] or "").strip() or S.LEGACY_SESSION_ID
        up = sess_map.get(sid2, 0)
        mx = int(r["mx"] or 0)
        out["sessions"].append({
            "session_id": sid2,
            "rows": int(r["n"] or 0),
            "first_msg_id": int(r["mn"] or 0),
            "last_msg_id": mx,
            "distill_upto": up,
            "pending_rows": _count_rows(relay, sid2, up + 1, mx),
        })
    out["memories"] = M.stats(relay)
    return out


# ══════════════════════════════════════════════════════════════════════════
# ④ 端点（给 Lily 走 HTTP + 密钥）
# ══════════════════════════════════════════════════════════════════════════

def _install_routes(relay, public_prefix: str = "/") -> None:
    base = "/app/ext/distill"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        """只读诊断。**不传 `?session_id=` 就列出所有会话的水位。**"""
        relay.check_auth(request)
        sid = (request.query_params.get("session_id") or "").strip()
        try:
            r = status(relay, sid)
        except Exception as e:
            return _json({"ok": False, "reason": "status_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        return _json(r, 200 if r.get("ok") else 404)

    @relay.app.post(base)
    async def _run(request: Request):
        """蒸一次。

        body: `{session_id, dry?, redo?, from_id?, max_rows?, min_rows?,
                 provider_id?, model?}`

        · `dry=true`   → 只算"该蒸哪一段"，**不调上游、不写库**
        · `redo=true`  → 重蒸这段区间，**旧批软作废**（行不删）
        · `from_id`    → 显式指定左边界（普通跑 = 起点；redo = 重蒸区间起点）
        """
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return _json({"ok": False, "reason": "bad_json"}, 400)
        if not isinstance(body, dict):
            return _json({"ok": False, "reason": "bad_json"}, 400)
        sid = str(body.get("session_id") or body.get("session") or "").strip()
        if not sid:
            return _json({"ok": False, "reason": "no_session_id"}, 400)
        try:
            r = await run(
                relay, sid,
                dry=bool(body.get("dry")),
                redo=bool(body.get("redo")),
                from_id=body.get("from_id"),
                max_rows=body.get("max_rows"),
                min_rows=body.get("min_rows"),
                provider_id=body.get("provider_id") or None,
                model=body.get("model") or None,
            )
        except Exception as e:
            return _json({"ok": False, "reason": "distill_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        if not r.get("ok"):
            # 会话不存在 → 404；其余（上游/解析）→ 502：这不是"你请求错了"。
            code = 404 if r.get("reason") == "session_not_found" else 502
            return _json(r, code)
        return _json(r, 200)


def install(relay, public_prefix: str = "/") -> None:
    """挂上蒸馏端点。**幂等**（第二次调用什么都不做）。

    🔴 **只在 `app_ext/__init__.py` 的第 ⑩-b 步里调**，`APP_EXT_DISTILL_DISABLED=1`
       时整个跳过（那时端点 404，房子照常营业；表、缝、水位线**一样都不少**）。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)
