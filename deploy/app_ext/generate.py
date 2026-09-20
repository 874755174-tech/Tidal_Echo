#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
P2 ⑨ · 停止 / 重答 / 多版本（stop / retry / reroll）
==========================================================================

## 一句话

让"这句我不想让它说完 / 我想让它重说"变成**真的** —— 而不是只在屏幕上变灰。

## 为什么这些事只能落在这层（身体在红线目录，改不了）

生成链路（读代码得来，不是推测）：

    前端 → 房子 /app/send → forward_to_loop → 身体 handle_ingest
         → 身体 stream_chat（上游就是这个房子！）→ 网关代理到真 provider
         → 身体 sink 回调 → 房子 /channel/out → save_message → 前端 SSE

🔴🔴 **身体的 `stream_id` 是它本地 `uuid4` 生成的**（`examples/api_loop.py:386`），
   发给上游的 body 里**只有** `{model, messages, temperature, max_tokens, stream}`
   —— 没有 stream_id，也没有 session_id。
   ⇒ 本层**永远不知道**身体那边的 `stream_id`。定位"当前这次生成"只能用
   **`api_session`**（复用 ⑧ 的 `context.session_of`：拿最后一条 user 原文回库反查）。
   前端传来的 `stream_id` 只当**回执**用，不当定位键。

## 三个动作

| 动作 | 语义 | 库里留下的痕迹 |
|---|---|---|
| **stop**   | 当前这次生成，停          | 那半条标 `meta.truncated = true` |
| **retry**  | 最后一条回复，重答一次    | 旧的那条标 `meta.superseded = true` |
| **reroll** | 同上，但前端给 `‹ 1/2 ›`  | 同上（前端按"中间没有新 in"自动成组） |

🔴 **还有第三种留痕，不来自任何请求**：上游**自己**在流中途炸掉时，网关会调
   `note_upstream_error()`，给随之落库的那条标 `meta.upstream_error = true`。
   它跟 `truncated` **分开**：一个是"用户按了停"，一个是"上游断了" ——
   两者在屏幕上长得一模一样（都是半截），但排查方向完全不同。
   ⚠️ 这一条是 2026-09-19 做 ⑨ 时**实测发现的缺口**：在此之前，
   上游断掉落库的那半条在库里**一点痕迹都没有**（见 `note_upstream_error` 的注释）。

🔴 **范围：只做"最后一条回复"**（Lily 2026-09-19 拍板）。
   对历史中间某条重答会牵连它之后**所有**轮次（那些上下文里含被作废的回复），
   是另一个量级的事，⑨ 不做。

## 🔴 stop 为什么是"两段式"

"停止"其实是**两件事**，缺一个都不算停住：

    ① 前端：立刻不渲染（AbortController 断 SSE）—— 体验
    ② 网关：真的不再向 provider 要数据     —— 钱

② 只能落在这儿：网关是**唯一**自己过手上游流的地方
   （`llm_routes.py:304` `_producer()` + `:315` `_gen()`）。
   本模块只负责**置标志** + **等那半条落库后打标**；真正收尾在 `llm_routes`。

🔴🔴 **收尾必须是"温柔掐"，不是断流。**
   如果直接把上游流掐断，身体 `run_model` 会走到 `except Exception`
   然后 **fallback 去链上的下一个模型重跑一遍**（`api_loop.py:376-381`）——
   于是"用户点了停止"的最终表现变成"换了个模型又生成了一整条"。
   所以网关那边要**补一个正常的 `finish=stop` + `data: [DONE]`**，
   让身体以为"模型正常说完了"，把那半条照常 POST 回来。

## 🔴 红线（2026-09-19 收窄后）

本模块**会写 `messages`**，但**只准写 `meta` 这一列**，且必须走
`schema.update_message_meta()`（自带三道守卫：行数不变 / 正文逐字不变 / 只 UPDATE meta）。
**原文一条不删、一个字不改** —— 这是 ⑨ 版的"只插不删"。

## 逃生开关

    APP_EXT_GENERATE_DISABLED=1   关掉 ⑨（网关的停止检查点也一起失效，一切照旧）

"""

import asyncio
import json
import os
import time
import urllib.request

from fastapi import Request
from fastapi.responses import JSONResponse

from . import schema as _schema

# ══════════════════════════════════════════════════════════════════════════
# 0 · 开关与常量
# ══════════════════════════════════════════════════════════════════════════


def _off() -> bool:
    return os.environ.get("APP_EXT_GENERATE_DISABLED", "").strip().lower() in (
        "1", "true", "yes")


#: 在飞登记最长留多久（秒）。与房子身体侧的 `STREAM_DRAFT_TTL`(600) 同量级，
#: 略长一点：留出"停止后等那半条落库"的余量。
INFLIGHT_TTL = float(os.environ.get("GENERATE_INFLIGHT_TTL", "900") or 900)

#: 停止之后等那半条落库的最长窗口（秒）。超时就放弃打标并记一条日志。
TAG_WAIT = float(os.environ.get("GENERATE_TAG_WAIT", "25") or 25)
TAG_STEP = 0.4

#: 叫身体重跑时等多久（秒）。
LOOP_TIMEOUT = float(os.environ.get("GENERATE_LOOP_TIMEOUT", "20") or 20)

#: 认不出会话时用的占位 key（跟 `context.session_of` 的"认不出就跳过"一个态度）。
UNKNOWN_KEY = "__session_unknown__"


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# 1 · 在飞登记表（内存；重启即空 —— 与 stream_drafts 同一个已知取舍）
# ══════════════════════════════════════════════════════════════════════════
#
# key = api_session（**不是** stream_id —— 那个本层根本不知道，见文件顶部）。
# 🔴 单 Kael 状态机保证：同一会话同一时刻只会有一个执行上下文
#    → 用会话当 key 无歧义，这也是"不传 session 就停当前那个"能成立的前提。

_INFLIGHT: dict = {}


def _key_of(session_id) -> str:
    return str(session_id or "").strip() or UNKNOWN_KEY


def prune() -> None:
    """清掉超时没动的登记（防内存长草）。"""
    now = time.time()
    for k in [k for k, v in _INFLIGHT.items()
              if now - float(v.get("touched") or 0) > INFLIGHT_TTL]:
        _INFLIGHT.pop(k, None)


def begin(session_id, *, model: str = "") -> dict:
    """一次生成开始。返回登记项。"""
    prune()
    k = _key_of(session_id)
    ent = _INFLIGHT.get(k) or {}
    ent.update({
        "session_id": str(session_id or ""),
        "model": model,
        "started": time.time(),
        "touched": time.time(),
        "chars": 0,
        "stop": False,
        "stopped_at": None,
        "seq": int(ent.get("seq") or 0) + 1,
    })
    _INFLIGHT[k] = ent
    return ent


def register_notify(session_id, fn) -> None:
    """登记一个"叫醒"回调（网关那边传 `queue.put_nowait` 进来）。

    🔴 **为什么要这个**：网关的流循环平时**阻塞**在 `await q.get()` 上。
       如果只在循环顶上查标志，就得给 `q.get()` 加超时轮询 —— 而
       `asyncio.wait_for` 掐 `Queue.get()` 存在"刚好取到却当成超时"的经典竞态，
       丢了 chunk 就会让回复缺一截。
       改成**往队列里塞一个哨兵**：不轮询、零延迟、不丢东西。
       回调抛错一律吞掉（停止是"更好用"，不是"能不能说话"的前提）。
    """
    ent = _INFLIGHT.get(_key_of(session_id))
    if ent is not None:
        ent["notify"] = fn


def _notify(session_id) -> None:
    ent = _INFLIGHT.get(_key_of(session_id))
    fn = (ent or {}).get("notify")
    if not fn:
        return
    try:
        fn()
    except Exception:
        pass


def touch(session_id, n: int = 0) -> None:
    """心跳 + 累计吐了多少字（停止时用来报告"已经说了多少"）。"""
    ent = _INFLIGHT.get(_key_of(session_id))
    if ent:
        ent["touched"] = time.time()
        ent["chars"] = int(ent.get("chars") or 0) + int(n or 0)


def should_stop(session_id) -> bool:
    """网关每个 chunk 前问的就是这一句。**认不出会话 → False（照常走）。**"""
    ent = _INFLIGHT.get(_key_of(session_id))
    return bool(ent and ent.get("stop"))


def end(session_id, *, stopped: bool = False) -> dict:
    ent = _INFLIGHT.pop(_key_of(session_id), None) or {}
    if ent and stopped:
        ent["ended_by"] = "stop"
    return ent


def live() -> list:
    """当前在飞的那些（给 status / 停止后的报告用）。"""
    prune()
    return sorted(_INFLIGHT.values(), key=lambda v: float(v.get("started") or 0))


def _only_live(include_stopped: bool = False) -> dict:
    items = [v for v in live() if include_stopped or not v.get("stop")]
    return items[-1] if items else {}


def request_stop(session_id=None) -> tuple:
    """置停止标志。返回 `(ok, 说明, 会话)`。

    传了 session → 停那个会话。
    **不传 session → 停"当前在飞的那一个"**（单 Kael：同时只会有一个）。
    🔴 只置标志，**不在这里收尾** —— 收尾在网关的流循环里做（见文件顶部）。
    🔴 **重复请求停止也算成功**（返回 `"已经在停了。"`）：停止是两段式的，
       标志置完到那半条落库之间还有空档，用户可能再点一次。
    """
    prune()
    if session_id:
        ent = _INFLIGHT.get(_key_of(session_id))
        if not ent:
            return False, "这个会话现在没有正在生成的内容。", ""
        if ent.get("stop"):
            return True, "已经在停了。", str(ent.get("session_id") or "")
        ent["stop"] = True
        ent["stopped_at"] = time.time()
        _notify(session_id)                 # 往网关的队列里塞哨兵（见 register_notify）
        return True, "已请求停止。", str(ent.get("session_id") or "")
    # 🔴 这里要**连已经置了 stop 的也算**（`include_stopped=True`）：
    #    停止是两段式的 —— 置完标志到那条落库之间还有一段时间，
    #    期间用户完全可能**再点一次**（手快 / 网络慢）。
    #    如果按"没在飞"报错，前端就会把一次正常的双击显示成红字。
    #    语义上"已经在停了"也是**成功**，不是失败。
    ent = _only_live(include_stopped=True)
    if not ent:
        return False, "现在没有正在生成的内容。", ""
    if ent.get("stop"):
        return True, "已经在停了。", str(ent.get("session_id") or "")
    ent["stop"] = True
    ent["stopped_at"] = time.time()
    _notify(ent.get("session_id"))
    return True, "已请求停止。", str(ent.get("session_id") or "")


# ══════════════════════════════════════════════════════════════════════════
# 2 · 读会话（会话归属用 meta.api_session —— ⑧ 那条硬发现）
# ══════════════════════════════════════════════════════════════════════════

def _parse_meta(raw) -> dict:
    try:
        m = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def rows_of_session(relay, session_id: str, limit: int = 500) -> list:
    """取某会话的消息（按 id 升序）。`meta.api_session` 为空的行**不算**任何会话。

    ⚠️ 为什么不用 `meta.session`：⑧ 拆导出时实测过 —— `$.session` 全部缺失，
       真正的归属在 `$.api_session`。写成 `session` 会**一条都取不到**。
    """
    sid = str(session_id or "").strip()
    with _schema.connect(relay) as conn:
        raw = conn.execute(
            "SELECT id, ts, direction, kind, text, meta FROM messages "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in raw:
        meta = _parse_meta(r["meta"])
        if str(meta.get("api_session") or "").strip() != sid:
            continue
        out.append({"id": int(r["id"]), "ts": r["ts"], "direction": r["direction"],
                    "kind": r["kind"], "text": r["text"] or "", "meta": meta})
    out.sort(key=lambda x: x["id"])
    return out


def max_message_id(relay) -> int:
    with _schema.connect(relay) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM messages").fetchone()
    return int(row["n"] or 0)


def last_turn(relay, session_id: str) -> dict:
    """最后一条 user 消息 + 它之后最后一条 out 回复。

    返回 `{"ok": True, "in": {...}, "out": {...}}` 或 `{"ok": False, "error": {...}}`。
    🔴 只认**会话内**的消息；`in` 只认 `user`/`voice`（`__legacy__` 那 3 条无归属的
       落不进来，这是有意的 —— 重答必须知道自己在哪个会话里说话）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return _fail("no_session", "没告诉我要重答哪个会话。")
    rows = rows_of_session(relay, sid)
    if not rows:
        return _fail("no_session_rows", f"会话 {sid} 里一条消息都没有。")
    last_in = None
    for r in rows:
        if r["direction"] == "in" and r["kind"] in ("user", "voice") and r["text"].strip():
            last_in = r
    if last_in is None:
        return _fail("no_user_message", "这个会话里没有可用来重答的你说的话。")
    outs = [r for r in rows
            if r["direction"] == "out" and r["kind"] == "reply" and r["id"] > last_in["id"]]
    if not outs:
        return _fail("no_reply_yet",
                     "这条消息还没等到回复，没有可重答的那一条。")
    return {"ok": True, "session_id": sid, "in": last_in, "out": outs[-1]}


def _fail(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


# ══════════════════════════════════════════════════════════════════════════
# 3 · 叫身体重跑（复用房子自己的路：/app/send 也是这么叫的）
# ══════════════════════════════════════════════════════════════════════════

def loop_ingest_url() -> str:
    """跟 `backend/app.py:88` 读**同一个**环境变量（不 import 它，避免耦合）。"""
    return os.environ.get("RELAY_LOOP_INGEST_URL",
                          "http://127.0.0.1:3020/loop/ingest").strip()


def brain_target() -> str:
    """跟 `backend/app.py:352 brain_target()` 同一个规则（读同一个文件）。

    只用来给**好的报错**：桌面模式下没有身体可叫。
    """
    p = os.environ.get("RELAY_BRAIN_FILE", "").strip()
    if not p:
        base = os.environ.get("RELAY_DB", "").strip()
        if base:
            p = os.path.join(os.path.dirname(base), "brain_target")
    if not p:
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        return ""
    return t if t in ("desktop", "loop") else ""


def _fire_ingest_sync(msg_id: int, text: str, session_id: str) -> tuple:
    """同步 POST 到身体的 ingest（跟房子 `_forward_to_loop_sync` 同一形状）。"""
    data = json.dumps({"id": msg_id, "text": text, "session_id": session_id},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        loop_ingest_url(), data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LOOP_TIMEOUT) as resp:
        return int(resp.status), resp.read()[:400].decode("utf-8", "replace")


# ══════════════════════════════════════════════════════════════════════════
# 4 · 三个动作
# ══════════════════════════════════════════════════════════════════════════

_TAG_KEYS = ("truncated", "stop_requested_at")
_MARK_KEYS = ("superseded", "superseded_by", "rewritten_at", "variant")

#: "上游自己断掉"的痕迹。跟 `truncated` **分开**，因为语义不同：
#:   truncated      = **用户**按了停止（他知道自己喊了停）
#:   upstream_error = **上游**中途炸了（他/她根本不知道 —— 屏幕上看起来像"说完了"）
#: 🔴 分开的理由：将来回看"这条为什么短"时，这两个答案指向完全不同的排查方向。
UPSTREAM_ERROR_KEYS = ("upstream_error", "upstream_error_at")


def _newest_reply_after(relay, after_id: int, session_id: str = "") -> dict:
    """找 `id > after_id` 的最新一条 `out/reply`。找不到 → `{}`。

    `session_id` 给了 → 只认那个会话（走 `rows_of_session`，与别处同一条口径）。
    `session_id` 空 → **只按 id 认**。

    ⚠️ 这是一个**有意开的口子**，不是偷懒：`note_upstream_error` 那条路可能
       认不出会话（⑧ 就有"查不到就不猜"的先例），而这里要的是**留痕**不是**注入** ——
       "认不出会话 → 干脆不记"会把我们正要补的那个洞又留回去。
       安全性靠**单 Kael** 兜底：同一时刻只有一次生成在跑，
       `after_id` 之后的第一条 `out/reply` 几乎不可能是别人的。
       🔴 光靠"单 Kael"还不够 —— 如果**下一轮**在等待窗口里开始了，
       "第一条 out/reply"就会是**下一轮的回复**。所以配 `_newer_user_row()`
       一起用（见 `_tag_after`）：一旦看到新的"你说的话"，立刻收手。
    """
    if session_id:
        rows = [r for r in rows_of_session(relay, session_id) if r["id"] > after_id]
    else:
        rows = _rows_after(relay, after_id)
    fresh = [r for r in rows if r["direction"] == "out" and r["kind"] == "reply"]
    return fresh[-1] if fresh else {}


def _newer_user_row(relay, after_id: int, session_id: str = "") -> dict:
    """`id > after_id` 里有没有**新的"人说的话"**（`in` + `user`/`voice`）。

    🔴 有 → 说明**新的一轮已经开始了**，我们等的那半条不会再来了
       （身体那边这次生成已经作废/没落库）。此时必须**立刻放弃打标**，
       否则会把**下一轮的回复**标成 `truncated` / `upstream_error` ——
       那是往 `messages` 里写**假痕迹**，比不写坏得多。
    """
    if session_id:
        rows = [r for r in rows_of_session(relay, session_id) if r["id"] > after_id]
    else:
        rows = _rows_after(relay, after_id)
    hit = [r for r in rows
           if r["direction"] == "in" and r["kind"] in ("user", "voice")]
    return hit[0] if hit else {}


def _rows_after(relay, after_id: int) -> list:
    with _schema.connect(relay) as conn:
        raw = conn.execute(
            "SELECT id, ts, direction, kind, text, meta FROM messages "
            "WHERE id > ? ORDER BY id", (int(after_id),)).fetchall()
    return [{"id": int(r["id"]), "ts": r["ts"], "direction": r["direction"],
             "kind": r["kind"], "text": r["text"] or "",
             "meta": _parse_meta(r["meta"])} for r in raw]


async def _tag_after(relay, session_id: str, after_id: int, patch: dict,
                     *, reason: str) -> bool:
    """等那条新回复落库，然后给它打上 `patch`。

    ⚠️ 为什么是"等一下再看"而不是当场标：我们改的是**上游的流**；那条要等
       身体收到"正常结束"→ 再 POST `/channel/out` → 房子 `save_message` 之后才存在。
       这段空档 = 一次本机往返（实测亚秒级），但为了稳，这里给一个**有界**窗口
       （`GENERATE_TAG_WAIT`，默认 25 秒）。超时就放弃，并留一条日志。
    🔴 只找 `id > after_id` 的**新**行，绝不回头改任何已有消息。
    🔴 用 `reason` 只影响日志文案，不影响行为 —— 两个调用点共用这一条机器。
    """
    deadline = time.time() + TAG_WAIT
    while time.time() < deadline:
        await asyncio.sleep(TAG_STEP)
        try:
            # 🔴 先看"新的一轮开始了没有" —— 顺序不能反（先取 reply 就会误标，见函数注释）
            if _newer_user_row(relay, after_id, session_id):
                print(f"[app_ext] {reason}：没等到那条，倒是**新的一轮已经开始了** —— "
                      f"放弃打标（绝不给下一轮的回复写假痕迹）· "
                      f"session={session_id or '（认不出）'}")
                return False
            target = _newest_reply_after(relay, after_id, session_id)
        except Exception:
            return False
        if not target:
            continue
        if all(target["meta"].get(k) == v for k, v in patch.items()):
            return True
        try:
            wrote = _schema.update_message_meta(relay, target["id"], patch)
        except Exception as e:
            print(f"[app_ext] 打 {reason} 标失败（不影响消息本身）："
                  f"{type(e).__name__}: {e}")
            return False
        if wrote:
            print(f"[app_ext] {reason} · 已落库并标记 · "
                  f"session={session_id or '（认不出，按 id 认的）'} · "
                  f"msg_id={target['id']}")
            return True
    print(f"[app_ext] {reason}：等 {TAG_WAIT:.0f}s 没等到那条落库，"
          f"就不打标了（消息本身照常存着）· session={session_id or '（认不出）'}")
    return False


async def _tag_truncated_after(relay, session_id: str, after_id: int) -> bool:
    """停止之后，把随之落库的**那半条**标上 `truncated`。"""
    return await _tag_after(relay, session_id, after_id,
                            {"truncated": True, "stop_requested_at": now_iso()},
                            reason="停止成功 · 半条已落库并标记为 truncated")


async def note_upstream_error(relay, session_id: str) -> bool:
    """🔴 **上游**中途炸掉时，给随之落库的那条打 `upstream_error`。

    为什么需要这个（2026-09-19 做 ⑨ 时实测发现的缺口）：

        网关的 `_gen()` 在 `("err", e)` 分支里只往流里发一条
        `data: {"error": …}` 帧 + `data: [DONE]` —— 而身体的 `stream_chat`
        对 error 帧**视而不见**（它只取 `delta.content`），读到 `[DONE]`
        就当"模型正常说完了"。于是：

          上游吐到第 3 片炸了  →  身体收到"正常结束"  →  半截照常落库
                            →  **库里一点痕迹都没有**（既不是 truncated，
                               也看不出上游出过错）

        屏幕上看起来就是"他话说了一半"，但**没有任何地方记得这件事发生过**。
        ⑨ 的 stop 已经把"用户主动停"这一半补上了；这一半归这里。

    🔴 与 stop **共用一个机器**（`_tag_after`）：都是"等那条落库再打标"，
       都有界（`TAG_WAIT`），都只碰 `meta`，都走三道守卫。
    """
    if _off():
        return False
    marker = max_message_id(relay)
    sid = str(session_id or "").strip()
    if sid == UNKNOWN_KEY:
        sid = ""                    # 认不出 → 走"只按 id 认"（见 _newest_reply_after）
    try:
        asyncio.create_task(_tag_after(
            relay, sid, marker,
            {"upstream_error": True, "upstream_error_at": now_iso()},
            reason="上游中途断开 · 半条已落库并标记为 upstream_error"))
    except Exception:
        return False
    return True



async def stop(relay, session_id: str = "", stream_id: str = "") -> dict:
    """停。**只置标志 + 安排"等落库后打标"**，真正收尾在网关。"""
    if _off():
        return _fail("disabled", "⑨ 被 APP_EXT_GENERATE_DISABLED 关掉了。")
    ok, msg, target = request_stop(session_id or None)
    if not ok:
        return _fail("nothing_inflight", msg)
    marker = max_message_id(relay)
    # 🔴 **认不出会话也照样排打标**（target 可能是 ""）—— 那时 `_newest_reply_after`
    #    退化成"只按 id 认"。理由同 `_newest_reply_after` 的注释：这里是**留痕**，
    #    不是注入；"认不出 → 干脆不记"会把要补的洞留回去。
    try:
        asyncio.create_task(_tag_truncated_after(relay, target, marker))
    except Exception:
        pass
    return {
        "ok": True, "action": "stop", "message": msg,
        "session_id": target, "stream_id": str(stream_id or ""),
        "marker_id": marker,
        "note": "已请求停止。网关会在流里温柔收尾（补一个正常结束），"
                "那半条会照常落库并标上 truncated —— 原文一条不删。",
    }


async def rewrite(relay, session_id: str, *, mode: str = "retry") -> dict:
    """重答 / 换版本。**只做最后一条回复**。

    顺序很重要：**先叫身体，再标旧的那条**。
    （反过来会出现"旧的白标了、新的没来" —— 屏幕上凭空少一条回复。按这个顺序，
      叫不动身体时库里一格都没动。）
    """
    if _off():
        return _fail("disabled", "⑨ 被 APP_EXT_GENERATE_DISABLED 关掉了。")

    found = last_turn(relay, session_id)
    if not found.get("ok"):
        return found
    sess = found["session_id"]
    src_in = found["in"]
    old_out = found["out"]

    if brain_target() == "desktop":
        return _fail("brain_desktop",
                     "现在房子是桌面模式（brain_target=desktop），没有身体可以叫它重答。")

    if any(v.get("session_id") == sess for v in live() if not v.get("stop")):
        return _fail("busy", "这个会话正在生成中，先等它说完或者先停掉。")

    # ① 叫身体重跑（同一条 user 消息、同一个会话）
    try:
        status, raw = await asyncio.to_thread(
            _fire_ingest_sync, src_in["id"], src_in["text"], sess)
    except Exception as e:
        return _fail("loop_unreachable",
                     f"叫不动身体（{type(e).__name__}: {e}）。库里一格都没动。")

    # ② 标旧的那条（**不删、不改正文**）
    patch = {"superseded": True, "superseded_by": mode, "rewritten_at": now_iso()}
    if mode == "reroll":
        patch["variant"] = True
    try:
        tagged = _schema.update_message_meta(relay, old_out["id"], patch)
    except Exception as e:
        tagged = False
        print(f"[app_ext] 标记 superseded 失败：{type(e).__name__}: {e}")

    return {
        "ok": True,
        "action": mode,
        "session_id": sess,
        "user_message_id": src_in["id"],
        "superseded_id": old_out["id"] if tagged else None,
        "superseded_ok": bool(tagged),
        "loop_status": status,
        "loop_reply": raw,
        "note": ("已经叫身体用同一条你说的话再答一次。"
                 "旧的那条**没删**，只是标了 superseded（默认不显示，还能切回来看）。"
                 if tagged else
                 "已经叫身体重答，但旧那条的标记没写成功 —— 屏幕上可能会同时看到两条。"),
    }


# ══════════════════════════════════════════════════════════════════════════
# 5 · 只读诊断
# ══════════════════════════════════════════════════════════════════════════

def status(relay, session_id: str = "") -> dict:
    out = {
        "ok": True,
        "enabled": not _off(),
        "inflight": [{k: v.get(k) for k in
                      ("session_id", "model", "chars", "stop", "seq")}
                     for v in live()],
        "brain_target": brain_target() or "（读不到）",
        "loop_url": loop_ingest_url(),
        "tag_wait_sec": TAG_WAIT,
        "guard": _schema.guard_report(relay),
        "note": ("在飞登记是内存态，容器重启即空（与房子的 stream_drafts 同一个取舍）。"
                 "停止定位用 api_session，不用 stream_id —— 那个身体从不往上游传。"),
    }
    sid = str(session_id or "").strip()
    if sid:
        found = last_turn(relay, sid)
        if found.get("ok"):
            out["last_turn"] = {
                "user_message_id": found["in"]["id"],
                "reply_message_id": found["out"]["id"],
                "reply_chars": len(found["out"]["text"]),
                "reply_meta_flags": {k: found["out"]["meta"].get(k)
                                     for k in ("truncated", "superseded", "variant",
                                               "upstream_error")
                                     if found["out"]["meta"].get(k) is not None},
            }
        else:
            out["last_turn"] = found.get("error")
    return out


# ══════════════════════════════════════════════════════════════════════════
# 6 · 端点
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


def _answer(out: dict):
    """统一回包：`ok=False` → 400（并带上 error.code，前端好判断）。"""
    try:
        code = 200 if out.get("ok") else 400
    except Exception:
        code = 500
    return JSONResponse(out, status_code=code)


def install(relay, public_prefix: str = "/") -> None:
    base = "/app/ext/generate"

    @relay.app.post(base + "/stop")
    async def _stop(request: Request):
        """停掉当前这次生成。**只置标志**，收尾在网关。"""
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        try:
            out = await stop(relay,
                             str(body.get("session_id") or body.get("api_session") or ""),
                             str(body.get("stream_id") or ""))
        except Exception as e:                      # fail-open：端点也不许把房子带走
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}},
                status_code=500)
        return _answer(out)

    @relay.app.post(base + "/retry")
    async def _retry(request: Request):
        """最后一条回复，重答一次（旧的那条标 superseded，不删）。"""
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        try:
            out = await rewrite(relay,
                                str(body.get("session_id") or body.get("api_session") or ""),
                                mode="retry")
        except Exception as e:
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}},
                status_code=500)
        return _answer(out)

    @relay.app.post(base + "/reroll")
    async def _reroll(request: Request):
        """同 retry，但前端可以用 `‹ 1/2 ›` 把旧版本切回来看。"""
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        try:
            out = await rewrite(relay,
                                str(body.get("session_id") or body.get("api_session") or ""),
                                mode="reroll")
        except Exception as e:
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}},
                status_code=500)
        return _answer(out)

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        relay.check_auth(request)
        try:
            return JSONResponse(status(
                relay, request.query_params.get("session_id") or ""))
        except Exception as e:
            return JSONResponse({"ok": False, "error": {
                "code": "internal", "message": f"{type(e).__name__}: {e}"}},
                status_code=500)


def summary_line() -> str:
    """启动时打一行（与别的层风格一致）。

    🔴 **这一行必须是 GBK 安全的**（不带 ⑨ / ⚠️ / ✅ 这类符号）：
       它打在 `register()` 的 try **之外**，而 Windows 上子进程 stdout 是 cp936
       → 一个不在 GBK 里的字符就会抛 `UnicodeEncodeError` → **房子起不来**。
       （Zeabur 是 UTF-8 不会中招，但本地验收会 —— 这个坑记过，别再踩。）
    """
    return (f"停止/重答/多版本就绪 · 开关={'关' if _off() else '开'} · "
            f"定位键=api_session（stream_id 身体不往上游传）· "
            f"范围=只做最后一条回复 · 库里只加 meta（truncated / superseded），"
            f"原文一条不删")
