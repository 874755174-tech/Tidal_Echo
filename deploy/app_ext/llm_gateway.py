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

# 🆕 只读诊断 `raw_probe` 的护栏（2026-09-15，CoT 链路确诊用）
#   为什么要护栏：这个端点会把**上游原话**搬回来，上游要是吐个 10MB，房子就没了。
#   `FRAMES` 限行数、`CHARS` 限总长、`TOKENS` 限这次调用多贵（诊断只要看到字段结构，
#   不需要长回答）。
RAW_MAX_FRAMES = int(os.environ.get("LLM_RAW_MAX_FRAMES", "60") or 60)
RAW_MAX_CHARS = int(os.environ.get("LLM_RAW_MAX_CHARS", "60000") or 60000)
RAW_MAX_TOKENS = int(os.environ.get("LLM_RAW_MAX_TOKENS", "64") or 64)


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


def expose_reasoning() -> bool:
    """出口帧 / 非流式响应**要不要带思考链**（CoT）。

    🆕 2026-09-16：确诊出上游确实把 CoT 放在 `reasoning_content` 独立字段里，
    而网关解析时把它丢了（见 `CoT显示链路.md` 断点①②）。修完之后默认**带出去**，
    因为"带出去"本身是安全的 —— 实测下游身体（`examples/api_loop.py:326`）是

        chunk = delta.get("content") or ""
        if chunk: ...

    即：帧里只有 `reasoning_content`、没有 `content` 时它拿到空串 → 直接跳过，
    **既不会截断回复、也不会崩**（验收里专门有"通车仿真 + 模拟身体消费"两条钉子）。

    🔴 保留这个开关是为了**可逆**：万一将来某个下游客户端对多出来的字段挑食，
    在 Zeabur 配 `LLM_EXPOSE_REASONING=0` 就退回改动前的形状，不用回滚代码。
    解析层**照样接住**思考链（`on_reasoning` 依然会被调用），只是不往出口放。
    """
    v = (os.environ.get("LLM_EXPOSE_REASONING") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


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
    on_reasoning: Optional[Callable[[str], Any]] = None,
) -> dict:
    """流式调用。逐段把文本喂给 `on_text`，思考链喂给 `on_reasoning`，最后返回汇总。

    🔴 2026-09-16 之前这里只认 `delta.content`（`P.parse_stream` 的老行为），
       上游放在 `reasoning_content` 里的思考链**被静默丢掉** —— 这就是
       "那个站能出 CoT、房子里看不到"的断点①。现在改成走
       `P.parse_stream_parts()`，按"这是正文还是思考链"分流。

    两者是**两条独立的流**，最后各自汇总在返回值的 `text` / `reasoning` 里：
      · `on_text`       → 正文增量（行为与改前完全一致）
      · `on_reasoning`  → 思考链增量（**新**；不传 = 只丢不报，跟改前一样）
    """
    p, mid = P.resolve(provider_id, model)
    url, headers, body = P.adapt(p["id"], mid, {**req, "stream": True})

    t0 = time.time()
    text_parts: list = []
    reasoning_parts: list = []
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
                    # 可能抛 ProviderError
                    for kind, piece in P.parse_stream_parts(p["id"], payload):
                        if kind == "reasoning":
                            reasoning_parts.append(piece)
                            if on_reasoning is not None:
                                await _emit(on_reasoning, piece)
                        else:
                            text_parts.append(piece)
                            await _emit(on_text, piece)
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
        "reasoning": "".join(reasoning_parts),
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

    # 🆕 非流式也要把思考链接住（`message.reasoning_content` / anthropic 的
    #    thinking 块 / gemini 的 thought part）—— 与流式同一个道理，改前全丢。
    text, reasoning = P.parse_complete_parts(p["id"], obj)
    return {
        "provider_id": p["id"],
        "model": mid,
        "text": text,
        "reasoning": reasoning,
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


# ══════════════════════════════════════════════════════════════════════════
# 🆕 只读诊断：把上游的**原话**搬回来（2026-09-15）
# ══════════════════════════════════════════════════════════════════════════
#
# 用途只有一句话：回答"上游到底给没给思考链、给在哪个字段里"。
#
#   路径 A  上游用独立字段（reasoning_content / reasoning / thinking …）
#           → 会死在网关解析（我们只取 content）
#   路径 B  上游把 CoT 内联在 content 里（<thinking>…</thinking>）
#           → 网关原样透传，死在前端 stripInlineThinkingText
#   路径 C  上游压根没给 → 问题在**请求参数**（多半要开 reasoning_effort/thinking）
#
# 这三条路的修法完全不同，所以**先确诊再施工**。`raw_hints` 就是那只眼睛。

# 独立字段名（各家叫法不一，命中任一即算"独立字段"）。
# 🔴 故意**带引号**写成 JSON 键的形状：不带引号的话，内联标记 `<thinking>` 里的
#    "thinking" 会被误判成"独立字段"，**路径 A 和路径 B 就分不开了** ——
#    而这两条路的修法完全不同，分不开等于没确诊。
_RAW_REASONING_MARKS = (
    '"reasoning_content"', '"reasoning_details"', '"reasoning"', '"thinking"',
)
# 内联标记（出现在正文里 = 路径 B）
_RAW_INLINE_MARKS = ("<thinking", "</thinking", "```thinking", "[thinking]")


def _redact(text: str, secret: str) -> str:
    """把 key 从要回给外部的东西里抹掉。key 为空时原样返回（别把空串当 key 替换）。"""
    s = (secret or "").strip()
    if not s or not text:
        return text
    return text.replace(s, "***")


def _redact_obj(obj, secret: str):
    """递归脱敏任意 JSON 结构（诊断会把"我们发出去的 body"也回显，一并抹）。"""
    s = (secret or "").strip()
    if not s:
        return obj
    if isinstance(obj, dict):
        return {k: _redact_obj(v, s) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, s) for v in obj]
    if isinstance(obj, str):
        return obj.replace(s, "***")
    return obj


def _url_parts(url: str) -> tuple:
    """拆出 `(scheme://host, path)`。

    🔴 **故意不返回 query** —— Gemini 那类供应商把 key 放在 query 里
       （见 `providers.py` 的 gemini 分支），回完整 URL 等于把 key 发出去。
    """
    from urllib.parse import urlsplit      # 与 providers.py 的 `_env` 同款写法
    u = urlsplit(url or "")
    origin = (u.scheme + "://" + u.netloc) if u.netloc else ""
    return origin, (u.path or "")


def raw_hints(blob: str) -> dict:
    """在原始响应里"扫一眼"，判定走的是路径 A / B / C。

    ⚠️ 它只做**提示**，不替代人看原始帧 —— 它回答"该往哪儿看"。
    """
    low = (blob or "").lower()
    inline = any(m in low for m in _RAW_INLINE_MARKS)
    fields = [k for k in _RAW_REASONING_MARKS if k in low]
    if inline and fields:
        path = "A+B"
    elif inline:
        path = "B"
    elif fields:
        path = "A"
    else:
        path = "C"
    return {"path": path, "reasoning_fields": fields, "inline_thinking": inline}


async def raw_probe(
    provider_id: Optional[str],
    model: Optional[str],
    prompt: str = "ping",
    stream: bool = True,
    max_frames: int = 0,
    max_tokens: int = 0,
) -> dict:
    """只读诊断：真发一次**最小**调用，把上游**原始**响应原样带回来。

    🔴 本函数**不调用** `P.parse_stream` / `P.parse_complete`。
       它的全部价值就是"上游原话"；一旦解析，被丢掉的字段就再也看不见了 ——
       而那恰好就是我们要查的东西。
    🔴 不写库、不改状态：只读上游，只回内容。
    🔴 只回 host + path（不回 query，见 `_url_parts`），响应里出现 key 一律抹成 `***`。
    """
    p, mid = P.resolve(provider_id, model)
    req = {
        "system": "",
        "messages": [{"role": "user", "content": (prompt or "ping")}],
        "params": {"max_tokens": max(1, int(max_tokens or RAW_MAX_TOKENS)), "temperature": 0},
        "stream": bool(stream),
    }
    # 走同一个 adapt → 回显的 request_body 就是"我们真正发给上游的东西"
    # （诊断"是不是少发了 reasoning_effort"时，这一项是直接证据）
    url, headers, body = P.adapt(p["id"], mid, req)
    key = (p.get("key") or "").strip()

    n_cap = max(1, min(int(max_frames or RAW_MAX_FRAMES), 200))
    t0 = time.time()
    frames: list = []
    truncated = False
    total = 0
    status = 0

    try:
        async with _client(stream=bool(stream)) as client:
            if stream:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    status = resp.status_code
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        frames.append(_redact(("HTTP %d\n%s" % (resp.status_code, raw))
                                              [:RAW_MAX_CHARS], key))
                    else:
                        async for line in resp.aiter_lines():
                            if not (line or "").strip():
                                continue        # 空行只是 SSE 帧分隔，无信息量
                            if len(frames) >= n_cap or total + len(line) > RAW_MAX_CHARS:
                                truncated = True
                                break       # 提前跳出 → 连接关闭 → 上游也停，不白烧钱
                            frames.append(_redact(line, key))
                            total += len(line)
            else:
                resp = await client.post(url, headers=headers, json=body)
                status = resp.status_code
                txt = resp.text or ""
                if len(txt) > RAW_MAX_CHARS:
                    txt, truncated = txt[:RAW_MAX_CHARS], True
                frames.append(_redact(txt, key))
    except httpx.TimeoutException as e:
        raise GatewayError("timeout", f"连 {p['label']} 超时：{type(e).__name__}", 504)
    except httpx.HTTPError as e:
        raise GatewayError("network", f"连不上 {p['label']}：{type(e).__name__}: {e}", 502)

    origin, path = _url_parts(url)
    blob = "\n".join(frames)
    return {
        "ok": 0 < status < 400,
        "provider_id": p["id"],
        "model": mid,
        "host": origin,               # ⚠️ 无 query（gemini 的 key 在 query）
        "path": path,
        "status": status,
        "stream": bool(stream),
        "request_body": _redact_obj(body, key),
        "frames": frames,
        "frame_count": len(frames),
        "truncated": truncated,
        "hints": raw_hints(blob),
        "ms": int((time.time() - t0) * 1000),
    }


def run(coro):
    """给同步调用方（脚本 / 非 async 路由）用的小包装。"""
    return asyncio.run(coro)
