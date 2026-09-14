#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 · 模型网关（下半）—— 真正发 HTTP、收 SSE
==========================================================================

`providers.py` 只算"该发什么"；本文件负责"真的发出去"。

## 🔴 为什么要抄 `examples/api_loop.py` 的两个写法

    httpx.AsyncClient(timeout=None, trust_env=False)

1. **`trust_env=False`** —— 不加这条，httpx 会去读 `HTTP_PROXY` / `HTTPS_PROXY`
   环境变量。Zeabur 容器里这些变量常被平台注进来，于是"本机好好的、线上连不上"。
2. **流式请求 `read=None`** —— 一次长回复可能好几分钟，默认 5 秒读超时会
   在句子中间掐断，且报的是 `ReadTimeout`（看着像上游挂了）。

## 三类错误必须分开（否则排查时全糊在一起）

    ProviderError   配置/参数问题（供应商没配、模型不在列表）→ 400，不用重试
    GatewayError    网络/上游问题（连不上、401、429、5xx）→ 502/503，可重试
    上游 SSE 里的 error 事件 → 中途出事，已经在吐字了 → 交给上层收尾

## 不发请求的验证方式

`tools/providers_check.py` 会起一个**本地假上游**，把 `PROVIDER_RELAY_BASE`
指过去 —— 三格式的流都能在离线环境跑通，不需要真 key、不花一分钱。
"""

import asyncio
import inspect
import json
import os
import time
from typing import Any, Callable, Optional

import httpx

from . import providers as P

# 读超时默认不限（长回复）；这两个 env 是给"网络很烂时"用的
CONNECT_TIMEOUT = float(os.environ.get("LLM_CONNECT_TIMEOUT", "15") or 15)
READ_TIMEOUT = os.environ.get("LLM_READ_TIMEOUT", "").strip()
READ_TIMEOUT = float(READ_TIMEOUT) if READ_TIMEOUT else None
PROBE_MAX_TOKENS = int(os.environ.get("PROBE_MAX_TOKENS", "16") or 16)


class GatewayError(Exception):
    """网络 / 上游层面的失败。`status` 是准备回给调用方的码。"""

    def __init__(self, code: str, message: str, status: int = 502, upstream: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.upstream = upstream

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.upstream:
            d["upstream_status"] = self.upstream
        return d


def _client(stream: bool) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT,
            read=None if stream else (READ_TIMEOUT or 120.0),
            write=30.0,
            pool=10.0,
        ),
        trust_env=False,        # 🔴 见文件顶部第 1 条，别删
        follow_redirects=True,
    )


async def _emit(on_text, text: str) -> None:
    """回调支持同步和异步（两种写法上层都会用，别要求调用方迁就）。"""
    if on_text is None:
        return
    r = on_text(text)
    if inspect.isawaitable(r):
        await r


def _sse_payloads(line: str) -> Optional[str]:
    """从一行原始 SSE 里取出 `data:` 后面的内容。不是 data 行就返回 None。"""
    s = (line or "").strip()
    if not s or s.startswith(":") or s.startswith("event:"):
        return None
    if s.startswith("data:"):
        return s[5:].strip()
    return None


async def stream_chat(
    provider_id: Optional[str],
    model: Optional[str],
    req: dict,
    on_text: Optional[Callable[[str], Any]] = None,
) -> dict:
    """流式调用。逐段把文本喂给 `on_text`，最后返回汇总。"""
    p, mid = P.resolve(provider_id, model)
    url, headers, body = P.adapt(p["id"], mid, {**req, "stream": True})

    t0 = time.time()
    text_parts: list = []
    usage: dict = {}

    async with _client(stream=True) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    raise GatewayError(
                        "upstream_error",
                        P.describe_http_error(resp.status_code, raw),
                        502 if resp.status_code >= 500 else 400,
                        upstream=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    payload = _sse_payloads(line)
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        break
                    chunk = P.parse_stream(p["id"], payload)     # 可能抛 ProviderError
                    if chunk:
                        text_parts.append(chunk)
                        await _emit(on_text, chunk)
                    u = _pick_usage(payload)
                    if u:
                        usage = u
        except httpx.TimeoutException as e:
            raise GatewayError("timeout", f"连 {p['label']} 超时：{type(e).__name__}", 504)
        except httpx.HTTPError as e:
            raise GatewayError("network", f"连不上 {p['label']}：{type(e).__name__}: {e}", 502)

    return {
        "provider_id": p["id"],
        "model": mid,
        "text": "".join(text_parts),
        "chunks": len(text_parts),
        "usage": usage,
        "ms": int((time.time() - t0) * 1000),
    }


def _pick_usage(payload: str) -> dict:
    """顺手捡一下 usage（各家的字段名不同，能捡到就捡）。捡不到返回 {}。"""
    if "usage" not in payload and "usageMetadata" not in payload:
        return {}
    try:
        obj = json.loads(payload)
    except Exception:
        return {}
    for k in ("usage", "usageMetadata", "message"):
        v = obj.get(k)
        if isinstance(v, dict):
            u = v.get("usage") if k == "message" else v
            if isinstance(u, dict) and u:
                return u
    return {}


async def complete(
    provider_id: Optional[str],
    model: Optional[str],
    req: dict,
) -> dict:
    """非流式调用（探测、以及不需要打字机效果的地方用）。"""
    p, mid = P.resolve(provider_id, model)
    url, headers, body = P.adapt(p["id"], mid, {**req, "stream": False})

    t0 = time.time()
    async with _client(stream=False) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as e:
            raise GatewayError("timeout", f"连 {p['label']} 超时：{type(e).__name__}", 504)
        except httpx.HTTPError as e:
            raise GatewayError("network", f"连不上 {p['label']}：{type(e).__name__}: {e}", 502)

    if resp.status_code >= 400:
        raise GatewayError(
            "upstream_error",
            P.describe_http_error(resp.status_code, resp.text),
            502 if resp.status_code >= 500 else 400,
            upstream=resp.status_code,
        )
    try:
        obj = resp.json()
    except Exception:
        raise GatewayError("bad_response", f"{p['label']} 返回的不是 JSON", 502)

    return {
        "provider_id": p["id"],
        "model": mid,
        "text": P.parse_complete(p["id"], obj),
        "usage": (obj.get("usage") or obj.get("usageMetadata") or {}) if isinstance(obj, dict) else {},
        "ms": int((time.time() - t0) * 1000),
    }


async def probe(provider_id: Optional[str], model: Optional[str] = None) -> dict:
    """发一次**最小**真实调用，回答"这个模型到底通不通"。

    规划 §4.2 说得很硬：写进允许列表的模型名必须**用真实调用验证过**。
    这个函数就是那个验证。成本：一次十几个 token 的调用。
    """
    req = {
        "system": "",
        "messages": [{"role": "user", "content": "ping"}],
        "params": {"max_tokens": max(1, PROBE_MAX_TOKENS), "temperature": 0},
        "stream": False,
    }
    try:
        out = await complete(provider_id, model, req)
    except (P.ProviderError, GatewayError) as e:
        return {"ok": False, "provider_id": provider_id, "model": model,
                "error": e.as_dict() if hasattr(e, "as_dict") else {"message": str(e)}}
    return {
        "ok": True,
        "provider_id": out["provider_id"],
        "model": out["model"],
        "ms": out["ms"],
        "sample": (out["text"] or "").strip()[:120],
        "usage": out["usage"],
    }


async def probe_provider(provider_id: str) -> dict:
    """把一个供应商的**所有**模型都探一遍（模型名写错时一次就能看出来）。"""
    p = P._view(provider_id)          # 内部用；这里就是要那份含 models 的完整视图
    if not P.is_available(p):
        return {"ok": False, "provider_id": provider_id,
                "error": {"code": "provider_unavailable",
                          "message": f"{p['label']} 没配齐（key / base / 模型清单）"}}
    results = []
    for m in p["models"]:
        results.append(await probe(provider_id, m))
    return {
        "ok": all(r["ok"] for r in results),
        "provider_id": provider_id,
        "label": p["label"],
        "verified": [r["model"] for r in results if r["ok"]],
        "failed": [{"model": r["model"], "error": r.get("error")} for r in results if not r["ok"]],
    }


def run(coro):
    """给同步调用方（脚本 / 非 async 路由）用的小包装。"""
    return asyncio.run(coro)
