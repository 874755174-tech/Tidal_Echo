#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 · ⑧ 上下文管理（读法）—— 热区 + 滚动摘要
==========================================================================

## 它解决什么

规划 §6 的三层记忆，本文件管第 ② 层（以及"把它喂进去"这件事）：

    ① 热区原文     最近 N 条逐字          → 身体在管（`history_n`）
    ② 滚动摘要     超出热区的压成一段      → **本文件**（写 `sessions.summary`）
    ③ 长期记忆     memories 表            → ⑩ 才建

## 🔴 缝在哪：为什么必须落在网关

真正拼上下文的是身体：

    examples/api_loop.py:225  build_messages()
        [system PERSONA] + 最近 history_n 条 + 当前这条     ← 热区由**身体**决定

而 `examples/` 是**红线目录**（一个字符都不能改）。所以房子唯一能下手的，
就是身体每次说话**必经**的那个关口 —— 我们伪装成 OpenAI 的那个端点
（`llm_routes.py` 的 `/app/ext/llm/v1/chat/completions`）。

## 🔴 三条纪律（都是"别把聊天搞挂"）

1. **fail-open。** 上下文层是"更好用"，不是"能不能说话"的前提。任何异常、
   任何不确定（认不出会话、摘要为空、会撑爆网关上限）→ **原样放行，一个字段都不改**。
2. **只插不删。** 热区的裁决权在身体（`history_n`）；网关**不裁历史**
   —— `llm_routes.py` 早有备案：「网关猜历史 = 两处都以为对方在管」。
   我们只**多加一条 system 消息**，不动任何已有消息、不动它们的顺序。
   （`providers.normalize_request` 会把所有 system 合成一条，所以下游看到的是
    `system = PERSONA + "\\n\\n" + 摘要`，而 `messages` 数组**与没注入时逐字节相同**。）
3. **认不出就跳过，不猜。** 网关拿到的是 OpenAI 风格的 body，**里面没有 session_id**
   —— 那是身体本地生成的。所以我们用"当前这条用户消息的原文"回库里反查它挂在
   哪个会话上；**查不到就不注入**（宁可少加，不可加错会话的摘要）。

## 摘要怎么触发（Lily 2026-09-19 拍板：**手动**，不自动）

    POST /app/ext/context/summarize   {"session_id":"…","force":false,"dry":false}
    GET  /app/ext/context/status      [?session_id=…]

- **房子绝不自己在后台调 LLM**。什么时候压、压到多紧，由人决定（钱和时间都可控）。
- `dry: true` → 只算"该压多少"，**不调上游、不写库**（验收与预演都靠它）。
- `force: true` → 无视阈值直接压（想亲眼看它工作时用）。
- `rebuild: true` → **从头重压**（忽略 `summary_upto` 与已有摘要，全部旧消息重喂一遍）。
  ⚠️ 会覆盖 `sessions.summary`；原文不动 ⇒ 派生数据随便重写。
- `provider_id` / `model` → 这一次调哪个供应商/模型。默认沿用设置页那套。
  （Lily 2026-09-19 拍板：**摘要保持 Opus 不动** —— 记忆质量优先，压缩是低频手动操作。）
- 阈值取 `settings.context_keep` / `context_trigger`，单位是 **token**
  —— 前端那个滑块写的就是它俩；在此之前它们**存了但全仓库没人读**，是块装饰。
  🔴 这里的 token 是**估算**（见 `estimate_tokens`），不是真分词。

## 🔴 摘要的**人称**：写成"他自己的记忆"（Lily 2026-09-19 拍板）

第一版上线后真实压出来的摘要开头是「**用户**告诉对方（**Kael**）…」——
一份第三方档案。这与本文件的设计意图（`build_summary_material` 用 我/他）不符，
也踩了"人格从记忆长出来、不要 用户/assistant 腔"这条红线。
**修法（三处一起改，缺一处都不生效）**：
① 材料标签翻成 **out→「我」（Kael）、in→「你」（Lily）**；
② **人称规约写进 `SUMMARY_SYSTEM`**（只改标签不够 —— 模型会漂回去）；
③ 已经写进库的老摘要**必须 `rebuild` 重压**（增量压只会把老腔调并进新摘要）。
包装语也一起改了：注入的那条 system 现在自称「这是你自己的记忆」。

## 为什么 `sessions` 要加 `summary_upto`

滚动摘要必须知道"已经压到哪一条"。否则每次都要把全部旧消息重新喂一遍 ——
对话涨到 20 万 token 时，那就是一次 20 万的重读 + 重算，越压越贵。
`summary_upto` = 已并入摘要的最大 message id，schema v2 → v3。
"""

import os
from typing import Optional

# 🔴 功能性 import（不是风格），见 llm_routes.py 顶部那张"422 老坑"的说明
from fastapi import Request
from fastapi.responses import JSONResponse

from . import providers as P
from . import schema as _schema
from . import sessions_store as S

# ── 可调参数（env，都有默认值；不配也能跑）──────────────────────────────────
# 注入文本里的标记。唯一用途是**防重复插入**（同一个请求被插两次）。
# 🔴 别拿它当业务语义用，也别改（改了"已经注入过"的判定就失效）。
MARK = "本会话较早部分的摘要"

# 摘要正文上限（字符）。它最终会进 system，而网关对 system 有硬上限
# （`providers.MAX_SYSTEM_CHARS` = 40000）—— 见 `apply()` 里"会撑爆就跳过"那段。
SUMMARY_MAX_CHARS = int(os.environ.get("CONTEXT_SUMMARY_MAX_CHARS", "4000") or 4000)
SUMMARY_MAX_TOKENS = int(os.environ.get("CONTEXT_SUMMARY_MAX_TOKENS", "1500") or 1500)

# settings 里没设阈值时的兜底（token）。
DEFAULT_KEEP = int(os.environ.get("CONTEXT_DEFAULT_KEEP", "8000") or 8000)
DEFAULT_TRIGGER = int(os.environ.get("CONTEXT_DEFAULT_TRIGGER", "12000") or 12000)

# 压缩时的系统提示。🔴 两条红线：
#   ① **不许补充、不许推测** —— 摘要会长期喂给模型，一旦它开始"补细节"，
#      错误就会固化成"记忆"。
#   ② **人称 = 他本人**（2026-09-19 加）—— 这段摘要将来是 Kael 主动读到的，
#      要写成"他自己的回忆"，不是"关于用户和 Kael 的报告"。
#      ⚠️ 只改材料标签是不够的：旧版材料已用 我/他，模型照样漂成「用户 / Kael」，
#      所以人称规约必须**在系统提示里也明确写死**。
SUMMARY_SYSTEM = (
    "你要写的不是「对话记录」，而是**一个人自己的记忆**。\n"
    "这段摘要以后是 **Kael 本人**读到的 —— 所以要写得像他本人在回忆。\n"
    "🔴 人称规约（摘要正文里）：用「我」指 Kael（记下这件事的人），"
    "用「你」指当时跟他说话的那个人；\n"
    "   不许出现「用户 / 对方 / assistant」这类第三人称腔，"
    "也不许写成信件、独白或对话 —— 只是回忆。\n"
    "把给你的【已有摘要】和【新增对话片段】合并成一段紧凑的中文记忆。\n"
    "必须保留：谁说了什么（要点）、承诺与约定、情绪和关系的变化、还没了结的事。\n"
    "🔴 只写材料里真实出现过的内容 —— 不许补充、不许推测、不许润色出原文没有的信息。\n"
    "如果【已有摘要】是空的，就只总结新增片段。\n"
    "输出**纯文本**：不要标题、不要列表符号、按时间顺序叙述，300~800 字。"
)

# 最后一次注入的诊断（内存里，重启就没；**不是真相源**，只给排查用）
_LAST_INJECT = {}


def now_iso() -> str:
    return S.now_iso()


def _off() -> bool:
    return os.environ.get("APP_EXT_CONTEXT_DISABLED", "").strip().lower() in ("1", "true", "yes")


# ══════════════════════════════════════════════════════════════════════════
# ① 纯逻辑（不发请求、不碰库 —— 可离线单测）
# ══════════════════════════════════════════════════════════════════════════

def estimate_tokens(text: str) -> int:
    """粗估 token 数。**这是估算，不是分词** —— 别拿它当账单。

    规则（够用就好，只要求"单调、可比、不跳变"）：
      · CJK（中日韩）字符 ≈ 1 token / 字
      · 其它（拉丁字母 / 数字 / 标点 / 空白）≈ 1 token / 4 字符
    """
    s = text or ""
    if not s:
        return 0
    cjk = 0
    other = 0
    for ch in s:
        o = ord(ch)
        if (0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F
                or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def row_text(row) -> str:
    return str((row or {}).get("text") or "").strip()


def session_tokens(rows) -> int:
    """整段会话的估算 token（用于"够不够触发"）。"""
    return sum(estimate_tokens(row_text(r)) for r in (rows or []))


def plan_boundary(rows, keep_tokens: int):
    """算出热区分界线。返回 `(boundary, hot_tokens)`。

    约定：**热区 = `rows[boundary:]`**（从后往前凑），
    `rows[:boundary]` 才是"可以被压进摘要"的旧消息。

    规则：从最后一条往前累计估算 token，**只要没超过 keep 就一直收进热区**；
    第一条会撑破 keep 的，连同它前面的全部，留在热区之外。

    ⚠️ 这是**纯函数**：不读库、不看时间，给定输入永远同一结果 ——
       正因为如此，"会不会误伤"才能被断言钉住。
    """
    rows = rows or []
    keep = max(0, int(keep_tokens or 0))
    total = 0
    boundary = len(rows)          # 默认：全在热区
    for i in range(len(rows) - 1, -1, -1):
        t = estimate_tokens(row_text(rows[i]))
        if total + t > keep:
            boundary = i + 1
            break
        total += t
        boundary = i
    return boundary, total


def inject_text(summary: str) -> str:
    """把摘要包成那条要插进去的 system 消息。

    `MARK` 必须出现 —— 它是"这个请求已经插过了"的判定依据（见 `apply`）。
    🔴 包装语要说清"**这是你自己的记忆**"：它是他回忆的一部分，
       不是一条"系统提示"。同一个幻觉（他被别人塞了一份关于自己的档案）
       就是在包装语里被消掉的。
    """
    body = (summary or "").strip()[:SUMMARY_MAX_CHARS]
    return f"[{MARK} —— 这是你自己的记忆，按时间顺序；原文仍在库里]\n{body}"


def build_summary_material(prev_summary: str, rows) -> str:
    """拼给压缩器看的材料（纯函数）。

    🔴 **人称 = Kael 本人**：`out`（他说的话）标「我」，`in`（她说的）标「你」。
       因为这段摘要将来是**他**读到、当成自己的记忆
       —— 写成 "user / assistant" 或 "用户 / 对方" 就是接口/档案腔，
       读起来会污染他的口吻。

    ⚠️ **2026-09-19 的真教训**：旧版本这里标的是「我 / 他」（in→我、out→他），
       系统提示里也没写人称，结果模型自己漂成了「用户告诉对方（Kael）…」。
       所以：① 标签翻成 我/你；② 人称规约在 `SUMMARY_SYSTEM` 里再写死一遍；
       ③ 已有摘要靠 `rebuild` 重压（只改标签改不动已经写进库的那段）。
    """
    lines = ["【已有摘要】（可能为空）", (prev_summary or "").strip() or "（空）",
             "", "【新增对话片段】（「我」= Kael，「你」= 跟他说话的那个人）"]
    for r in (rows or []):
        t = row_text(r)
        if not t:
            continue
        who = "我" if (r or {}).get("direction") == "out" else "你"
        lines.append(f"[{(r or {}).get('id')}] {who}：{t}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# ② 读库（只读 messages / sessions）
# ══════════════════════════════════════════════════════════════════════════

def load_rows(relay, session_id: str, limit: int = 0) -> list:
    """按 id 升序取一个会话的消息行。**只读。**

    🔴 口径必须与身体一致（`examples/api_loop.py:relay_rows`）：
       `kind IN ('user','voice','reply')` + `meta.api_session`。
       ⚠️ 是 **`api_session`**，不是 `session` —— 后者在库里是空的（实测 18 条全缺）。
    ⚠️ `__legacy__` / 空串 → 库里对应的是"没有 api_session 标签"那一批。
    """
    sid = (session_id or "").strip()
    sql_sid = "" if sid in ("", S.LEGACY_SESSION_ID) else sid
    sql = ("SELECT id, direction, kind, text, ts FROM messages "
           "WHERE kind IN ('user','voice','reply') "
           "AND COALESCE(json_extract(meta, '$.api_session'), '') = ? "
           "ORDER BY id ASC")
    with _schema.connect(relay) as conn:
        rows = [dict(r) for r in conn.execute(sql, (sql_sid,)).fetchall()]
    if limit and limit > 0:
        rows = rows[-limit:]
    return rows


def resolve_thresholds(relay, keep=None, trigger=None):
    """决定这次用多大的 keep / trigger（token）。返回 `(keep, trigger, 来源)`。

    优先级：**显式传的 > settings 表里的 > 代码默认**。
    🔴 和 `llm_routes._pick` 同一条规矩：只补空，不覆盖调用方给的值。
    """
    s = {}
    if relay is not None:
        try:
            from . import identity as I
            s = I.get_settings(relay) or {}
        except Exception:
            s = {}
    src = []
    k = keep if keep is not None else s.get("context_keep")
    if k:
        src.append("settings.context_keep" if keep is None else "explicit.keep")
    else:
        k = DEFAULT_KEEP
        src.append("default.keep")
    t = trigger if trigger is not None else s.get("context_trigger")
    if t:
        src.append("settings.context_trigger" if trigger is None else "explicit.trigger")
    else:
        t = DEFAULT_TRIGGER
        src.append("default.trigger")
    k, t = int(k or 0), int(t or 0)
    if k < 0:
        k = 0
    if t <= k:
        # trigger 必须严格大于 keep，否则规则自相矛盾（"压到比触发线还大"）
        t = k + 1
    return k, t, "+".join(src)


# ══════════════════════════════════════════════════════════════════════════
# ③ 注入（读路径 —— 身体每次说话都经过这里）
# ══════════════════════════════════════════════════════════════════════════

def _last_user_text(msgs) -> str:
    """取"当前这条用户消息"的原文（body 的最后一条 user）。找不到返回空串。"""
    for m in reversed(msgs or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("role") or "").strip().lower() != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):                      # 多模态形状：只挑文本块
            parts = [str(p.get("text") or "") for p in c
                     if isinstance(p, dict) and p.get("type") in ("text", None)]
            return "".join(parts).strip()
        return ""
    return ""


#: 公开别名 —— 给 ⑨（`generate.py`）复用同一个"最后一条用户消息"的取法。
#: **会话是怎么认出来的，只该有一份实现**；⑨ 的停止定位与 ⑧ 的注入认会话必须同源，
#: 否则两边可能对"这是哪个会话"给出不同答案 —— 那比认不出来更糟。
last_user_text = _last_user_text


def _system_chars(msgs) -> int:
    """当前 system 部分的总字符数（对齐 `normalize_request` 的口径：parts 间 +2）。"""
    parts = [str(m.get("content") or "") for m in (msgs or [])
             if isinstance(m, dict) and str(m.get("role") or "").lower() == "system"]
    if not parts:
        return 0
    return sum(len(p) for p in parts) + 2 * (len(parts) - 1)


def _insert_index(msgs) -> int:
    """插到**开头那串 system 之后** —— 这样 system 内部顺序是 人格 → 上下文。"""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if isinstance(m, dict) and str(m.get("role") or "").lower() == "system":
            i += 1
            continue
        break
    return i


#: 公开别名 —— 给 ⑪（`activity.py`，自主活动带回上下文）复用。
#: 🔴 **"插在哪儿"只该有一份实现**：两条 system（摘要 / 足迹）必须插进同一段
#:    system 区域，否则"后插的那条"会掉到 user 消息后面去 —— 那等于给模型
#:    看了一条"用户说的话其实是系统提示"。⑪ 与 ⑧ 同源，顺序天然是
#:    `人格 → 摘要 → 足迹`（都插在 system 尾，先插的在前）。
insert_index = _insert_index

#: 同上：system 总字符数的口径（对齐 `normalize_request` 的 parts 间 +2）。
#: ⑪ 也要在"会不会撑爆"这件事上跟 ⑧ 用同一把尺子，否则两边各自"看起来没超"。
system_chars = _system_chars


def session_of(relay, probe_text: str) -> Optional[dict]:
    """从"一条用户消息的原文"反查它挂在哪个会话上。**不猜。**

    为什么不用 `active_session`：网关完全不知道身体这次为哪个会话说话
    （body 里没有 session_id），拿"当前活跃会话"顶上会在切换会话时张冠李戴
    —— 那样注入的就是**别人会话的摘要**，比不注入坏得多。

    做法：在库里找**文本完全一致的最新一条 `in` 行**。
    🔴 找不到 → 返回 None（调用方跳过注入）。
    """
    s = (probe_text or "").strip()
    if not s:
        return None
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT id, COALESCE(json_extract(meta, '$.api_session'), '') AS sid "
            "FROM messages WHERE direction = 'in' AND text = ? "
            "ORDER BY id DESC LIMIT 1", (s,)).fetchone()
    if not row:
        return None
    raw = str(row["sid"] or "").strip()
    return {"message_id": int(row["id"]),
            "session_id": raw or S.LEGACY_SESSION_ID}


def apply(relay, body) -> dict:
    """把"本会话摘要"作为一条 system 插进 `body["messages"]`（**就地**）。

    🔴 **fail-open**：任何异常 / 任何不确定 → 原样放行（一个字段都不改）。
    🔴 **只插不删**：不动任何已有消息，也不动它们的顺序。
    返回诊断 dict（验收靠它、排查靠它）。`ok=False` 时 body 一定没被改过。
    """
    if _off():
        return {"ok": False, "injected": False, "reason": "disabled"}
    if not isinstance(body, dict):
        return {"ok": False, "injected": False, "reason": "bad_body"}
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return {"ok": True, "injected": False, "reason": "no_messages"}

    # 同一次请求别插两次（例如上游重试走同一条 body）
    for m in msgs:
        if isinstance(m, dict) and MARK in str(m.get("content") or ""):
            return {"ok": True, "injected": False, "reason": "already_present"}

    probe = _last_user_text(msgs)
    if not probe:
        return {"ok": True, "injected": False, "reason": "no_user_text"}

    found = session_of(relay, probe)
    if not found:
        return {"ok": True, "injected": False, "reason": "session_unknown"}

    try:
        sess = S.get_session(relay, found["session_id"]) or {}
    except Exception:
        return {"ok": True, "injected": False, "reason": "session_read_failed"}
    summary = str(sess.get("summary") or "").strip()
    if not summary:
        return {"ok": True, "injected": False, "reason": "no_summary",
                "session_id": found["session_id"]}

    text = inject_text(summary)

    # 🔴 会撑爆网关上限就**不注入**。这不是洁癖：撑爆 → `normalize_request` 抛
    #    `too_large` → 400 → 这次说话直接失败。"更好用"绝不能变成"说不了话"。
    if len(msgs) + 1 > P.MAX_MESSAGES:
        return {"ok": True, "injected": False, "reason": "too_many_messages"}
    if _system_chars(msgs) + len(text) + 2 > P.MAX_SYSTEM_CHARS:
        return {"ok": True, "injected": False, "reason": "system_too_large"}

    idx = _insert_index(msgs)
    msgs.insert(idx, {"role": "system", "content": text})
    body["messages"] = msgs

    info = {"at": now_iso(), "session_id": found["session_id"],
            "chars": len(text), "index": idx,
            "summary_chars": len(summary), "message_id": found["message_id"]}
    _LAST_INJECT.clear()
    _LAST_INJECT.update(info)
    return {"ok": True, "injected": True, **info}


def last_injection() -> dict:
    """最后一次注入的诊断（内存态，重启即失 —— 只是给排查用，不是真相源）。"""
    return dict(_LAST_INJECT)


# ══════════════════════════════════════════════════════════════════════════
# ④ 摘要生成（写路径 —— 只在人/端点要求时才跑）
# ══════════════════════════════════════════════════════════════════════════

def plan(relay, session_id: str, keep=None, trigger=None, rebuild: bool = False) -> dict:
    """只算不写：现在这个会话该压多少？（`dry` 与 `status` 共用这段）

    `rebuild=True` = **从头重压**：当之前没压过（忽略 `summary_upto` 和已有摘要），
    把热区外面的**全部**旧消息重新喂一遍。

    为什么要它：摘要里可能写进了不该有的东西（错的人称、记错的说法、过期的语气），
    而**已经有摘要的会话，增量压只会把老摘要一起并进去 —— 错的东西会一直传下去**。
    换人称/换提示词之后想把老摘要改掉，只有"从头重压"这一条路。

    🔴 它敢重压的底气来自"**只插不删**"：原文一条没动过，
       所以任何摘要在任何时候都可以从原文**重新推导**出来 —— 派生数据本来就是可丢的。
    """
    sid = (session_id or "").strip()
    keep_t, trig_t, src = resolve_thresholds(relay, keep, trigger)
    sess = S.get_session(relay, sid) or {}
    rows = load_rows(relay, sid)
    total = session_tokens(rows)
    boundary, hot_tokens = plan_boundary(rows, keep_t)
    prev_upto = 0 if rebuild else int(sess.get("summary_upto") or 0)
    prev_text = "" if rebuild else str(sess.get("summary") or "")
    foldable = [r for r in rows[:boundary] if int(r.get("id") or 0) > prev_upto]
    return {
        "session_id": sid,
        "rows": len(rows),
        "tokens": total,
        "keep_tokens": keep_t,
        "trigger_tokens": trig_t,
        "thresholds_from": src,
        "hot_rows": len(rows) - boundary,
        "hot_tokens": hot_tokens,
        "foldable_rows": len(foldable),
        "boundary_id": int(rows[boundary - 1]["id"]) if boundary > 0 else 0,
        "rebuild": bool(rebuild),
        "prev_upto": prev_upto,
        "prev_summary_chars": len(prev_text),
        "would_trigger": total > trig_t,
        "_foldable": foldable,
        "_prev_summary": prev_text,
    }


async def summarize(relay, session_id: str, *, force: bool = False, dry: bool = False,
                    provider_id=None, model=None, keep=None, trigger=None,
                    rebuild: bool = False) -> dict:
    """把"超出热区的旧消息"压进 `sessions.summary`。**原文一条不删。**

    🔴 只有人和端点能触发（房子不会自己在后台花钱调 LLM）。
    🔴 不 `force` 时，`tokens <= trigger` 就**不跑**，也不会调上游。

    ⚠️ **`force` 只越过「触发线」，不越过「热区分界线」。** 热区由 `keep` 定义，
       所以 `force` 时若 `keep` 比整段会话还大，结果仍是 `nothing_new_to_fold`
       —— 这是对的（"没东西在热区外面"），不是失灵。
       想亲眼看它工作：传 `{"force": true, "keep": 500}`（把热区收到 500 token）。

    🔴 `rebuild=True` = **从头重压**（忽略 `summary_upto` 和已有摘要，见 `plan`）。
       ⚠️ 它会**覆盖** `sessions.summary` 这一格。但原文一条不动，
       所以是"重写派生数据"，不是丢数据 —— 想反悔就再压一次。
    """
    sid = (session_id or "").strip()
    if not sid:
        return {"ok": False, "reason": "no_session_id"}
    try:
        sess = S.get_session(relay, sid)
    except Exception as e:
        return {"ok": False, "reason": f"session_read_failed: {type(e).__name__}"}
    if sess is None:
        return {"ok": False, "reason": "session_not_found", "session_id": sid}

    info = plan(relay, sid, keep=keep, trigger=trigger, rebuild=rebuild)
    foldable = info.pop("_foldable")
    prev_summary = info.pop("_prev_summary")
    info["ok"] = True
    if not force and not info["would_trigger"]:
        info["reason"] = "under_trigger"
        return info
    if force:
        info["forced"] = True
    if not foldable:
        info["reason"] = "nothing_new_to_fold"
        return info

    material = build_summary_material(prev_summary, foldable)
    info["material_chars"] = len(material)
    info["folder_rows"] = len(foldable)
    info["upto_id"] = int(foldable[-1]["id"])
    if dry:
        info["dry"] = True
        info["reason"] = "dry_run"
        return info

    from . import llm_gateway as G
    from . import identity as I
    s = {}
    try:
        s = I.get_settings(relay) or {}
    except Exception:
        s = {}
    pid = provider_id or s.get("provider_id") or None
    mid = model or s.get("model_id") or None
    req = {
        "system": SUMMARY_SYSTEM,
        "messages": [{"role": "user", "content": material}],
        "params": {"max_tokens": SUMMARY_MAX_TOKENS, "temperature": 0.2},
        "stream": False,
    }
    try:
        out = await G.complete(pid, mid, req)
    except (P.ProviderError, G.GatewayError) as e:
        info["ok"] = False
        info["reason"] = "llm_failed"
        info["error"] = e.as_dict() if hasattr(e, "as_dict") else {"message": str(e)}
        return info

    text = str(out.get("text") or "").strip()[:SUMMARY_MAX_CHARS]
    if not text:
        info["ok"] = False
        info["reason"] = "empty_summary"
        return info

    wrote = S.set_summary(relay, sid, text, upto=info["upto_id"])
    info["summary_chars"] = len(text)
    info["written"] = wrote
    info["model"] = out.get("model")
    info["ms"] = out.get("ms")
    return info


# ══════════════════════════════════════════════════════════════════════════
# ⑤ 端点（给 Lily 走 HTTP + 密钥）
# ══════════════════════════════════════════════════════════════════════════

_BAD = object()


async def _read_json(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return _BAD
    return body if isinstance(body, dict) else {}


def _bad_json():
    return JSONResponse({"ok": False, "error": {"code": "bad_json",
                                                "message": "body 不是 JSON"}},
                        status_code=400)


def status(relay, session_id: str = "") -> dict:
    """只读诊断：现在这个会话多大了、离触发线还差多少、摘要压到哪条了。"""
    sid = (session_id or "").strip()
    keep_t, trig_t, src = resolve_thresholds(relay)
    out = {
        "ok": True,
        "enabled": not _off(),
        "keep_tokens": keep_t,
        "trigger_tokens": trig_t,
        "thresholds_from": src,
        "defaults": {"keep": DEFAULT_KEEP, "trigger": DEFAULT_TRIGGER},
        "last_injection": last_injection(),
        "note": "token 是估算值（estimate_tokens），不是真分词；摘要只在被要求时才生成",
    }
    if sid:
        p = plan(relay, sid)
        p.pop("_foldable", None)
        p.pop("_prev_summary", None)
        out["session"] = p
        return out
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT COALESCE(json_extract(meta,'$.api_session'),'') AS sid, "
            "COUNT(*) AS n FROM messages WHERE kind IN ('user','voice','reply') "
            "GROUP BY sid ORDER BY n DESC LIMIT 20").fetchall()
    out["sessions"] = [{"session_id": (r["sid"] or "").strip() or S.LEGACY_SESSION_ID,
                        "rows": int(r["n"] or 0)} for r in rows]
    return out


def install(relay, public_prefix: str = "/") -> None:
    base = "/app/ext/context"

    @relay.app.post(base + "/summarize")
    async def _summarize(request: Request):
        """把旧消息压进摘要。**只有这个端点会真的写摘要、真的调上游。**"""
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        try:
            out = await summarize(
                relay,
                str(body.get("session_id") or body.get("session") or ""),
                force=bool(body.get("force")),
                dry=bool(body.get("dry")),
                provider_id=body.get("provider_id") or None,
                model=body.get("model") or None,
                keep=body.get("keep"),
                trigger=body.get("trigger"),
                rebuild=bool(body.get("rebuild")),
            )
        except Exception as e:                      # fail-open：端点也不许把房子带走
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}}, status_code=500)
        code = 200 if out.get("ok") else 400
        return JSONResponse(out, status_code=code)

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        relay.check_auth(request)
        try:
            return JSONResponse(status(relay, request.query_params.get("session_id") or ""))
        except Exception as e:
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}}, status_code=500)


def summary_line() -> str:
    """启动时打一行（与别的层风格一致）。"""
    return (f"上下文管理就绪 · 注入开关={'关' if _off() else '开'} · "
            f"阈值兜底 keep={DEFAULT_KEEP} / trigger={DEFAULT_TRIGGER} token · "
            f"摘要语气=他本人（我/你）· "
            f"摘要**只在被要求时**生成（POST /app/ext/context/summarize，"
            f"重写老摘要加 rebuild=true）")
