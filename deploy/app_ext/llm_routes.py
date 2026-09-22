#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 · 模型网关（端点层）—— /app/ext/providers 与 /app/ext/llm/*
==========================================================================

## 🔴 核心设计：对下游伪装成"OpenAI 兼容端点"

    POST /app/ext/llm/chat                   请求体 = OpenAI 风格，响应 = OpenAI 风格 SSE
    POST /app/ext/llm/complete               请求体 = OpenAI 风格，响应 = OpenAI 风格 JSON

    POST /app/ext/llm/v1/chat/completions    🆕 别名：**按 body 的 stream 分流**（OpenAI 标准路径）
    POST /app/ext/llm/chat/completions       🆕 别名：同上（少一层 v1，防 base 填错）

为什么这么做 —— 这是 P1 里最省事的一个决定：

  · `examples/api_loop.py` **已经**会解析 OpenAI SSE（`stream_chat`）。
    将来接身体时，它只要把 base 换成房子、key 换成 `RELAY_SECRET`，
    **一行解析代码都不用改**，而供应商密钥从此不出房子。
  · 任何 OpenAI SDK / 客户端都能直接指着房子用。
  · 换供应商对下游完全透明（今天 DeepSeek，明天中转站，下游不知道）。

所以"内部统一格式"只在房子内部存在；对外的语言是 OpenAI 那套。

## 🆕 为什么需要那两个别名端点（P3 通车前置，2026-09-15）

`examples/api_loop.py` 生成请求时是**硬编码拼接**的，而它在红线目录里（改不了）：

    route["url"].rstrip("/") + "/chat/completions"      # :305 流式 / :343 非流式

也就是说**它总会往后拼 `/chat/completions`**。通车 = 把它的 `LLM_API_BASE`
指到房子，于是房子必须认这条路径，否则通车第一秒就是 404。

🔴 更关键的一点：身体**流式与非流式用的是同一个路径**，只靠 body 里的
   `stream` 字段区分（`stream_chat:292` 发 `True`，`complete_chat:333` 发 `False`）。
   所以别名端点必须**自己按 `stream` 分流** —— 这才是真正的 OpenAI 兼容行为，
   将来换任何标准客户端也都是零改动。

## 端点

    GET  /app/ext/providers                 允许列表（**无 URL、无 key**）+ 当前选择
    POST /app/ext/providers/probe           真发一次最小调用验模型（body: provider_id, model?/all?）
    POST /app/ext/llm/complete              OpenAI 兼容非流式（忽略 body 里的 stream）
    POST /app/ext/llm/chat                  OpenAI 兼容流式
    POST /app/ext/llm/v1/chat/completions   🆕 OpenAI 标准路径，按 stream 分流
    POST /app/ext/llm/chat/completions      🆕 同上（别名，防 base 填错一层）

🔴 每个端点第一行都是 `relay.check_auth(request)`（fail-closed，不依赖中间件顺序
   —— 沿用 `sessions_manage.py` 的教训）。

## 错误分两段（这点别搞混）

    返回流**之前**的错误（供应商没配、模型不在允许列表、body 不合法）
        → 正常 HTTP 4xx + JSON。因为还没吐字，能干净地报错。
    已经开流**之后**的错误（上游 401、限流、断连）
        → HTTP 已经 200 了，改不了状态码 → 在流里发 `data: {"error": {...}}` + `[DONE]`。

下游必须**两种都处理**：只看 HTTP 状态码会漏掉第二类。
"""

import asyncio
import json
import time
import uuid

# 🔴 功能性 import，不是风格问题，别删（见 identity.py 顶部第三节的 422 老坑）
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import providers as P
from . import llm_gateway as G
from . import identity as I

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",     # 让 nginx/Zeabur 边缘别把流缓冲住
}

# 哨兵：请求体不是合法 JSON 对象。用独立对象而不是 None，
# 因为 `{}`（空对象）是合法 body，不能跟"读不出来"混为一谈。
_BAD = object()

# 🆕 P1 收尾（⑦ 参数下发）：settings 表里哪些字段要"补进请求"。
#   名单是**白名单式**的 —— 新加字段必须显式列进来，防止某天 settings 多一列就
#   悄悄开始影响上游请求（那是排查起来最费劲的一类 bug）。
#   对应身份层的 `identity.WRITABLE_FIELDS`；那边管"能不能存"，这里管"发不发"。
_PARAM_KEYS = ("max_tokens", "temperature", "top_p", "effort")


def _err(e, default_status: int = 400):
    if hasattr(e, "as_dict"):
        return JSONResponse({"ok": False, "error": e.as_dict()},
                            status_code=getattr(e, "status", default_status))
    return JSONResponse({"ok": False, "error": {"code": "internal", "message": str(e)}},
                        status_code=500)


def _bad_json():
    return JSONResponse({"ok": False, "error": {"code": "bad_json",
                                                "message": "body 不是 JSON"}},
                        status_code=400)


def _inject(relay, body: dict) -> dict:
    """⑧ 上下文管理：把「本会话摘要」插进 `body["messages"]`（就地）。**fail-open**。

    🔴 出任何问题都原样放行 —— 上下文层是"更好用"，不是"能不能说话"的前提。
    🔴 **只插不删**：热区由身体（`history_n`）决定，网关**不裁历史**
       （见 `_pick` 里那条备案："网关猜历史 = 两处都以为对方在管"）。
       落地细节与三条纪律见 `context.py` 顶部。
    """
    try:
        from . import context as C      # 局部导入：模块加载期别互相拉扯
        return C.apply(relay, body)
    except Exception as e:              # 有意兜住全部（见上）
        return {"ok": False, "injected": False, "reason": f"{type(e).__name__}: {e}"}


def _inject_activity(relay, body: dict) -> dict:
    """⑪ 自主活动层：把「你最近自己的经历」插进 `body["messages"]`（就地）。**fail-open**。

    🔴 与 ⑧（摘要）**同一台机器、同一个位置**，只是插的东西不同、MARK 不同：
       ⑧ 是"我们以前说过什么"，⑪ 是"我最近自己做了什么"（Lily 09-21 要的"第三层"）。
       插完 system = 人格 → 摘要 → 足迹，而 `messages` 数组与没注入时**逐字节相同**。
    🔴 顺序：必须在 ⑧ **之后**调 —— 两条都插在 system 段尾部，先插的在前，
       于是顺序天然是"从旧到新"（以前的对话 → 最近的经历）。
    🔴 出任何问题都原样放行（确定性纯拼接，一次 LLM 都不调 —— 见 `activity.py`）。
    """
    try:
        from . import activity as A
        return A.apply(relay, body)
    except Exception as e:              # 有意兜住全部（见 `activity.py` 文件头）
        return {"ok": False, "injected": False, "reason": f"{type(e).__name__}: {e}"}


def _G9():
    """⑨ 停止/重答模块（局部导入）。拿不到 → None，调用方一律跳过。"""
    try:
        from . import generate as GEN
        return GEN
    except Exception:
        return None


def _solve_session(relay, body: dict) -> str:
    """⑨ 用：认出这次生成在替**哪个会话**说话。认不出 → `""`（一切照旧）。

    🔴 为什么必须在这里认：身体的 `stream_id` 从不往上游传（见 `generate.py` 顶部），
       网关**唯一**能拿到的身份线索就是"当前这条用户消息的原文"。
    🔴 与 ⑧ 共用同一个取法与同一个反查（`context.last_user_text` / `session_of`）——
       **会话是怎么认出来的只该有一份实现**，否则两边会给出不同答案。
    🔴 fail-open：任何异常都返回 `""` → 停止检查点整段失效，但**说话照常**。
    """
    try:
        from . import context as C
        msgs = body.get("messages")
        if not isinstance(msgs, list):
            return ""
        probe = C.last_user_text(msgs)
        if not probe:
            return ""
        found = C.session_of(relay, probe)
        return str((found or {}).get("session_id") or "")
    except Exception:
        return ""


async def _read_json(request) -> dict:
    """读请求体。不是 JSON / 不是对象（数组等）→ 返回 `_BAD`，调用方回 400。"""
    try:
        body = await request.json()
    except Exception:
        return _BAD
    return body if isinstance(body, dict) else {}


def _bill(relay, *, route: str, stream: bool, provider_id=None, model=None,
          usage=None, ms=None, text=None, chars_out=None, ok=None, note=None,
          session_id=None, body=None) -> dict:
    """P2 usage 记账：把这次真实调用记一行。**fail-open**（出任何问题都吞掉）。

    🔴 为什么 fail-open：记账是"更好用"，**不是"能不能说话"的前提**
       （跟 ⑧ 摘要 / ⑪ 足迹同一个原则）。这里抛出去 = 一次成功的回复
       在最后一步把用户搞挂 —— 而那笔账本来只是用来算钱的。

    🔴 三种"没有账单"的情形必须**显式**记（`ok=False` + `note`），不许不写：
        · `stopped`        —— 被用户/下游掐断，上游**很可能已经烧了 token**
        · `upstream_error` —— 上游中途炸，同样可能有消耗
        · 其余（默认）      —— 上游正常结束但**没给 usage**（`ok` 留空由 store 判）
       不写 = 缺口不可见 = 账本自己在说谎（详见 `usage_store.py` 文件头 ①）。

    参数里的 `body` 只用来**反查会话 id**（跟 ⑨ 同一份实现，不另起一套）。
    `session_id` 显式给了就用它（流式那条路已经算过，不必再算一次）。
    """
    try:
        from . import usage_store as U
        if session_id is None and body is not None:
            session_id = _solve_session(relay, body) or None
        if chars_out is None and text is not None:
            chars_out = len(text)
        return U.record(
            relay,
            provider_id=provider_id, model=model, session_id=session_id,
            route=route, stream=stream, usage=usage, ok=ok, note=note,
            ms=ms, chars_out=chars_out,
        )
    except Exception as e:                      # 🔴 有意兜住全部（见上）
        return {"ok": False, "recorded": False, "reason": f"{type(e).__name__}: {e}"}


def _chunk(rid: str, model: str, created: int, text=None, finish=None, usage=None,
           reasoning=None) -> str:
    """拼一个 OpenAI 形状的 SSE 帧。

    🆕 2026-09-16：多了 `reasoning` —— 思考链走 `delta.reasoning_content`
    （DeepSeek 系命名，OpenAI 兼容站里最通用的一支；也正是我们上游回来的那个名字）。
    改之前这里只有 `content` 一个字段，网关就算接住了思考链也没地方放 —— 断点②。

    🔴 两条纪律，都是为了"别把下游搞挂"：
      · 思考链帧里**不放 `content` 键**。下游身体是
        `chunk = delta.get("content") or ""` 然后 `if chunk:`
        （`examples/api_loop.py:326`，红线目录改不了）→ 它看到思考链帧
        拿到空串、直接跳过，**既不截断回复也不崩**；验收里有专门一条模拟它消费。
      · 没有思考链时**一个字段都不加** → 出口帧形状与改动前**逐字节相同**，
        老客户端和老断言一律不受影响。
    """
    delta = {}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if text is not None:
        delta["content"] = text
    obj = {
        "id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        obj["usage"] = usage
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def install(relay, public_prefix: str = "/") -> None:
    base = "/app/ext"

    def _pick(body: dict) -> tuple:
        """决定这次用哪个供应商/模型，并**就地**把 settings 里的参数补进 body。

        优先级：**请求体显式传的 > settings 表里的 > 什么都不传（网关用自己的默认）**。

        🔴 为什么"就地改 body"而不是返回一个新 dict（2026-09-15）：
           两个调用点（`_do_complete` / `_do_chat`）拿到的 body 本来就是同一个对象，
           而它们紧接着就把它交给 `normalize_request`。就地补字段 → **调用点一行不用改**，
           也就没有"改了这儿忘了那儿"的机会。副作用是明确的、唯一的，写在这里备案。
           注意：只补 `None`，**不覆盖**调用方给的值 —— body 里显式传 `temperature: 0`
           是"真的要 0"，不能被 settings 顶掉。

        ⚠️ `context_keep` / `context_trigger` **不在这里下发**：
           它们管的是"发多少历史给模型"，属于上下文管理（P2），网关只负责转发消息，
           不替调用方裁剪历史（网关猜历史 = 两处都以为对方在管）。
        """
        s = I.get_settings(relay)
        if not isinstance(body, dict):
            body = {}
        pid = body.get("provider_id") or s.get("provider_id") or None
        mid = body.get("model") or s.get("model_id") or None
        for k in _PARAM_KEYS:
            if body.get(k) is None and s.get(k) is not None:
                body[k] = s[k]                 # 见 docstring：只补空，不覆盖
        return pid, mid

    # ── 允许列表 ───────────────────────────────────────────────────────────
    @relay.app.get(base + "/providers")
    async def _providers(request: Request):
        relay.check_auth(request)
        s = I.get_settings(relay)
        return JSONResponse({
            "ok": True,
            "providers": P.catalog(),
            "default_provider": P.default_provider(),
            "selected": {"provider_id": s.get("provider_id"), "model_id": s.get("model_id")},
        })

    # ── 模型探测（回答"这个模型名到底对不对"）──────────────────────────────
    @relay.app.post(base + "/providers/probe")
    async def _probe(request: Request):
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return _bad_json()
        body = body if isinstance(body, dict) else {}
        pid = (body.get("provider_id") or "").strip() or P.default_provider()
        if not pid:
            return JSONResponse({"ok": False, "error": {
                "code": "no_provider", "message": "没有任何可用供应商"}}, status_code=400)
        try:
            if body.get("all"):
                out = await G.probe_provider(pid)      # 把该供应商所有模型都探一遍
            else:
                out = await G.probe(pid, (body.get("model") or "").strip() or None)
        except P.ProviderError as e:
            return _err(e)
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    # ── 🆕 原始帧诊断（只读；CoT 链路确诊用，2026-09-15）────────────────────
    #
    # 为什么它必须存在：上面那些端点都会把上游响应**翻译**成我们的格式，
    # 翻译就会把"我们不认识的字段"丢掉（`P.parse_stream` 只取 `delta.content`
    # 就是这个道理）。于是"上游到底给没给思考链"这个问题，
    # 用 `/llm/chat` 永远问不出答案 —— 得有一条**原样搬回来**的路。
    #
    # 🔴 只读：真发一次最小调用，把上游原话回给你，**不落库、不改链路、不解析**。
    #    代价是**会花钱**（一次几十~几百 token），所以别当健康检查反复打。
    @relay.app.post(base + "/providers/raw")
    async def _raw(request: Request):
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        pid = (body.get("provider_id") or "").strip() or P.default_provider()
        if not pid:
            return JSONResponse({"ok": False, "error": {
                "code": "no_provider", "message": "没有任何可用供应商"}}, status_code=400)
        try:
            out = await G.raw_probe(
                pid,
                (body.get("model") or "").strip() or None,
                prompt=(body.get("prompt") or "ping"),
                stream=bool(body.get("stream", True)),
                max_frames=body.get("max_frames") or 0,
                max_tokens=body.get("max_tokens") or 0,
            )
        except P.ProviderError as e:
            return _err(e)
        except G.GatewayError as e:
            return _err(e)
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    # ── 内部实现（两个真身；被下面的薄壳路由 + 别名端点共用）─────────────────
    #
    # 抽出来的原因：别名端点要按 body 的 stream 字段在这两者之间分流，
    # 而这两段逻辑必须**只有一份**（复制粘贴迟早会漂移）。
    async def _do_complete(body: dict):
        """非流式。body 里的 stream 一律忽略 —— 这个端点的语义就是非流式。"""
        pid, mid = _pick(body)
        _inject(relay, body)          # ⑧：只插摘要，不裁历史（fail-open，见上面）
        _inject_activity(relay, body)  # ⑪：再插"他最近做过的事"（同样只插不删）
        try:
            req = P.normalize_request({**body, "stream": False})
            out = await G.complete(pid, mid, req)
        except (P.ProviderError, G.GatewayError) as e:
            return _err(e)
        msg = {"role": "assistant", "content": out["text"]}
        # 🆕 思考链同样只"有才加" —— 没配思考的模型，响应形状逐字节不变
        if G.expose_reasoning() and out.get("reasoning"):
            msg["reasoning_content"] = out["reasoning"]
        # 🆕 P2 usage 记账：**先记账再回**（回响应体本身不受影响）。
        #    记在 return 之前是为了让 `ms` 与 `chars_out` 都在手边；
        #    它是 fail-open 的，所以"记账出问题"不会变成"这次调用失败"。
        _bill(relay, route="complete", stream=False, provider_id=out["provider_id"],
              model=out["model"], usage=out["usage"], ms=out["ms"],
              text=out["text"], body=body)
        return JSONResponse({
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": out["model"],
            "provider_id": out["provider_id"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
            "usage": out["usage"],
            "ms": out["ms"],
        })

    async def _do_chat(body: dict):
        """流式（OpenAI 兼容 SSE）。"""
        pid, mid = _pick(body)
        _inject(relay, body)          # ⑧：只插摘要，不裁历史（fail-open，见上面）
        _inject_activity(relay, body)  # ⑪：再插"他最近做过的事"（同样只插不删）

        # 🔴 开流之前把能验的都验完 —— 这时还能回干净的 4xx
        try:
            req = P.normalize_request(body)
            p, mdl = P.resolve(pid, mid)
        except P.ProviderError as e:
            return _err(e)

        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        q: asyncio.Queue = asyncio.Queue()
        # 🆕 出口要不要带思考链（默认带；`LLM_EXPOSE_REASONING=0` 可退回旧形状）。
        #    注意：**开关只管出口，不管解析** —— 思考链照样被接住，
        #    只是不发进流里。这样"关掉"= 完全回到改动前的行为，可逆。
        show_think = G.expose_reasoning()

        # ── ⑨ 登记"这一次生成在飞" ─────────────────────────────────────────
        # 🔴 停止信号走**往队列塞哨兵**（`register_notify`），不给 `q.get()` 加超时轮询：
        #    `asyncio.wait_for` 掐 `Queue.get()` 有"刚好取到却当超时"的经典竞态，
        #    丢一个 chunk 会让回复缺一截。塞哨兵 = 不轮询、零延迟、不丢东西。
        # 🔴 认不出会话（g_sid=""）也照样登记 —— "停当前在飞的那一个"要能用。
        g_sid = _solve_session(relay, body)
        G9 = _G9()
        if G9 is not None:
            try:
                G9.begin(g_sid, model=mdl)
                G9.register_notify(g_sid, lambda: q.put_nowait(("stop", None)))
            except Exception:
                G9 = None                     # ⑨ 出问题 = 少了"能停"这个能力，说话照常

        async def _producer():
            try:
                res = await G.stream_chat(p["id"], mdl, req,
                                          on_text=lambda t: q.put(("d", t)),
                                          on_reasoning=lambda t: q.put(("r", t)))
                await q.put(("end", res))
            except asyncio.CancelledError:
                raise
            except BaseException as e:               # 已开流 → 只能在流里报
                await q.put(("err", e))

        async def _gen():
            task = asyncio.create_task(_producer())
            stopped = False
            # 🆕 usage 记账的"防重记"标志：下面三条正常收尾路径（stop / end / error）
            #    各自记一笔并置 True；`finally` 只给**没记过**的那条路兜底。
            billed = False
            try:
                while True:
                    kind, val = await q.get()
                    if kind == "stop":
                        # 🔴🔴 ⑨ 的收尾必须"**温柔掐**"：补一个**正常的** `finish=stop`
                        #     + `data: [DONE]`，让身体以为"模型正常说完了"。
                        #    如果这里直接把流掐断，身体 `run_model` 会走
                        #    `except Exception` → **fallback 去链上的下一个模型重跑一遍**
                        #    （`examples/api_loop.py:376-381`）→ 用户看到的"停止"
                        #    会变成"换了个模型又生成了一整条"。
                        stopped = True
                        task.cancel()          # 真停住上游：不再向 provider 要数据
                        # 🆕 usage 记账：**这一笔必须留痕**（见 `_bill` 的 docstring）。
                        #    掐断时账单还没到（它在最后一帧）→ 拿不到 usage；
                        #    但上游**很可能已经烧了 token** ⇒ 缺口要看得见，
                        #    不能因为"没数据"就当这笔调用没发生。
                        _bill(relay, route="chat", stream=True, provider_id=p["id"],
                              model=mdl, ok=False, note="stopped", session_id=g_sid)
                        billed = True
                        yield _chunk(rid, mdl, created, finish="stop")
                        yield "data: [DONE]\n\n"
                        break
                    if kind == "d":
                        yield _chunk(rid, mdl, created, text=val)
                        if G9 is not None:
                            try:
                                G9.touch(g_sid, len(val))
                            except Exception:
                                pass
                    elif kind == "r":
                        # 思考链单独成帧（delta 里只有 reasoning_content）。
                        # 顺序天然保序：跟正文走同一个队列，上游怎么发我们就怎么放。
                        if show_think:
                            yield _chunk(rid, mdl, created, reasoning=val)
                    elif kind == "end":
                        # 🆕 usage 记账：正常结束这一条 —— 账单就在 `val["usage"]` 里
                        #    （流式那条由 providers.adapt 显式索要，见 `_stream_usage_on()`）。
                        #    `ok` 不显式传：由 store 判断"这份账单到底有没有用"
                        #    （上游正常结束却一个数都没给 → 记成 ok=0，缺口照样可见）。
                        _bill(relay, route="chat", stream=True, provider_id=p["id"],
                              model=mdl, usage=val.get("usage"), ms=val.get("ms"),
                              text=val.get("text"), session_id=g_sid)
                        billed = True
                        yield _chunk(rid, mdl, created, finish="stop",
                                     usage=val.get("usage") or None)
                        yield "data: [DONE]\n\n"
                        break
                    else:
                        # 🔴 上游**中途**炸了（已开流，只能在流里报）。
                        #    这一帧 + [DONE] 对"这次说话"是对的（身体要有个结束标记
                        #    才能收尾），但它有个**副作用**（2026-09-19 实测发现）：
                        #    身体的 `stream_chat` 对 error 帧**视而不见**（只取
                        #    `delta.content`），读到 [DONE] 就当"模型正常说完了"
                        #    → 那半截照常落库，而**库里一点痕迹都没有**。
                        #    所以这里补一句留痕：等那条落库后打 `upstream_error`
                        #    （与 stop 的 `truncated` 共用同一台机器）。
                        #    只碰 `meta`，走三道守卫；任何异常都不许影响这条流。
                        if G9 is not None:
                            try:
                                await G9.note_upstream_error(relay, g_sid)
                            except Exception:
                                pass
                        # 🆕 usage 记账：上游**中途**炸了 —— 同样可能有消耗（它可能
                        #    已经生成了一半才断），所以留痕而不是不写。同 stop 那条。
                        _bill(relay, route="chat", stream=True, provider_id=p["id"],
                              model=mdl, ok=False, note="upstream_error",
                              session_id=g_sid)
                        billed = True
                        err = val.as_dict() if hasattr(val, "as_dict") else {"message": str(val)}
                        yield "data: " + json.dumps({"error": err}, ensure_ascii=False) + "\n\n"
                        yield "data: [DONE]\n\n"
                        break
            finally:
                # 🆕 usage 记账兜底：走到这儿还**没人记过账** = 这次生成是被
                #    **下游断开**带走的（手机锁屏 / 关页面 / 刷新）——
                #    `kind` 三条收尾路径都没轮到，而**上游那边很可能已经烧了 token**。
                #    `billed` 防的是"记两笔"（那三条都已经记过并置 True）。
                if not billed:
                    _bill(relay, route="chat", stream=True, provider_id=p["id"],
                          model=mdl, ok=False, note="client_disconnect",
                          session_id=g_sid)
                # 下游断开（手机锁屏 / 关页面）时把上游请求也停掉，别让它在后台烧钱
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                # ⑨ 注销登记（正常结束 / 报错 / 被停，三条路都要走到）
                if G9 is not None:
                    try:
                        G9.end(g_sid, stopped=stopped)
                    except Exception:
                        pass

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    # ── 非流式（薄壳）───────────────────────────────────────────────────────
    @relay.app.post(base + "/llm/complete")
    async def _complete(request: Request):
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        return await _do_complete(body)

    # ── 流式（薄壳）────────────────────────────────────────────────────────
    @relay.app.post(base + "/llm/chat")
    async def _chat(request: Request):
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        return await _do_chat(body)

    # ── 🆕 OpenAI 标准路径别名（P3 通车前置）──────────────────────────────
    #
    # 注册两条，是为了让 `LLM_API_BASE` 填错一层也能活：
    #     https://<域名>/app/ext/llm/v1   → /app/ext/llm/v1/chat/completions   ← 推荐填这个
    #     https://<域名>/app/ext/llm      → /app/ext/llm/chat/completions
    #
    # 🔴 stream 的默认值按 **OpenAI 语义**取（没带 = 非流式），
    #    跟 `normalize_request` 里默认 True 不同 —— 那边默认 True 是为
    #    `/llm/chat` 这个"名字就写着流式"的端点服务的。别把两者混了。
    @relay.app.post(base + "/llm/v1/chat/completions")
    @relay.app.post(base + "/llm/chat/completions")
    async def _oai_chat(request: Request):
        relay.check_auth(request)
        body = await _read_json(request)
        if body is _BAD:
            return _bad_json()
        if body.get("stream"):
            return await _do_chat(body)
        return await _do_complete(body)

    # 这两个名字本身用不到，显式取一下是为了让 linter 别把 import 判成无用
    _ = (SSE_HEADERS, _err)
