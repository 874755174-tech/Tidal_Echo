#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 · 模型网关（上半）—— 供应商允许列表 + 适配层
==========================================================================

## 这个文件解决什么

拍板第 1 项（2026-09-13）= **甲：房子直连，key 在房子**。
本文件是这条裁决的**上半**：把"选哪个模型"从身体手里收回到房子手里。

    ❌ 之前：模型链在 api_loop.config.json —— 换身体必然跟着换配置，key 也在身体手上
    ✅ 之后：`PROVIDERS` 字典是**唯一真相源**；前端/身体只传 `provider_id` + `model`

## 🔴 三条铁律（对应规划 §4.1）

| 铁律 | 本文件怎么保证 |
|---|---|
| ① 前端只能选允许列表里的 | `catalog()` 只回 `id/label/models/available/key_masked`，**没有 endpoint、没有 key** |
| ② key 不出服务端 | key 只在 `_view()` 里从 env 读出来，**只进 headers，绝不进任何返回值** |
| ③ 不接受任意 URL | 调用方传的是 `provider_id`；URL 由本文件的字典 + **服务端 env** 决定。前端传不了 URL |

## 设计：本文件**纯逻辑，不发网络请求**

    providers.py     数据 + 适配 + 流解析        ← 本文件（可离线单测，无 httpx 依赖）
    llm_gateway.py   真正发 HTTP / 收 SSE
    llm_routes.py    /app/ext/* 端点

这样 `tools/providers_check.py` 能**不起服务、不连外网**就断言三种格式的 url/headers/body。

## 内部统一格式（adapt 的输入，永远长这样）

    {"system": "……",
     "messages": [{"role": "user"|"assistant", "content": "……"}],
     "params":   {"temperature": 0.7, "max_tokens": 2000, "top_p": 1.0, "stop": [...]},
     "stream":   True}

上层不感知供应商差异 —— 差异全部在 `adapt()` 里消化。

## 模型名不是写死的

`models` 可以被 `PROVIDER_<NAME>_MODELS`（逗号分隔）整体覆盖。
规划 §4.2 要求"模型名必须用真实调用验证过再写进去" —— 所以：
  · 默认值只是**起点**，不是承诺
  · 加/换模型 = 改一个环境变量，**不需要改代码、不需要重新构建**
  · 真伪由 `POST /app/ext/providers/probe` 验证

## 涉及的环境变量（全部只填 Zeabur 面板，不进 git）

    PROVIDER_<NAME>_KEY      密钥（本文件唯一读它的地方）
    PROVIDER_<NAME>_BASE     endpoint 覆盖（中转站必须填；也可指到本地假上游做测试）
    PROVIDER_<NAME>_MODELS   模型清单覆盖（逗号分隔）
    PROVIDERS_DISABLED       逗号分隔，隐藏某些供应商（如 relay 没配好时）
    LLM_DEFAULT_PROVIDER     没设 settings.provider_id 时的默认供应商
"""

import json
from typing import Any, Optional
from urllib.parse import quote

# 供应商顺序 = 前端下拉框顺序 = "谁先可用就默认谁"的优先级
PROVIDER_ORDER = ["deepseek", "siliconflow", "openai", "anthropic", "gemini", "relay"]

ANTHROPIC_VERSION = "2023-06-01"

# 请求护栏（防止一个 body 就把房子打爆）
MAX_MESSAGES = 200
MAX_TOTAL_CHARS = 200_000
MAX_SYSTEM_CHARS = 40_000

# 供应商没给 max_tokens 时的兜底（Anthropic 必填这个字段）
DEFAULT_MAX_TOKENS = 4096


class ProviderError(Exception):
    """供应商相关的可预期错误。`status` 是准备回给调用方的 HTTP 码。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# 定义表：endpoint 可以被 env 覆盖；key **只在这里被引用（env 名）**，不存值
# ---------------------------------------------------------------------------

_BASE_DEFS: dict = {
    "deepseek": {
        "label": "DeepSeek",
        "endpoint": "https://api.deepseek.com/v1",
        "env_key": "PROVIDER_DEEPSEEK_KEY",
        "format": "openai",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "",
    },
    "siliconflow": {
        "label": "硅基流动",
        "endpoint": "https://api.siliconflow.cn/v1",
        "env_key": "PROVIDER_SILICONFLOW_KEY",
        "format": "openai",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B"],
        "note": "",
    },
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "env_key": "PROVIDER_OPENAI_KEY",
        "format": "openai",
        "models": ["gpt-5", "gpt-5-mini"],
        "note": "",
    },
    "anthropic": {
        "label": "Claude（官方）",
        "endpoint": "https://api.anthropic.com/v1",
        "env_key": "PROVIDER_ANTHROPIC_KEY",
        "format": "anthropic",
        "models": ["claude-opus-4-6", "claude-sonnet-4-6"],
        "note": "",
    },
    "gemini": {
        "label": "Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta",
        "env_key": "PROVIDER_GEMINI_KEY",
        "format": "gemini",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "note": "",
    },
    # 中转站：OpenAI 兼容但 base 不固定 → **必须**填 PROVIDER_RELAY_BASE
    "relay": {
        "label": "中转站",
        "endpoint": "",                       # ← 故意为空：没配就不可用
        "env_key": "PROVIDER_RELAY_KEY",
        "format": "openai",
        "models": [],                         # ← 故意为空：站点模型名不可猜
        "note": "需要在服务端配置中转站地址与模型清单",
    },
}


def _env(name: str) -> str:
    import os
    return (os.environ.get(name) or "").strip()


def _disabled_ids() -> set:
    raw = _env("PROVIDERS_DISABLED")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _view(provider_id: str) -> dict:
    """把 env 叠加到定义表上，得到这个供应商的**完整**视图（含 key）。

    🔴 这个 dict 含 key，**只能内部用**。任何回给外部的东西都要过 `_public()`。
    """
    pid = (provider_id or "").strip().lower()
    d = _BASE_DEFS.get(pid)
    if not d:
        raise ProviderError(
            "unknown_provider",
            f"未知供应商 {provider_id!r}（允许列表：{PROVIDER_ORDER}）", 400,
        )

    up = pid.upper()
    base_env = _env(f"PROVIDER_{up}_BASE")
    endpoint = (base_env or d["endpoint"]).rstrip("/")

    models_env = _env(f"PROVIDER_{up}_MODELS")
    if models_env:
        models = [m.strip() for m in models_env.split(",") if m.strip()]
    else:
        models = list(d["models"])

    return {
        "id": pid,
        "label": d["label"],
        "format": d["format"],
        "endpoint": endpoint,
        "base_from_env": bool(base_env),
        "key": _env(d["env_key"]),
        "env_key": d["env_key"],
        "models": models,
        "note": d["note"],
    }


def mask_key(key: str) -> Optional[str]:
    """只回"能认出来是哪把"的最小信息。**绝不返回完整 key。**

    `sk-abcdefghijklmn` → `sk-ab…klmn`
    """
    k = (key or "").strip()
    if not k:
        return None
    if len(k) <= 8:
        return "…" + k[-2:] if len(k) > 2 else "…"
    return f"{k[:5]}…{k[-4:]}"


def is_available(p: dict) -> bool:
    """密钥在 + endpoint 有值 + 模型清单非空 → 才算可用。

    三条缺一不可：只有 key 没 base 会打到空 URL；有 key 没模型清单
    前端下拉框是空的。**宁可显示"未配置"，也不要让用户点了报错。**
    """
    return bool(p["key"]) and bool(p["endpoint"]) and bool(p["models"])


def default_model(p: dict) -> Optional[str]:
    return p["models"][0] if p["models"] else None


def _public(p: dict) -> dict:
    """🔴 白名单式投影 —— 新加字段必须**显式**列出来，默认不外泄。"""
    return {
        "id": p["id"],
        "label": p["label"],
        "format": p["format"],
        "models": list(p["models"]),
        "default_model": default_model(p),
        "available": is_available(p),
        "key_masked": mask_key(p["key"]),
        "needs_base": not p["endpoint"],
        "note": p["note"],
    }


def catalog() -> list:
    """给前端的允许列表。**没有 URL、没有 key、没有 env 名。**"""
    disabled = _disabled_ids()
    out = []
    for pid in PROVIDER_ORDER:
        if pid in disabled:
            continue
        out.append(_public(_view(pid)))
    return out


def default_provider() -> Optional[str]:
    """没选供应商时用哪个：env 指定 → 否则第一个可用的。"""
    want = _env("LLM_DEFAULT_PROVIDER").lower()
    if want and want in _BASE_DEFS and want not in _disabled_ids():
        p = _view(want)
        if is_available(p):
            return want
    for pid in PROVIDER_ORDER:
        if pid in _disabled_ids():
            continue
        if is_available(_view(pid)):
            return pid
    return None


def resolve(provider_id: Optional[str], model: Optional[str]) -> tuple:
    """解析出 (供应商视图, 模型名)。**严格**：不在允许列表里一律拒绝。

    这是"③ 不接受任意 URL"的落点：调用方给的是 id，不是地址。
    """
    pid = (provider_id or "").strip() or (default_provider() or "")
    if not pid:
        raise ProviderError(
            "no_provider",
            "没有任何可用供应商：请在 Zeabur 配置 PROVIDER_*_KEY（中转站还要 "
            "PROVIDER_RELAY_BASE + PROVIDER_RELAY_MODELS）", 400,
        )
    if pid.lower() in _disabled_ids():
        raise ProviderError("provider_disabled", f"供应商 {pid} 已被 PROVIDERS_DISABLED 关闭", 400)

    p = _view(pid)                      # 未知 id → unknown_provider

    if not p["key"]:
        raise ProviderError(
            "provider_unavailable",
            f"{p['label']} 没配密钥（{p['env_key']} 为空）", 400,
        )
    if not p["endpoint"]:
        raise ProviderError(
            "provider_unavailable", f"{p['label']} 没配 base（PROVIDER_{p['id'].upper()}_BASE）", 400,
        )
    if not p["models"]:
        raise ProviderError(
            "provider_unavailable",
            f"{p['label']} 没有模型清单（PROVIDER_{p['id'].upper()}_MODELS 为空）", 400,
        )

    mid = (model or "").strip() or default_model(p)
    if mid not in p["models"]:
        raise ProviderError(
            "model_not_allowed",
            f"模型 {mid!r} 不在 {p['id']} 的允许列表里：{p['models']}", 400,
        )
    return p, mid


def validate_choice(settings: dict) -> None:
    """给 settings 写入做**宽松**校验：只在"选了供应商 + 有模型清单"时才查模型名。

    ⚠️ 故意宽松：没选供应商时不动（P0 阶段的行为原样保留）。
       —— 严格校验留给 `resolve()`，那是真正调用模型的地方。
    """
    pid = (settings.get("provider_id") or "").strip()
    if not pid:
        return
    if pid.lower() in _disabled_ids():
        raise ValueError(f"供应商 {pid} 已被 PROVIDERS_DISABLED 关闭")
    p = _view(pid)                      # 未知 id → ProviderError（继承 Exception）
    mid = (settings.get("model_id") or "").strip()
    if mid and p["models"] and mid not in p["models"]:
        raise ValueError(f"model_id {mid!r} 不在 {pid} 的允许列表里：{p['models']}")


# ---------------------------------------------------------------------------
# 请求归一化：把外部 body 收敛成内部统一格式
# ---------------------------------------------------------------------------

def _flatten_content(v: Any) -> str:
    """`content` 允许是字符串，也允许是 [{"type":"text","text":"..."}]（多模态形态）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for item in v:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    raise ProviderError("bad_content", f"content 类型不支持：{type(v).__name__}", 400)


def _num(params: dict, key: str, lo: float, hi: float) -> None:
    v = params.get(key)
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProviderError("bad_param", f"{key} 必须是数字", 400)
    if not (lo <= float(v) <= hi):
        raise ProviderError("bad_param", f"{key} 必须在 {lo}~{hi}", 400)


def normalize_request(body: Any) -> dict:
    """外部 body（OpenAI 风格）→ 内部统一格式。

    只认 `system/user/assistant` 三种 role。`tool` / `function` 一律拒绝 ——
    网关 v1 **不猜**：将来要支持工具调用时，得先把"工具结果怎么表达"设计清楚，
    而不是先静默丢掉一半上下文（丢一半的上下文比报错危险得多）。
    """
    if not isinstance(body, dict):
        raise ProviderError("bad_request", "body 必须是 JSON 对象", 400)

    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ProviderError("bad_request", "messages 必须是非空数组", 400)
    if len(raw) > MAX_MESSAGES:
        raise ProviderError("too_many_messages", f"messages 最多 {MAX_MESSAGES} 条", 400)

    system_parts, msgs = [], []
    total = 0
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ProviderError("bad_request", f"messages[{i}] 不是对象", 400)
        role = str(m.get("role") or "").strip().lower()
        text = _flatten_content(m.get("content"))
        if role == "system":
            system_parts.append(text)
        elif role in ("user", "assistant"):
            msgs.append({"role": role, "content": text})
        else:
            raise ProviderError(
                "unsupported_role",
                f"messages[{i}] 的 role={role!r} 不支持（只支持 system/user/assistant）", 400,
            )
        total += len(text)

    if not msgs:
        raise ProviderError("bad_request", "至少要有一条 user / assistant 消息", 400)
    if total > MAX_TOTAL_CHARS:
        raise ProviderError("too_large", f"消息总长度超过 {MAX_TOTAL_CHARS} 字符", 400)

    system = "\n\n".join(s for s in system_parts if s)
    if len(system) > MAX_SYSTEM_CHARS:
        raise ProviderError("too_large", f"system 超过 {MAX_SYSTEM_CHARS} 字符", 400)

    params: dict = {}
    for k in ("temperature", "top_p", "max_tokens"):
        v = body.get(k)
        if v is not None:
            params[k] = v
    _num(params, "temperature", 0.0, 2.0)
    _num(params, "top_p", 0.0, 1.0)
    if "max_tokens" in params:
        v = params["max_tokens"]
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                params["max_tokens"] = int(v)
            except Exception:
                raise ProviderError("bad_param", "max_tokens 必须是整数", 400)
        if not (1 <= params["max_tokens"] <= 200_000):
            raise ProviderError("bad_param", "max_tokens 必须在 1~200000", 400)

    stop = body.get("stop")
    if stop is not None:
        if isinstance(stop, str):
            params["stop"] = [stop]
        elif isinstance(stop, list) and all(isinstance(s, str) for s in stop):
            params["stop"] = stop[:4]
        else:
            raise ProviderError("bad_param", "stop 必须是字符串或字符串数组", 400)

    return {
        "system": system,
        "messages": msgs,
        "params": params,
        "stream": bool(body.get("stream", True)),
    }


def _merge_same_role(msgs: list) -> list:
    """把连续同角色的消息合并成一条。

    🔴 Anthropic 与 Gemini 都**要求角色交替**。不合并的话，多段 system /
       连续两条 user 会被上游拒掉（400），而 OpenAI 那边却毫发无伤 ——
       于是表现为"只有某几个供应商坏"。合并是对所有格式都安全的最小处理。
    """
    out: list = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = (out[-1]["content"] + "\n\n" + m["content"]).strip()
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


# ---------------------------------------------------------------------------
# 适配层：内部格式 → 各供应商的真实 (url, headers, body)
# ---------------------------------------------------------------------------

def adapt(provider_id: str, model: str, req: dict) -> tuple:
    """返回 `(url, headers, body)`。**供应商的全部差异都在这里消化。**

    ⚠️ 本函数不发请求（纯函数）→ 可以离线断言 url/headers/body 的每一个字段。
    """
    p = _view(provider_id)
    if not p["key"]:
        raise ProviderError("provider_unavailable", f"{p['label']} 没配密钥", 400)

    key = p["key"]
    fmt = p["format"]
    params = dict(req.get("params") or {})
    messages = req.get("messages") or []
    system = (req.get("system") or "").strip()
    stream = bool(req.get("stream", True))
    max_tokens = params.get("max_tokens") or DEFAULT_MAX_TOKENS
    stop = params.get("stop") or []
    temp = params.get("temperature")
    top_p = params.get("top_p")

    # ── ① OpenAI 兼容：DeepSeek / SiliconFlow / OpenAI / 中转站 / 本地 vLLM ──
    if fmt == "openai":
        url = p["endpoint"] + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body = {"model": model, "messages": msgs, "stream": stream}
        body["max_tokens"] = max_tokens
        if temp is not None:
            body["temperature"] = temp
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop"] = stop
        return url, headers, body

    # ── ② Anthropic 官方：x-api-key 而非 Bearer；system 是顶层字段 ──
    if fmt == "anthropic":
        url = p["endpoint"] + "/messages"
        headers = {
            "x-api-key": key,                          # ⚠️ 不是 Authorization
            "anthropic-version": ANTHROPIC_VERSION,    # ⚠️ 必须带
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": _merge_same_role(messages),    # ⚠️ messages 里不能有 system
            "max_tokens": max_tokens,                  # ⚠️ 必填
        }
        if system:
            body["system"] = system                    # ⚠️ 顶层
        if stream:
            body["stream"] = True
        if temp is not None:
            body["temperature"] = temp
        if top_p is not None:
            body["top_p"] = top_p
        if stop:
            body["stop_sequences"] = stop              # ⚠️ 字段名不同
        return url, headers, body

    # ── ③ Gemini：key 在 query；content → parts；assistant → "model" ──
    if fmt == "gemini":
        verb = "streamGenerateContent" if stream else "generateContent"
        url = f"{p['endpoint']}/models/{quote(model, safe='')}:{verb}"
        sep = "&" if "?" in url else "?"
        url += f"{sep}key={quote(key, safe='')}"
        if stream:
            url += "&alt=sse"
        headers = {"Content-Type": "application/json"}
        body: dict = {}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["contents"] = [
            {"role": "model" if m["role"] == "assistant" else "user",   # ⚠️ 助手叫 model
             "parts": [{"text": m["content"]}]}                          # ⚠️ parts 不是 content
            for m in _merge_same_role(messages)
        ]
        gen: dict = {}
        if temp is not None:
            gen["temperature"] = temp
        if top_p is not None:
            gen["topP"] = top_p
        if params.get("max_tokens") is not None:
            gen["maxOutputTokens"] = max_tokens        # ⚠️ 名字不同
        if stop:
            gen["stopSequences"] = stop
        if gen:
            body["generationConfig"] = gen
        return url, headers, body

    raise ProviderError("bad_format", f"未知格式 {fmt!r}", 500)


# ---------------------------------------------------------------------------
# 反向：各供应商的流 / 完整响应 → 归一化文本
# ---------------------------------------------------------------------------

def parse_stream(provider_id: str, payload: str) -> Optional[str]:
    """吃一行 SSE 的 `data:` **后面那部分**，吐一段文本增量（没有就 None）。

    静默忽略认不出的行（`[DONE]` / `ping` / 心跳 / 空行）—— 流里噪音很多，
    逐行报错会让一整条回复因为一个心跳断掉。
    """
    raw = (payload or "").strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if isinstance(obj, dict) and obj.get("error"):
        raise _stream_error(obj["error"])
    fmt = _BASE_DEFS[provider_id]["format"]

    if fmt == "openai":
        ch = obj.get("choices") or []
        if not ch:
            return None
        delta = ch[0].get("delta") or {}
        return delta.get("content") or None

    if fmt == "anthropic":
        typ = obj.get("type")
        if typ == "error":
            raise _stream_error(obj.get("error") or obj)
        if typ == "content_block_delta":
            d = obj.get("delta") or {}
            if d.get("type") in ("text_delta", None):
                return d.get("text") or None
        return None

    if fmt == "gemini":
        cands = obj.get("candidates") or []
        if not cands:
            return None
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        txt = "".join(str(x.get("text") or "") for x in parts if isinstance(x, dict))
        return txt or None

    return None


def _stream_error(err: Any) -> ProviderError:
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("type") or err)
    else:
        msg = str(err)
    return ProviderError("upstream_error", msg, 502)


def parse_complete(provider_id: str, obj: Any) -> str:
    """非流式响应 → 归一化文本。"""
    fmt = _BASE_DEFS[provider_id]["format"]
    if not isinstance(obj, dict):
        return ""
    if obj.get("error"):
        raise _stream_error(obj["error"])

    if fmt == "openai":
        ch = obj.get("choices") or []
        if not ch:
            return ""
        return ((ch[0].get("message") or {}).get("content")) or ""

    if fmt == "anthropic":
        blocks = obj.get("content") or []
        return "".join(str(b.get("text") or "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")

    if fmt == "gemini":
        cands = obj.get("candidates") or []
        if not cands:
            return ""
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        return "".join(str(x.get("text") or "") for x in parts if isinstance(x, dict))

    return ""


def describe_http_error(status: int, body: str) -> str:
    """把上游的 HTTP 错误翻译成**人能看懂 + 能行动**的一句话。"""
    detail = ""
    try:
        obj = json.loads(body or "{}")
        e = obj.get("error")
        if isinstance(e, dict):
            detail = str(e.get("message") or "")
        elif isinstance(e, str):
            detail = e
        elif obj.get("message"):
            detail = str(obj["message"])
    except Exception:
        detail = (body or "").strip()[:200]

    head = {
        400: "上游说请求不合法（多半是这个供应商不吃这组参数）",
        401: "密钥无效或已过期",
        403: "密钥没有访问这个模型的权限",
        404: "端点或模型名不对（检查 PROVIDER_*_BASE 与模型清单）",
        413: "请求体太大",
        429: "触发限流/余额不足",
        500: "上游服务内部错误",
        502: "上游网关错误",
        503: "上游暂时不可用",
        529: "上游过载",
    }.get(status, f"上游返回 {status}")
    return f"{head}：{detail}" if detail else head


def summary_line() -> str:
    """给启动日志用的一行摘要（**不含 key**）。"""
    ok = [p["id"] for p in catalog() if p["available"]]
    return f"可用 {len(ok)}/{len(catalog())}：{','.join(ok) if ok else '无'}"
