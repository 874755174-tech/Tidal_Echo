#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 ⑪ · 自主活动带回上下文（「第三层」）—— 读 + 注入
==========================================================================

## 它解决什么：他读得到事件，读不到温度

`自主活动接进聊天.md` §5 把这件事拆成三层，前两层是"给你看的"，第三层是
"**给他自己知道的**"：

    ① 独白原文     他对着自己说的话        → 他的**房间**（书房，只读，原文不搬）
    ② 足迹卡片     他醒来干了什么（轻量）  → 聊天里的一张卡（`kind="activity"`）
    ③ 带回去的     同一份足迹**腌进上下文**  → **本文件**

Lily 2026-09-21 原话（这是本层存在的理由）：

    「我建前后端**要的就是自主醒来的可视化体现在上下文里**，让他跟我说话的时候，
      不要觉得需要拉工具才知道另一个自己干了什么，而是**上下文就能让他知道**」

⇒ 所以卡片和注入**不是两件事**，是**同一份数据的两次投递**：
   同一个 `activity` 行 → 前端渲染成卡片（给人看）
                       → 本层拼成一条 system（给他读）

## 🔴 缝在哪：又是那个"身体每次说话必经的关口"

    examples/api_loop.py  build_messages()  [system PERSONA] + 热区 + 当前这条
    examples/              是红线目录（一个字符都不能改）

所以房子只能做**出口那一跳**：`llm_routes._do_chat/_do_complete` 里，
在 ⑧（`context.apply`）之后再加一条 system。**和 ⑧ 是同一台机器，同一个位置**，
只是插的东西不同、MARK 不同（两条互不影响、可各自单独关掉）。

插完的顺序（`normalize_request` 会把所有 system 合成一条）：

    system = PERSONA（我是谁） + 摘要（我们以前说过什么） + **足迹（我最近做过什么）**
    messages = **与没注入时逐字节相同**（只插不删，热区仍归身体）

## 🔴 四条纪律

1. **fail-open。** 任何异常 / 任何不确定 → 原样放行，一个字段都不改。
   这条比 ⑧ 更要紧一点：⑧ 出问题只是"少一段摘要"，本层出问题是"说不了话"。
2. **只插不删。** 不动任何已有消息、不动顺序（沿用 ⑧ 的纪律与 `insert_index`）。
3. **认不出会话也照样注入（常驻）。** 复用 `context.last_user_text` 只做
   "这轮是用户在说话"这个门；**不再**拿 `session_of` 反查会话来切窗口
   （他只有一个，行迹是同一段人生，不该被"她在哪个会话"切成一段段）。
4. 🔴 **确定性：本层一次 LLM 都不调、一个字都不写。**
   注入的文本是**已落库的行**逐字拼出来的 —— 所以**不可能编造他没做过的事**。
   这正对上「不许编造经历」那条铁律（§5.4「A 类永不由模型改写」）。

## 带哪几段：最近 `ACTIVITY_MAX_ITEMS` 段（常驻，滚动更新）

不引入任何新状态（不加水位线、不加游标），只用库里已有的东西：

    items = 库中 kind='activity' 的行，按 id 倒序取最近 `ITEMS_MAX` 段
            （`recent()` → `load_window(0, 0, limit)`）

    ⇒ 每轮说话都带上这最近 N 段；新行迹来了顶掉最旧的那段。

**为什么改掉旧窗口**：旧规则「她上次开口之后 → 这次开口之前」有个反直觉的坑——
她第一句没问、第二句再问，窗口就空了，他"眨眼就忘"（Lily 2026-09-30 亲口指出）。
"我最近做过什么"该是**常驻事实**，不是被她说话节奏切成的一次性快照。
代价是每轮多带 ~N 段摘要（一段约 190 字，3 段 < 600 字，远低于 system 上限），
这是"记得"，不是"复读"（过渡方案；终态 = 行迹进 OB、聊天时自然浮现，P3 判据③）。

## 数据形状（写侧 = 身体，读侧 = 本文件）

写侧**只有一条路**：身体 POST `/channel/out`，`type="activity"`
（`backend/app.py:659`，红线目录，一个字符不改）：

    {"type":"activity",
     "text":"下午 14:30 · 他待了 9 分钟",          ← 卡片标题（身体写，房子不替他想词）
     "activity":{                                   ← 整块装进 messages.meta
        "started":"2026-09-21T14:30:00+08:00",
        "ended":  "2026-09-21T14:39:00+08:00",
        "state":  "completed",                     ← completed|quiet|failed|suspended
        "actions":[{"at":"14:33","text":"翻了翻 Galatea 的 3 个新帖"}],
        "footprint":"河边的风比上次凉了"            ← 可选：B 类，他自己留的话
     }}

🔴 **房子不新增第二条写入口**。`/channel/out` 已经给了两件白捡的东西：
   ① 落库（`save_message("out", "activity", …)`）② **SSE 实时推给网页**（卡片当场出现）。

## 🔴 形状锁死、取值不锁死（与 ⑩-a 的 `source` 同一条规矩）

`state` / `source` 这类取值**原样存原样用**（未知值不动它 —— 那就是"缝"）；
但**形状**（是不是 ISO 时刻、是不是 `[a-z0-9_-]`）在**读**的时候校验：
不合法就**丢**，绝不替它编一个默认值（编了就是"伪造来源"那个 bug 的翻版）。

## 端点（全部只读）

    GET  /app/ext/activity           最近几段（`?limit=`）—— 已归一化
                                     🆕 `&before=<id>` = 往回翻一页（**游标**，不是偏移量）
    GET  /app/ext/activity/tides     🆕 行迹页**首页**（语义明确：永远是"最近一页"）
    GET  /app/ext/activity/status    开关 / 最近一次注入 / 计数
    POST /app/ext/activity/preview   给一个 OpenAI 风格 body → **只算不插**，看会带回去什么

### 🆕 为什么翻页用「游标」而不是 `?offset=`（2026-10-06 · Tides 行迹页）

`load_window(relay, after_id, before_id, limit)` **本来就是窗口语义**（`_SELECT` 里有
`id > ? AND id < ?`）—— 注入侧一直在用，只是没往外露。所以 `before` 只是把已有的上界
参数接到端点上，不是新机制。

**为什么不用 `offset`**：他每醒一次就多一行 activity，`OFFSET 20` 的意思是
"从我这条线往下数 20 行"—— 她往下翻一次，中间他就醒了一次，于是**同一段会被跳过**。
游标记的是"我看到第几条了"，翻页期间新增的行**留在上面那页**，不会从她眼皮底下溜走。

🔴 **游标不推进就是到底了**：返回条数 < `limit` ⇒ 没有更早的了。
前端据此收手，**并且不显示"还剩多少条"** —— 那是差值、那是债（红线六）。

挂载：`app_ext/__init__.py` 第 ⑪ 步；逃生开关 `APP_EXT_ACTIVITY_DISABLED=1`
（关掉 = 不注入、端点不挂；**卡片照常显示** —— 卡片是原版链路，不归本层管）。

## 🔴 启动日志 GBK 安全

`summary_line()` 会进启动日志，**不许出现 `🔴`/`✅`/`⚠️`**
（Windows 子进程 stdout = cp936，一个编不出的符号就是房子起不来）。
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

# ── 可调参数（env，都有默认值）──────────────────────────────────────────────

#: 注入块正文字符上限。它最终进 system，而网关对 system 有硬上限
#: （`providers.MAX_SYSTEM_CHARS` = 40000）—— 见 `apply()` 里"会撑爆就跳过"那段。
TEXT_MAX = int(os.environ.get("ACTIVITY_TEXT_MAX_CHARS", "700") or 700)

#: 最多带回去几段活动（"她上次开口之后"的窗口里，取最近的 N 段）。
ITEMS_MAX = max(1, int(os.environ.get("ACTIVITY_MAX_ITEMS", "3") or 3))

#: 一段活动最多几行动作、每行多少字。
ACTIONS_MAX = 12
LINE_MAX = 90

#: 块首那行说明。🔴 **这也是去重标记**（同一次请求被插两次时靠它拦住），
#:    别改字面量、别拿它当业务语义用（同 ⑧ 的 `MARK`）。
MARK = "你最近自己的经历"

#: 取活动行的 SQL（**只读**）。写侧不在这里 —— 见文件头"数据形状"。
_SELECT = ("SELECT id, ts, direction, kind, text, meta FROM messages "
           "WHERE kind = ? AND id > ? AND id < ? ORDER BY id DESC LIMIT ?")

#: 形状校验（取值不校验 —— 那是"缝"）
_RE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
_RE_AT = re.compile(r"^\d{1,2}:\d{2}$")
_RE_STATE = re.compile(r"^[a-z0-9_-]{1,16}$")
_RE_HHMM = re.compile(r"[T ](\d{2}:\d{2})")

_LAST_INJECT: dict = {}
_LAST_SKIP: dict = {}          # 🔍 跳过也留痕：`last_injection` 只记成功，
                               #    排查"为什么没注入"以前是盲区（2026-09-30 实测）


def _skip(info: dict) -> dict:
    """把一次"决定不注入"记进 `_LAST_SKIP` 后**原样返回**（不改返回形状）。"""
    rec = {"at": now_iso(), "reason": info.get("reason") or ""}
    for k in ("session_id", "after_id"):
        if info.get(k):
            rec[k] = info[k]
    _LAST_SKIP.clear()
    _LAST_SKIP.update(rec)
    return info


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _off() -> bool:
    return os.environ.get("APP_EXT_ACTIVITY_DISABLED", "").strip().lower() in ("1", "true", "yes")


# ══════════════════════════════════════════════════════════════════════════
# ① 纯逻辑（不读库、不发请求 —— 可离线单测）
# ══════════════════════════════════════════════════════════════════════════

def _s(v) -> str:
    return str(v).strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def norm_time(v) -> Optional[str]:
    """ISO 形状的时刻 → 原样；形状不对 → **None**（不补、不猜）。"""
    s = _s(v)
    if not s or len(s) > 40 or not _RE_TIME.match(s):
        return None
    return s


def norm_at(v) -> Optional[str]:
    """一行动作的时刻。接受 `14:33` 或完整 ISO（取里面的 HH:MM）；其它 → None。"""
    s = _s(v)
    if not s:
        return None
    if _RE_AT.match(s):
        return s
    m = _RE_HHMM.search(s)
    return m.group(1) if m else None


def norm_state(v) -> Optional[str]:
    """状态词。**形状**锁死（小写短标识符），**取值**不锁死（未知值原样留）。"""
    s = _s(v).lower()
    return s if _RE_STATE.match(s) else None


def day_of(t: Optional[str]) -> str:
    """ISO 时刻 → `MM-DD`（给注入块标日期用）；拿不到 → 空串。"""
    s = _s(t)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[5:10]
    return ""


def minutes_of(started, ended) -> Optional[int]:
    """起止时刻差（分钟，向下取整）。**算不出来就是 None** —— 不拿别的数顶。"""
    a, b = _s(started), _s(ended)
    if not a or not b:
        return None
    try:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except Exception:
        return None
    d = (tb - ta).total_seconds()
    if d < 0:
        return None
    return int(d // 60)


def norm_actions(raw) -> list:
    """动作行归一化。**只做形状**，不改措辞（措辞是身体写的，房子不碰）。

    · 接受 `[{"at","text"}]`，也接受 `["一句话"]`（没有时刻就只留话）
    · 空行 / 非字符串非字典 → 丢
    · 每行截到 `LINE_MAX`，整段最多 `ACTIONS_MAX` 行
    """
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str):
            txt, at = _s(item), None
        elif isinstance(item, dict):
            txt = _s(item.get("text") or item.get("what") or item.get("line"))
            at = norm_at(item.get("at") or item.get("time") or item.get("ts"))
        else:
            continue
        if not txt:
            continue
        out.append({"at": at, "text": txt[:LINE_MAX]})
        if len(out) >= ACTIONS_MAX:
            break
    return out


def extract(meta) -> Optional[dict]:
    """从 `messages.meta` 里取出这一段活动。取不到可用内容 → **None**。

    容忍两种摆法（同 `kind="act"` 的先例）：
        嵌套：`{"activity": {...}}`   ← 推荐（`/channel/out` 会把 body 的非 type/text 键摊进 meta）
        平铺：`{"actions": [...], "started": ...}`
    """
    if not isinstance(meta, dict):
        return None
    src = meta.get("activity")
    if not isinstance(src, dict):
        # 平铺：至少要有一个"确实是这段活动"的键，否则任何 meta 都会被误判成活动
        if not any(k in meta for k in ("actions", "started", "ended", "footprint")):
            return None
        src = meta

    actions = norm_actions(src.get("actions"))
    started = norm_time(src.get("started") or src.get("start"))
    ended = norm_time(src.get("ended") or src.get("end"))
    footprint = _s(src.get("footprint") or src.get("note"))[:LINE_MAX * 2] or None
    if not actions and not footprint:
        return None
    return {
        "started": started,
        "ended": ended,
        "state": norm_state(src.get("state") or src.get("status")),
        "minutes": minutes_of(started, ended),
        "source": norm_state(src.get("source")),
        "actions": actions,
        "footprint": footprint,
    }


def build_block(items) -> str:
    """把几段活动拼成**给他读的**那段文字（纯函数）。

    🔴 只做三件事：**按行拼、加时刻、加"这是你自己的"框架话**。
       不总结、不润色、不加原文里没有的动词 —— 一个模型都没调，纯拼接。

    例：
        · 09-21 14:33 翻了翻 Galatea 的 3 个新帖
        · 09-21 14:36 去乌有乡走到河边，捡了块石头
        · 09-21 14:38（当时留的话：河边的风比上次凉了）
    """
    lines = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        d = day_of(it.get("started"))
        for a in (it.get("actions") or []):
            at = _s((a or {}).get("at"))
            txt = _s((a or {}).get("text"))
            if not txt:
                continue
            stamp = " ".join(x for x in (d, at) if x)
            lines.append(f"· {stamp} {txt}".strip() if stamp else f"· {txt}")
        fp = _s(it.get("footprint"))
        if fp:
            stamp = " ".join(x for x in (d, norm_at(it.get("ended")) or norm_at(it.get("started"))) if x)
            head = f"· {stamp} " if stamp else "· "
            lines.append(f"{head}（当时留的话：{fp}）")
    return "\n".join(lines)


def inject_text(items) -> str:
    """包成要插进去的那条 system。**MARK 必须出现**（`apply` 的去重依据）。

    🔴 框架话只说一件事：**这是你自己做过的，不是别人告诉你的**。
       同一个幻觉（他被塞了一份"关于自己的报告"）就是在这一行里消掉的
       —— 和 ⑧ 注入摘要时那句"这是你自己的记忆"同理。
    """
    body = build_block(items)[:TEXT_MAX]
    return (f"[{MARK} —— 你自己做的，不是别人告诉你的；"
            f"原文在你自己的房间（书房）里，这里只是记事。"
            f"当她问起你醒来/最近做了什么，这些就是你的经历本身，照实说即可]\n{body}")


# ══════════════════════════════════════════════════════════════════════════
# ② 读库（**只读** —— 本文件没有任何写 messages 的语句）
# ══════════════════════════════════════════════════════════════════════════

def _rows(relay, after_id: int, before_id: int, limit: int) -> list:
    from . import schema as _schema

    sql_after = int(after_id or 0)
    sql_before = int(before_id or 0) or 2 ** 62          # 0 = 不设上界
    with _schema.connect(relay) as conn:
        return [dict(r) for r in conn.execute(
            _SELECT, ("activity", sql_after, sql_before, max(1, int(limit)))).fetchall()]


def load_window(relay, after_id: int, before_id: int, limit: int = None) -> list:
    """窗口内的活动段，**按时间升序**返回（拼文本要正的）。"""
    n = int(limit if limit is not None else ITEMS_MAX)
    rows = _rows(relay, after_id, before_id, n)
    out = []
    for r in reversed(rows):                             # SQL 取的是最近 N 条（DESC）
        try:
            import json as _json
            meta = _json.loads(r.get("meta") or "{}")
        except Exception:
            meta = {}
        it = extract(meta)
        if not it:
            continue
        it["message_id"] = int(r.get("id") or 0)
        it["ts"] = r.get("ts")
        it["title"] = _s(r.get("text"))
        out.append(it)
    return out


def recent(relay, limit: int = 20) -> list:
    """最近的几段活动（不分窗口）—— `resolve` 的入口 + 只读端点/排查共用。

    常驻注入（2026-09-30）之后，它**就是**注入取数的唯一来源：每轮都带最近
    `ITEMS_MAX` 段，滚动更新。旧窗口函数 `prev_in_id` 已随窗口语义一起删除。
    """
    return load_window(relay, 0, 0, limit)


# ══════════════════════════════════════════════════════════════════════════
# ②-b 翻页游标（Tides 行迹页 · 2026-10-06）—— 纯逻辑，可离线单测
# ══════════════════════════════════════════════════════════════════════════

#: 单页上限。**有盖**是故意的：这是"给人看的一览"，不是"把整库倒出来"。
#:    真要全量，验收器 / 排查走 `counts()` 与 SQL，不走这个端点。
PAGE_MAX = max(1, int(os.environ.get("ACTIVITY_PAGE_MAX", "200") or 200))


def norm_cursor(v) -> int:
    """游标归一化。**只接受正整数 id**，其余一律当"没有游标"（= 从最新开始）。

    🔴 为什么这么严：`before` 直接进 SQL 上界，一个负数/乱码会让 `_rows` 的
       `id < ?` 语义变得莫名其妙（负数 → 空结果；非数字 → 抛异常）。而"翻页失败"
       最难受的形态是**静默给一个空页**（她会以为是没行迹了）。
       ⇒ 这里宁可把坏游标**降级成"从头开始"**，也不让它变成一个假空页。
    """
    s = _s(v)
    if not s or not s.isdigit():
        return 0
    try:
        n = int(s)
    except Exception:
        return 0
    # `isdigit()` 已经挡掉负号与小数，这里 return n 就够了（不多加一道 n>0：
    #   突变测试证明它跟 isdigit() 完全等价 —— 加着只是让人以为"这里有第二道保险"，
    #   其实是重复。真要第二道承重的墙，在 `page()` 里那道负数夹。）
    return n


def page_limit(v, default: int = 40) -> int:
    """单页条数：`?limit=` → 夹到 `1..PAGE_MAX`。坏值 → 默认值（不是 20，也不是 0）。"""
    s = _s(v)
    n = default
    if s and s.lstrip("-").isdigit():
        try:
            n = int(s)
        except Exception:
            n = default
    return max(1, min(PAGE_MAX, n))


def page(relay, before: int = 0, limit: int = 40) -> dict:
    """Tides 行迹页的一页。**只读**（走 `load_window`，它只 SELECT）。

    返回 `{items, next_before, has_more}`：
      · `items`      —— 按时间**升序**（旧的在前，翻页往下长）
      · `next_before`—— 下一页的游标 = 本页最小 `message_id`；**没有下一页时为 0**
      · `has_more`   —— 还有更早的（`next_before != 0`）
    🔴 **第一页是"最近 N 段"但仍按升序返回** —— 前端把它整体倒过来当最新在上，
       往后翻的新页直接接在下面（不倒）。倒一次就够，倒两次会让人以为时间倒流。
    """
    before = int(before or 0)
    # 🔴 纵深防御：`page()` 是公开入口，不许假设"上游一定已经 norm_cursor 过了"。
    #    负上界会让 `id < -50` 变成空结果 —— 她看到的是"没有更早的了"，
    #    而其实有：那是最难查的一种假（不报错、不 500、只是静静地骗她）。
    if before < 0:
        before = 0
    limit = max(1, min(PAGE_MAX, int(limit or 40)))
    # 🔴 多取一段来判断"还有没有更早的"，多取的那段**不返回**
    #    （否则最后一页会露出重复行 —— 前端一去重就把游标语义搞脏了）。
    #
    # 🔴🔴 注意截断方向：`load_window` 返回的是**升序**（旧的在前），
    #    所以"只留 limit 条"必须是 `[-limit:]` 而不是 `[:limit]` ——
    #    后者会砍掉**最新**的那一段（她打开页面看到的第一件事就没了）。
    #    2026-10-06 第一版就写反了，验收 B1 抓到。
    items = load_window(relay, 0, before, limit + 1)
    has_more = len(items) > limit
    if has_more:
        items = items[-limit:]
    next_before = min((int(i.get("message_id") or 0) for i in items), default=0)
    return {"items": items, "next_before": next_before if has_more else 0,
            "has_more": has_more}


# ══════════════════════════════════════════════════════════════════════════
# ③ 注入（读路径 —— 身体每次说话都经过这里）
# ══════════════════════════════════════════════════════════════════════════

def resolve(relay, probe_text: str) -> dict:
    """算出这次该带回去哪几段。**只读。**

    返回 `{ok, reason, session_id, message_id, after_id, items, text, chars}`。
    任何一步不确定 → `ok=True, text=""`（调用方照常放行）。

    🔴 2026-09-30 改「常驻注入」：原「她上次开口→这次开口」窗口作废——
    那个窗口会让"她第一句没问、第二句再问"就查无此行迹（违背正常聊天逻辑，
    Lily 亲口指出）。现在**每轮都带他最近的 `ITEMS_MAX` 段经历**（滚动更新），
    "我最近做过什么"是他上下文的常驻事实，不再是"只在你开口那瞬见一眼"的快照。
    `probe_text` 仍由调用方传入（用来确认"这轮是用户在说话"），
    但**不再用于反查会话**——他只有一个，行迹是同一段人生，跟会话无关。
    """
    out = {"ok": True, "reason": "", "session_id": "", "message_id": 0,
           "after_id": 0, "items": [], "text": "", "chars": 0}
    items = recent(relay, ITEMS_MAX)
    if not items:
        out["reason"] = "no_activity"
        return out
    text = inject_text(items)
    out["items"] = items
    out["text"] = text
    out["chars"] = len(text)
    return out


def apply(relay, body) -> dict:
    """把「你最近自己的经历」作为一条 system 插进 `body["messages"]`（**就地**）。

    🔴 **fail-open**：任何异常 / 任何不确定 → 原样放行（一个字段都不改）。
    🔴 **只插不删**：不动任何已有消息、不动顺序（同 ⑧）。
    返回诊断 dict（验收靠它、排查靠它）。`ok=False` 时 body 一定没被改过。
    """
    if _off():
        return _skip({"ok": False, "injected": False, "reason": "disabled"})
    if not isinstance(body, dict):
        return _skip({"ok": False, "injected": False, "reason": "bad_body"})
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return _skip({"ok": True, "injected": False, "reason": "no_messages"})

    for m in msgs:                                # 同一次请求别插两次（上游重试）
        if isinstance(m, dict) and MARK in str(m.get("content") or ""):
            return _skip({"ok": True, "injected": False, "reason": "already_present"})

    from . import context as C
    from . import providers as P

    probe = C.last_user_text(msgs)
    if not probe:
        return _skip({"ok": True, "injected": False, "reason": "no_user_text"})

    try:
        got = resolve(relay, probe)
    except Exception as e:
        return _skip({"ok": True, "injected": False,
                      "reason": f"resolve_failed:{type(e).__name__}"})
    if not got.get("text"):
        return _skip({"ok": True, "injected": False,
                      "reason": got.get("reason") or "empty",
                      "session_id": got.get("session_id") or "",
                      "after_id": got.get("after_id") or 0})

    text = got["text"]
    # 🔴 会撑爆网关上限就**不注入**。撑爆 → `normalize_request` 抛 `too_large` → 400
    #    → 这次说话直接失败。"更好用"绝不能变成"说不了话"。
    if len(msgs) + 1 > P.MAX_MESSAGES:
        return _skip({"ok": True, "injected": False, "reason": "too_many_messages"})
    if C.system_chars(msgs) + len(text) + 2 > P.MAX_SYSTEM_CHARS:
        return _skip({"ok": True, "injected": False, "reason": "system_too_large"})

    idx = C.insert_index(msgs)
    msgs.insert(idx, {"role": "system", "content": text})
    body["messages"] = msgs

    info = {"at": now_iso(), "session_id": got["session_id"], "chars": len(text),
            "index": idx, "items": len(got["items"]), "after_id": got["after_id"],
            "message_id": got["message_id"]}
    _LAST_INJECT.clear()
    _LAST_INJECT.update(info)
    return {"ok": True, "injected": True, **info}


def last_injection() -> dict:
    """最后一次注入的诊断（内存态，重启即失 —— 只给排查用，不是真相源）。"""
    return dict(_LAST_INJECT)


# ══════════════════════════════════════════════════════════════════════════
# ④ 端点（全部只读）
# ══════════════════════════════════════════════════════════════════════════

def counts(relay) -> dict:
    """活动行计数（只读体检用）。"""
    from . import schema as _schema

    with _schema.connect(relay) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE kind = ?",
                         ("activity",)).fetchone()["n"]
        last = conn.execute("SELECT MAX(id) AS id FROM messages WHERE kind = ?",
                            ("activity",)).fetchone()["id"]
    return {"rows": int(n or 0), "last_id": int(last or 0)}


def _install_routes(relay, public_prefix: str = "/") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    base = "/app/ext/activity"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    @relay.app.get(base)
    async def _list(request: Request):
        """最近几段活动（已归一化）。`?limit=` 默认 20，上限 100。

        🆕 带 `?before=<message_id>` 时**翻到更早的一页**（Tides 行迹页用），
           此时上限放宽到 `PAGE_MAX`(=200)、返回里多三个字段（见 `page()`）：
           `next_before` / `has_more` / `page`。

        🔴 **不带 `before` 时的返回值与改前逐字节一致** —— 注入侧与老调用方不受影响。
        """
        relay.check_auth(request)
        raw_before = _s(request.query_params.get("before"))
        if raw_before:                                   # 🆕 翻页分支
            try:
                got = page(relay, norm_cursor(raw_before),
                           page_limit(request.query_params.get("limit"), 40))
                return _json({"ok": True, "count": len(got["items"]),
                              "items": got["items"], "page": True,
                              "next_before": got["next_before"],
                              "has_more": got["has_more"],
                              "note": "只读翻页；写侧是身体的 POST /channel/out type=activity"})
            except Exception as e:
                return _json({"ok": False, "reason": "page_failed",
                              "detail": f"{type(e).__name__}: {e}"}, 500)
        try:                                              # 老分支：一个字没改
            limit = int(request.query_params.get("limit") or 20)
        except Exception:
            limit = 20
        limit = max(1, min(100, limit))
        try:
            items = recent(relay, limit)
            return _json({"ok": True, "count": len(items), "items": items,
                          "note": "只读；写侧是身体的 POST /channel/out type=activity"})
        except Exception as e:
            return _json({"ok": False, "reason": "list_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)

    @relay.app.get(base + "/tides")
    async def _tides(request: Request):
        """Tides 行迹页的**首页**（= 第一页 + 明确语义，省得前端自己判"是不是第一页"）。

        与 `GET /app/ext/activity?before=` 的差别只有一个：这里**永远有 items**
        （哪怕是空的——她会看到"还没有行迹"，而不是一个报错）。
        """
        relay.check_auth(request)
        try:
            got = page(relay, 0, page_limit(request.query_params.get("limit"), 40))
            return _json({"ok": True, "count": len(got["items"]), "items": got["items"],
                          "page": True, "next_before": got["next_before"],
                          "has_more": got["has_more"],
                          "note": "Tides 行迹页 · 只读"})
        except Exception as e:
            return _json({"ok": False, "reason": "tides_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        """开关 / 最近一次注入 / 计数。只读，不触发任何东西。"""
        relay.check_auth(request)
        try:
            c = counts(relay)
        except Exception as e:
            c = {"error": f"{type(e).__name__}: {e}"}
        return _json({
            "ok": True,
            "enabled": not _off(),
            "mark": MARK,
            "limits": {"text_max_chars": TEXT_MAX, "items_max": ITEMS_MAX,
                       "actions_max": ACTIONS_MAX, "line_max": LINE_MAX,
                       "page_max": PAGE_MAX},
            "counts": c,
            "last_injection": last_injection(),
            "last_skip": dict(_LAST_SKIP),
            "window": "常驻注入：每轮带最近 N 段（滚动更新，不再按她开口切窗口）",
            "tides": "行迹页：GET /app/ext/activity/tides 翻页，?before=<id> 往回",
            "note": "本层只读 + 只注入；不调 LLM、不写 messages、不挂 MCP 门",
        })

    @relay.app.post(base + "/preview")
    async def _preview(request: Request):
        """**只算不插**：给一个 OpenAI 风格 body，看这次会带回去什么。

        body: `{"messages":[{"role":"user","content":"…"}]}`
        （也可以只给 `{"probe":"那条用户消息的原文"}` —— 手工排查时更省事）
        """
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return _json({"ok": False, "reason": "bad_json"}, 400)
        if not isinstance(body, dict):
            return _json({"ok": False, "reason": "bad_json"}, 400)
        probe = _s(body.get("probe"))
        if not probe:
            from . import context as C
            msgs = body.get("messages")
            probe = C.last_user_text(msgs) if isinstance(msgs, list) else ""
        # 常驻注入后 probe 不再用于反查会话 —— 没有也能看（直接看最近 N 段）。
        try:
            got = resolve(relay, probe or "")
        except Exception as e:
            return _json({"ok": False, "reason": "resolve_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        return _json({
            "ok": True,
            "would_inject": bool(got.get("text")),
            "reason": got.get("reason"),
            "session_id": got.get("session_id"),
            "after_id": got.get("after_id"),
            "message_id": got.get("message_id"),
            "items": got.get("items"),
            "text": got.get("text"),
            "note": "只算不插：这条 system 会在下次说话时出现（每轮都带最近 N 段）",
        })


def install(relay, public_prefix: str = "/") -> None:
    """挂上活动层端点。**幂等**（第二次调用什么都不做）。"""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)


_INSTALLED = False


def summary_line() -> str:
    """启动时打一行（与别的层风格一致）。**GBK 安全**（见文件头）。"""
    return ("自主活动层就绪 · 注入开关=" + ("关" if _off() else "开")
            + f" · 最多 {ITEMS_MAX} 段 / {TEXT_MAX} 字符"
            + " · 常驻=每轮带最近 N 段 · 只读+只注入（不调 LLM、不写库、不挂 MCP 门）")
