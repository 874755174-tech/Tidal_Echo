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


async def _read_json(request) -> dict:
    """读请求体。不是 JSON / 不是对象（数组等）→ 返回 `_BAD`，调用方回 400。"""
    try:
        body = await request.json()
    except Exception:
        return _BAD
    return body if isinstance(body, dict) else {}


def _chunk(rid: str, model: str, created: int, text=None, finish=None, usage=None) -> str:
    """拼一个 OpenAI 形状的 SSE 帧。"""
    obj = {
        "id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0,
                     "delta": ({"content": text} if text is not None else {}),
                     "finish_reason": finish}],
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
        try:
            req = P.normalize_request({**body, "stream": False})
            out = await G.complete(pid, mid, req)
        except (P.ProviderError, G.GatewayError) as e:
            return _err(e)
        return JSONResponse({
            "id": "chatcmpl-" + uuid.uuid4().hex[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": out["model"],
            "provider_id": out["provider_id"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": out["text"]}}],
            "usage": out["usage"],
            "ms": out["ms"],
        })

    async def _do_chat(body: dict):
        """流式（OpenAI 兼容 SSE）。"""
        pid, mid = _pick(body)

        # 🔴 开流之前把能验的都验完 —— 这时还能回干净的 4xx
        try:
            req = P.normalize_request(body)
            p, mdl = P.resolve(pid, mid)
        except P.ProviderError as e:
            return _err(e)

        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        q: asyncio.Queue = asyncio.Queue()

        async def _producer():
            try:
                res = await G.stream_chat(p["id"], mdl, req,
                                          on_text=lambda t: q.put(("d", t)))
                await q.put(("end", res))
            except asyncio.CancelledError:
                raise
            except BaseException as e:               # 已开流 → 只能在流里报
                await q.put(("err", e))

        async def _gen():
            task = asyncio.create_task(_producer())
            try:
                while True:
                    kind, val = await q.get()
                    if kind == "d":
                        yield _chunk(rid, mdl, created, text=val)
                    elif kind == "end":
                        yield _chunk(rid, mdl, created, finish="stop",
                                     usage=val.get("usage") or None)
                        yield "data: [DONE]\n\n"
                        break
                    else:
                        err = val.as_dict() if hasattr(val, "as_dict") else {"message": str(val)}
                        yield "data: " + json.dumps({"error": err}, ensure_ascii=False) + "\n\n"
                        yield "data: [DONE]\n\n"
                        break
            finally:
                # 下游断开（手机锁屏 / 关页面）时把上游请求也停掉，别让它在后台烧钱
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
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
