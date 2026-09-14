#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 · 模型网关（端点层）—— /app/ext/providers 与 /app/ext/llm/*
==========================================================================

## 🔴 核心设计：对下游伪装成"OpenAI 兼容端点"

    POST /app/ext/llm/chat      请求体 = OpenAI 风格，响应 = OpenAI 风格 SSE
    POST /app/ext/llm/complete  请求体 = OpenAI 风格，响应 = OpenAI 风格 JSON

为什么这么做 —— 这是 P1 里最省事的一个决定：

  · `examples/api_loop.py` **已经**会解析 OpenAI SSE（`stream_chat`）。
    将来接身体时，它只要把 base 换成房子、key 换成 `RELAY_SECRET`，
    **一行解析代码都不用改**，而供应商密钥从此不出房子。
  · 任何 OpenAI SDK / 客户端都能直接指着房子用。
  · 换供应商对下游完全透明（今天 DeepSeek，明天中转站，下游不知道）。

所以"内部统一格式"只在房子内部存在；对外的语言是 OpenAI 那套。

## 端点

    GET  /app/ext/providers         允许列表（**无 URL、无 key**）+ 当前选择
    POST /app/ext/providers/probe   真发一次最小调用验模型（body: provider_id, model?/all?）
    POST /app/ext/llm/chat          OpenAI 兼容流式
    POST /app/ext/llm/complete      OpenAI 兼容非流式

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


def _err(e, default_status: int = 400):
    if hasattr(e, "as_dict"):
        return JSONResponse({"ok": False, "error": e.as_dict()},
                            status_code=getattr(e, "status", default_status))
    return JSONResponse({"ok": False, "error": {"code": "internal", "message": str(e)}},
                        status_code=500)


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
        """决定这次用哪个供应商/模型：请求体 → settings → 默认。"""
        s = I.get_settings(relay)
        pid = (body or {}).get("provider_id") or s.get("provider_id") or None
        mid = (body or {}).get("model") or s.get("model_id") or None
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
            return JSONResponse({"ok": False, "error": {"code": "bad_json",
                                                        "message": "body 不是 JSON"}},
                                status_code=400)
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

    # ── 非流式 ─────────────────────────────────────────────────────────────
    @relay.app.post(base + "/llm/complete")
    async def _complete(request: Request):
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": {"code": "bad_json",
                                                        "message": "body 不是 JSON"}},
                                status_code=400)
        body = body if isinstance(body, dict) else {}
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

    # ── 流式（OpenAI 兼容 SSE）─────────────────────────────────────────────
    @relay.app.post(base + "/llm/chat")
    async def _chat(request: Request):
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": {"code": "bad_json",
                                                        "message": "body 不是 JSON"}},
                                status_code=400)
        body = body if isinstance(body, dict) else {}
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

    # 这两个名字本身用不到，显式取一下是为了让 linter 别把 import 判成无用
    _ = (SSE_HEADERS, _err)
