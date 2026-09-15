#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1 模型网关验收 —— 允许列表 / 三格式适配 / 真 HTTP 通路 / 端点 / 红线
==========================================================================

## 这份脚本的核心武器：本地假上游

三种供应商格式（openai / anthropic / gemini）如果只能靠真 key 验证，
那"改一行适配代码"的代价就是：花钱 + 等网络 + 结果还是概率性的。

所以这里起一个**本地假上游**（`127.0.0.1:8794`），把 `PROVIDER_*_BASE`
指过去 —— 于是：

  · 三种格式的**真 HTTP 往返**（包括 SSE 逐字流）全部可离线跑
  · 假上游会把**收到的 headers / body 记下来**，用来断言"我们真的发对了"
  · 401 / 404 / 连不上 这些错误路径也能**稳定复现**（真实上游做不到）

真 key 只在"验模型名拼写"时才需要 —— 那是 `POST /app/ext/providers/probe`
的活，跑在真实环境里。

## 覆盖清单

  A. 纯逻辑（不起服务、不联网）
     1.  catalog 投影**不含** URL / key / 环境变量名
     2.  mask_key 只回前后几位
     3.  未配 key → available=False、resolve 报 provider_unavailable
     4.  未知供应商 → unknown_provider
     5.  模型不在允许列表 → model_not_allowed
     6.  PROVIDERS_DISABLED 能隐藏供应商
     7.  PROVIDER_<X>_MODELS 能整体覆盖模型清单
     8.  PROVIDER_<X>_BASE 能整体覆盖 endpoint
     9.  OpenAI 适配：Bearer / system 进 messages / 参数名一致
     10. Anthropic 适配：x-api-key / anthropic-version / system 顶层 / max_tokens 必填 / stop_sequences
     11. 🔴 Anthropic 连续同角色被合并（上游要求交替）
     12. Gemini 适配：key 在 query / parts / assistant→model / generationConfig
     13. Gemini 流式与非流式端点名不同
     14. 流解析三格式各吐对的文本
     15. 流里的噪音（[DONE] / ping / 非 JSON）被静默忽略
     16. 上游 error 事件 → 抛出而不是静默吞掉
     17. normalize：tool/function role 被拒（不静默丢上下文）
     18. normalize：temperature / top_p / max_tokens / stop 校验
     19. normalize：空 messages / 超长 / 超条数被拒
     20. validate_choice：选了供应商但模型不配对 → 拒
     21. validate_choice：没选供应商 → 放行（P0 行为不变）

  B. 真 HTTP 往返（打本地假上游）
     22. OpenAI 流式：逐段收到 + 拼接正确 + 段数对
     23. Anthropic 流式：同上
     24. Gemini 流式：同上
     25. 三种格式的非流式 complete
     26. 🔴 假上游收到的 Authorization 确实是我们的 key
     27. 🔴 假上游收到的 x-api-key 同上（Anthropic）
     28. 🔴 假上游收到的 body 里 system 在正确位置
     29. 上游 401 → GatewayError，人话里含"密钥无效"
     30. 上游 404 → 人话里含"模型名"
     31. 连不上（端口 1）→ GatewayError network，不是崩
     32. probe 成功 → ok=True + sample
     33. probe 全模型（all）→ verified 列表

  C. 房子端点（起真实 relay）
     34. 🔴 /app/ext/providers 无密钥 → 401
     35. 🔴 /app/ext/llm/chat 无密钥 → 401
     36. 🔴 /app/ext/llm/complete 无密钥 → 401
     37. 🔴 /app/ext/providers/probe 无密钥 → 401
     38. 🔴 带密钥的 /providers 响应里**没有** URL、没有 key、没有环境变量名
     39. /providers 里 relay.available=True（因为配了假上游）
     40. 非流式端点走通 → OpenAI 兼容形状
     41. 🔴 流式端点走通 → SSE 逐段 + 以 [DONE] 收尾
     42. 未知供应商 → 400（流没开起来，干净的 4xx）
     43. 模型不在允许列表 → 400
     44. PUT settings 存 provider_id + model_id → 落库
     45. PUT settings 模型不配对 → 400
     46. GET settings 读得回 provider_id
     47. probe 端点 200
     48. 🔴 回归：P0 的 /app/ext/me 仍 200
     49. 🔴 回归：原版 /app/history 仍 200

  C2. 🆕 OpenAI 路径别名（P3 通车前置 · 2026-09-15）
     51. 🔴 两条别名路径无密钥 → 401（fail-closed）
     52. 🔴 stream=True → SSE 逐段 + 以 [DONE] 收尾（分流到流式）
     53. 🔴 stream=False → JSON 非流式（分流到非流式）
     54. 🔴 不带 stream → 非流式（OpenAI 语义：默认不流）
     55. 🔴 少一层 v1 的别名也走通（LLM_API_BASE 填错一层有救）
     56. 🔴 开流前错误 → 干净 400（不是塞进流里）
     57. 🔴 通车仿真：照身体（api_loop.py）的形状打一遍 + 参数没被吃掉

  D. 红线
     50. git diff e7c9bf5 -- backend/ examples/ channel/ 为空

  E. 🆕 P1 收尾（2026-09-15）：settings 的参数真的下发 + effort 翻译
     58. effort 被 normalize 带进内部格式
     59. 🔴 没配 EFFORT_PARAM → 上游 body 里没有 effort（默认不发，不猜字段名）
     60. 配了 EFFORT_PARAM → 按配置的字段名发出、值原样
     61. EFFORT_MAP 生效（xhigh → high）
     62. EFFORT_MAP 的值可以是嵌套对象（贴 thinking:{...} 那类上游）
     63. 🔴 EFFORT_MAP 写坏 → 按原值发，不抛错（可选项不该拖垮请求）
     64. effort 非字符串 → bad_param（干净 400，不是 500）
     64b. 🔴 EFFORT_VALUES 覆盖设置页全部档位（含 xhigh）
     65. 🔴 settings 里的参数在 body 没传时被补进请求（原来"存了＝没存"）
     66. 🔴 body 显式传的优先（temperature=0 不被 settings 顶掉）
     67. 🔴 effort 能落库（xhigh 合法）但没配 EFFORT_PARAM 时照样不下发
     68. 🔴 body 不带 model → 用 settings 里选的那个模型

跑法（项目 venv）：
    .venv\\Scripts\\python.exe tools\\providers_check.py
结果写 tools/providers_report.txt
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"
BACKEND = REPO / "backend"

PROJECT_VENV = REPO / ".venv" / "Scripts" / "python.exe"
PY = str(PROJECT_VENV) if PROJECT_VENV.exists() else sys.executable

try:
    import fastapi  # noqa: F401
except Exception:
    if Path(PY).exists() and os.path.abspath(PY) != os.path.abspath(sys.executable):
        sys.exit(subprocess.call([PY, os.path.abspath(__file__)] + sys.argv[1:]))
    raise

sys.path.insert(0, str(DEPLOY))

SECRET = "test-secret-providers-0123456789"
PORT = 8793          # 房子
MOCK_PORT = 8794     # 假上游
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
BASE_COMMIT = "e7c9bf5"
MODEL = "mock-model"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def wait_port(port: int, path: str = "/healthz", timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def req(url: str, *, method="GET", token=None, body=None, timeout=15):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "{}")
            except Exception:
                return resp.status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def req_sse(url: str, *, token=None, body=None, timeout=30):
    """读一整条 SSE 流，返回 (status, [帧文本...])。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method="POST")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, [e.read().decode("utf-8", "replace")]
    lines = []
    with resp:
        for raw in resp:
            lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
    return resp.status, lines


# ══════════════════════════════════════════════════════════════════════════
# 假上游：三种格式都演一遍，并把收到的请求记下来
# ══════════════════════════════════════════════════════════════════════════

MOCK_TEXT = "答案在风里"
seen: dict = {}


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):        # 别把噪音打进报告
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _send(self, code: int, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, frames):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for f in frames:
            self.wfile.write(f.encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.005)

    def do_GET(self):
        if self.path.startswith("/__ping"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path
        body = self._read_body()
        seen["path"] = path
        seen["auth"] = self.headers.get("Authorization")
        seen["x_api_key"] = self.headers.get("x-api-key")
        seen["anthropic_version"] = self.headers.get("anthropic-version")
        seen["body"] = body

        # ── 错误路径（用来稳定复现错误翻译）──
        if "/bad401" in path:
            return self._send(401, {"error": {"message": "invalid api key"}})
        if "/bad404" in path:
            return self._send(404, {"error": {"message": "model not found"}})
        if "/bad500" in path:
            return self._send(500, {"error": {"message": "boom"}})

        model = body.get("model") or "unknown"

        # ── Gemini（路径里带动词；**model 在 URL 里，不在 body**）──
        # ⚠️ 动词大小写不同：流式是 streamGenerateContent、非流式是 generateContent
        #    → 一律 lowercase 再判（上一版就是被首字母骗了，非流式探测一直 404）
        if "generatecontent" in path.lower():
            model = path.split("/models/")[-1].split(":")[0] or model
            text = f"[{model}] {MOCK_TEXT}"
            if "streamgeneratecontent" in path.lower():
                chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
                frames = [
                    "data: " + json.dumps(
                        {"candidates": [{"content": {"parts": [{"text": c}]}}]},
                        ensure_ascii=False) + "\n\n"
                    for c in chunks
                ]
                # 真实的 Gemini 会在末帧带 usageMetadata → 顺手验一下能不能捡到
                frames.append("data: " + json.dumps(
                    {"candidates": [{"content": {"parts": []}}],
                     "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 3}},
                    ensure_ascii=False) + "\n\n")
                return self._sse(frames)
            return self._send(200, {
                "candidates": [{"content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 3},
            })

        # ── Anthropic ──
        if path.rstrip("/").endswith("/messages"):
            text = f"[{model}] {MOCK_TEXT}"
            if body.get("stream"):
                chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
                frames = [
                    # message_start 带 usage（真实 Anthropic 就这样）
                    "event: message_start\ndata: " + json.dumps(
                        {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
                        ensure_ascii=False) + "\n\n",
                    "event: ping\ndata: {\"type\":\"ping\"}\n\n",
                ]
                frames += [
                    "event: content_block_delta\ndata: " + json.dumps(
                        {"type": "content_block_delta",
                         "delta": {"type": "text_delta", "text": c}}, ensure_ascii=False) + "\n\n"
                    for c in chunks
                ]
                frames.append("event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")
                return self._sse(frames)
            return self._send(200, {
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 1, "output_tokens": 3},
            })

        # ── OpenAI 兼容 ──
        if path.rstrip("/").endswith("/chat/completions"):
            text = f"[{model}] {MOCK_TEXT}"
            if body.get("stream"):
                chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
                frames = [
                    "data: " + json.dumps(
                        {"choices": [{"delta": {"content": c}}]}, ensure_ascii=False) + "\n\n"
                    for c in chunks
                ]
                # 末帧带 usage（choices 为空，不该被当成正文）
                frames.append("data: " + json.dumps(
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 3}},
                    ensure_ascii=False) + "\n\n")
                frames.append("data: [DONE]\n\n")
                return self._sse(frames)
            return self._send(200, {
                "choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 3},
            })

        return self._send(404, {"error": {"message": f"mock 不认这个路径：{path}"}})


def start_mock() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ══════════════════════════════════════════════════════════════════════════
# 各类断言
# ══════════════════════════════════════════════════════════════════════════

def part_a() -> None:
    """纯逻辑：不起服务、不联网。"""
    import app_ext.providers as P

    # 1) 未配任何东西时的 catalog
    cat = P.catalog()
    blob = json.dumps(cat, ensure_ascii=False)
    chk("① catalog 不含 endpoint（http/https）", "http" not in blob and "https" not in blob,
        blob[:120])
    chk("① catalog 不含环境变量名", "PROVIDER_" not in blob, "")
    chk("① 未配 key 的供应商 available=False",
        all(not p["available"] for p in cat), str([(p["id"], p["available"]) for p in cat]))
    chk("① 未配 key 时 key_masked 为 None",
        all(p["key_masked"] is None for p in cat), "")

    # 2) mask_key 不吐完整 key
    k = "sk-abcdefghijklmnop"
    m = P.mask_key(k)
    chk("② mask_key 只回头尾", m and k not in m and m.startswith("sk-ab") and m.endswith("mnop"), m)
    chk("② 空 key → None", P.mask_key("") is None, "")

    # 3-5) resolve 的拒绝路径
    for label, fn, code in [
        ("③ 未配 key → provider_unavailable", lambda: P.resolve("deepseek", None), "provider_unavailable"),
        ("④ 未知供应商 → unknown_provider", lambda: P.resolve("nope", None), "unknown_provider"),
    ]:
        try:
            fn()
            chk(label, False, "没被挡住")
        except P.ProviderError as e:
            chk(label, e.code == code, f"{e.code}: {e.message[:60]}")
    try:
        P.resolve("deepseek", "gpt-5")
        chk("⑤ 模型不在允许列表 → model_not_allowed", False, "没被挡住")
    except P.ProviderError as e:
        chk("⑤ 模型不在允许列表 → model_not_allowed",
            e.code in ("model_not_allowed", "provider_unavailable"),
            f"{e.code}（未配 key 时先报 provider_unavailable，也是对的）")

    # 6) PROVIDERS_DISABLED
    os.environ["PROVIDERS_DISABLED"] = "gemini,relay"
    ids = [p["id"] for p in P.catalog()]
    chk("⑥ PROVIDERS_DISABLED 能隐藏供应商", "gemini" not in ids and "relay" not in ids, str(ids))
    os.environ["PROVIDERS_DISABLED"] = ""

    # 配齐假上游（后续都要用）
    os.environ.update({
        "PROVIDER_DEEPSEEK_KEY": "sk-ds-000000000000",
        "PROVIDER_ANTHROPIC_KEY": "sk-ant-111111111111",
        "PROVIDER_GEMINI_KEY": "AIza222222222222",
        "PROVIDER_RELAY_KEY": "sk-relay-333333333333",
        "PROVIDER_RELAY_BASE": MOCK_BASE + "/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
    })

    # 7) MODELS 覆盖
    os.environ["PROVIDER_DEEPSEEK_MODELS"] = "a-model, b-model"
    ds = [p for p in P.catalog() if p["id"] == "deepseek"][0]
    chk("⑦ PROVIDER_<X>_MODELS 覆盖模型清单",
        ds["models"] == ["a-model", "b-model"], str(ds["models"]))
    chk("⑦ 覆盖后 default_model = 第一个", ds["default_model"] == "a-model", str(ds["default_model"]))

    # 8) BASE 覆盖
    p = P._view("relay")
    chk("⑧ PROVIDER_<X>_BASE 覆盖 endpoint",
        p["endpoint"] == MOCK_BASE + "/v1" and p["base_from_env"], p["endpoint"])

    # 9-13) 适配层
    r = P.normalize_request({
        "messages": [{"role": "system", "content": "你是汐"},
                     {"role": "user", "content": "一"},
                     {"role": "user", "content": "二"},
                     {"role": "assistant", "content": "三"}],
        "temperature": 0.7, "max_tokens": 2000, "stop": "END", "stream": True,
    })
    chk("⑨ normalize 把 system 抽出来（不进 messages）",
        r["system"] == "你是汐" and len(r["messages"]) == 3, str(r["system"]))

    u, h, b = P.adapt("deepseek", "a-model", r)
    chk("⑨ OpenAI：Bearer + system 回到 messages 首位 + 参数名一致",
        h["Authorization"].startswith("Bearer ") and b["messages"][0]["role"] == "system"
        and b["temperature"] == 0.7 and b["max_tokens"] == 2000 and b["stop"] == ["END"],
        u)
    chk("⑨ OpenAI：url 以 /chat/completions 结尾",
        u.endswith("/v1/chat/completions"), u)

    u, h, b = P.adapt("anthropic", "claude-opus-4-6", r)
    chk("⑩ Anthropic：x-api-key（不是 Bearer）", "x-api-key" in h and "Authorization" not in h, str(list(h)))
    chk("⑩ Anthropic：带 anthropic-version", h.get("anthropic-version") == P.ANTHROPIC_VERSION, str(h))
    chk("⑩ Anthropic：system 是顶层字段、不在 messages 里",
        b.get("system") == "你是汐" and all(m["role"] != "system" for m in b["messages"]), "")
    chk("⑩ Anthropic：max_tokens 必填且带上", b.get("max_tokens") == 2000, str(b.get("max_tokens")))
    chk("⑩ Anthropic：stop → stop_sequences",
        b.get("stop_sequences") == ["END"], str(b.get("stop_sequences")))
    chk("⑪ 🔴 Anthropic：连续同角色被合并（一+二）",
        len(b["messages"]) == 2 and b["messages"][0]["content"] == "一\n\n二",
        str([m["content"] for m in b["messages"]]))
    chk("⑩ Anthropic：url 以 /messages 结尾（不是 /chat/completions）",
        u.endswith("/v1/messages"), u)

    u, h, b = P.adapt("gemini", "gemini-2.5-flash", r)
    chk("⑫ Gemini：key 在 query 里，header 里没有 key",
        "key=" in u and not any("key" in k.lower() and k.lower() != "content-type" for k in h),
        u)
    chk("⑫ Gemini：assistant → role=model", b["contents"][1]["role"] == "model",
        str([c["role"] for c in b["contents"]]))
    chk("⑫ Gemini：内容在 parts 里", b["contents"][0]["parts"][0]["text"] == "一\n\n二", "")
    chk("⑫ Gemini：max_tokens → maxOutputTokens / generationConfig",
        b["generationConfig"]["maxOutputTokens"] == 2000
        and b["generationConfig"]["stopSequences"] == ["END"], str(b.get("generationConfig")))
    chk("⑫ Gemini：system → systemInstruction",
        b["systemInstruction"]["parts"][0]["text"] == "你是汐", "")
    u2 = P.adapt("gemini", "gemini-2.5-flash", {**r, "stream": False})[0]
    chk("⑬ Gemini：流式/非流式端点名不同",
        ":streamGenerateContent?" in u and ":generateContent?" in u2 and "alt=sse" in u, u2)

    # 14-16) 流解析
    chk("⑭ openai 流解析",
        P.parse_stream("deepseek", '{"choices":[{"delta":{"content":"你"}}]}') == "你", "")
    chk("⑭ anthropic 流解析",
        P.parse_stream("anthropic",
                       '{"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}') == "好", "")
    chk("⑭ gemini 流解析",
        P.parse_stream("gemini", '{"candidates":[{"content":{"parts":[{"text":"呀"}]}}]}') == "呀", "")
    chk("⑮ 噪音被静默忽略（[DONE]/ping/非 JSON/空）",
        P.parse_stream("deepseek", "[DONE]") is None
        and P.parse_stream("anthropic", '{"type":"ping"}') is None
        and P.parse_stream("deepseek", "not json") is None
        and P.parse_stream("deepseek", "") is None, "")
    try:
        P.parse_stream("deepseek", '{"error":{"message":"rate limited"}}')
        chk("⑯ 上游 error 事件会抛出（不被吞掉）", False, "被吞了")
    except P.ProviderError as e:
        chk("⑯ 上游 error 事件会抛出（不被吞掉）", "rate limited" in e.message, e.message)

    # 17-19) 归一化拒绝
    def rejects(label, payload):
        try:
            P.normalize_request(payload)
            chk(label, False, "没被挡住")
        except P.ProviderError:
            chk(label, True, "")

    rejects("⑰ tool role 被拒（不静默丢上下文）",
            {"messages": [{"role": "tool", "content": "x"}]})
    rejects("⑰ 没听过 role 被拒", {"messages": [{"role": "wizard", "content": "x"}]})
    rejects("⑱ temperature 越界被拒",
            {"messages": [{"role": "user", "content": "x"}], "temperature": 9})
    rejects("⑱ top_p 越界被拒",
            {"messages": [{"role": "user", "content": "x"}], "top_p": 5})
    rejects("⑱ max_tokens 非法被拒",
            {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0})
    rejects("⑱ stop 类型错被拒",
            {"messages": [{"role": "user", "content": "x"}], "stop": 123})
    rejects("⑲ 空 messages 被拒", {"messages": []})
    rejects("⑲ 超条数被拒",
            {"messages": [{"role": "user", "content": "x"}] * (P.MAX_MESSAGES + 1)})
    rejects("⑲ 超长被拒",
            {"messages": [{"role": "user", "content": "x" * (P.MAX_TOTAL_CHARS + 1)}]})
    ok = P.normalize_request({"messages": [{"role": "user", "content": "x"}], "stream": False})
    chk("⑲ 正常请求 stream=False 被尊重", ok["stream"] is False, "")
    ok2 = P.normalize_request({"messages": [{"role": "user", "content": [{"type": "text", "text": "多模态"}]}]})
    chk("⑲ content 是 parts 数组时被拍平", ok2["messages"][0]["content"] == "多模态", "")

    # 20-21) settings 配对校验
    try:
        P.validate_choice({"provider_id": "deepseek", "model_id": "gpt-5"})
        chk("⑳ 选了供应商但模型不配对 → 拒", False, "没被挡住")
    except ValueError as e:
        chk("⑳ 选了供应商但模型不配对 → 拒", "不在" in str(e), str(e)[:60])
    try:
        P.validate_choice({"provider_id": None, "model_id": "whatever"})
        chk("㉑ 没选供应商 → 放行（P0 行为不变）", True, "")
    except Exception as e:
        chk("㉑ 没选供应商 → 放行（P0 行为不变）", False, str(e))

    # ── ㉑b 迁移：v1 老库 → v2 ─────────────────────────────────────────────
    # 🔴 这条**必须**测：线上那台（P0 已部署）的库就是 v1 形态 —— settings 表
    #    里没有 provider_id。ALTER 那一步写坏了 = 线上炸，而本地新库测不出来。
    import app_ext.schema as S

    mig = Path(tempfile.mkdtemp(prefix="kaelhome_v1_"))
    try:
        db = mig / "v1.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "ts TEXT NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL, "
                     "text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}')")
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, handle TEXT UNIQUE, "
                     "display_name TEXT, secret_hash TEXT NOT NULL, "
                     "role TEXT DEFAULT 'owner', created TEXT NOT NULL)")
        # ← v1 的 settings：**没有 provider_id**
        conn.execute("CREATE TABLE settings (user_id TEXT PRIMARY KEY REFERENCES users(id), "
                     "persona TEXT, model_id TEXT, max_tokens INTEGER, temperature REAL, "
                     "top_p REAL, context_keep INTEGER, context_trigger INTEGER, effort TEXT, "
                     "extra TEXT DEFAULT '{}', updated TEXT NOT NULL)")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                     "title TEXT, since_id INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, "
                     "summary TEXT, archived INTEGER DEFAULT 0, created TEXT NOT NULL, "
                     "updated TEXT NOT NULL)")
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                     "kind TEXT NOT NULL, text TEXT NOT NULL, source_msg INTEGER, "
                     "salience REAL DEFAULT 0.5, created TEXT NOT NULL, last_used TEXT)")
        conn.execute("INSERT INTO users VALUES ('u_owner','lily','Lily','pbkdf2$x$y$z','owner','2026-09-14T00:00:00Z')")
        conn.execute("INSERT INTO settings (user_id, persona, model_id, updated) "
                     "VALUES ('u_owner','老 persona 还在吗','旧的模型','2026-09-14T00:00:00Z')")
        conn.execute("INSERT INTO messages (ts,direction,kind,text,meta) VALUES "
                     "('2026-09-14T00:00:00Z','in','user','老消息','{}')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        class OldRelay:
            DB_PATH = str(db)

        rep = S.ensure_schema(OldRelay())
        chk("㉑b 🔴 v1 老库升级：报出 migrated = settings.provider_id",
            rep["migrated"] == ["settings.provider_id"], str(rep["migrated"]))
        cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(settings)")}
        chk("㉑b 🔴 provider_id 列真的落到库里了", "provider_id" in cols, str(sorted(cols)))
        row = sqlite3.connect(db).execute(
            "SELECT persona, model_id, provider_id FROM settings WHERE user_id='u_owner'").fetchone()
        chk("㉑b 🔴 迁移不丢已有设置（persona / model_id 原样）",
            row[0] == "老 persona 还在吗" and row[1] == "旧的模型" and row[2] is None,
            str(row))
        chk("㉑b 🔴 迁移不动 messages（1 条老消息还在）",
            sqlite3.connect(db).execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1, "")
        rep2 = S.ensure_schema(OldRelay())
        chk("㉑b 🔴 迁移幂等：再跑一次 migrated=[]",
            rep2["migrated"] == [] and rep2["created"] == [], str(rep2["migrated"]))
        chk("㉑b 版本号推到 2",
            sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 2, "")
    finally:
        shutil.rmtree(mig, ignore_errors=True)


def part_a2() -> None:
    """🆕 P1 收尾（2026-09-15）：`effort` → 上游字段。**纯逻辑，直接打 `adapt()`。**

    为什么单独一段而不是塞进 part_a / part_c：
      · 这一段要**临时配 env**（`PROVIDER_RELAY_EFFORT_PARAM`）来模拟"她那家站认的字段名"，
        而 part_c 是**起子进程**跑的（env 走显式 dict，改本进程的 os.environ 传不过去）。
        所以只能用 `P.adapt()` 在进程内断言 —— 它本来就发不出请求，是纯函数。
      · `try/finally` 复原 env：漏了会把 part_b / part_c 的"上游收到的 body"断言污染掉
        （part_c 的 65/67 就是断言"没有 effort 字段"，泄露进去必红）。

    设计要点（也是这几条断言的由来）：`effort` **默认不发**。
    各家站字段名不统一（reasoning_effort / thinking / enable_thinking），
    往不认识的字段上写有的站直接 400 → 通车当天自己把自己搞挂。
    "配了才生效"比"猜一个"安全，这就是 58-63 在守的东西。
    """
    import app_ext.providers as P
    from app_ext import identity as I

    req = P.normalize_request({
        "messages": [{"role": "user", "content": "在吗"}],
        "effort": "xhigh",
    })
    chk("58 effort 被 normalize 带进内部格式（不是被静默丢掉）",
        req["params"].get("effort") == "xhigh", str(req["params"]))

    try:
        # ① 不配 → 不发（默认值就是"不猜"）
        os.environ.pop("PROVIDER_RELAY_EFFORT_PARAM", None)
        os.environ.pop("PROVIDER_RELAY_EFFORT_MAP", None)
        _, _, body = P.adapt("relay", MODEL, req)
        chk("🔴 59 没配 EFFORT_PARAM → 上游 body 里没有 effort（默认不发）",
            not any(k in body for k in ("effort", "reasoning_effort", "thinking", "enable_thinking")),
            json.dumps(body, ensure_ascii=False)[:150])

        # ② 配了字段名 → 原值发
        os.environ["PROVIDER_RELAY_EFFORT_PARAM"] = "reasoning_effort"
        _, _, body = P.adapt("relay", MODEL, req)
        chk("60 配了 EFFORT_PARAM → 按配置的字段名发出（值原样）",
            body.get("reasoning_effort") == "xhigh",
            json.dumps(body, ensure_ascii=False)[:150])

        # ③ 值映射：界面 5 档、上游只认 3 档时的接法
        os.environ["PROVIDER_RELAY_EFFORT_MAP"] = '{"xhigh": "high"}'
        _, _, body = P.adapt("relay", MODEL, req)
        chk("61 EFFORT_MAP 生效（xhigh → high）",
            body.get("reasoning_effort") == "high",
            json.dumps(body, ensure_ascii=False)[:150])

        # ④ 映射的值允许是嵌套对象（贴 thinking:{type,budget_tokens} 这类上游）
        os.environ["PROVIDER_RELAY_EFFORT_MAP"] = '{"xhigh": {"type": "enabled", "budget_tokens": 10000}}'
        _, _, body = P.adapt("relay", MODEL, req)
        chk("62 EFFORT_MAP 的值可以是对象（嵌套字段直接塞进 body）",
            isinstance(body.get("reasoning_effort"), dict)
            and body["reasoning_effort"].get("budget_tokens") == 10000,
            json.dumps(body, ensure_ascii=False)[:150])

        # ⑤ 映射表写坏 → 按原值发（可选项不该拖垮整条请求）
        os.environ["PROVIDER_RELAY_EFFORT_MAP"] = "{这不是 JSON"
        _, _, body = P.adapt("relay", MODEL, req)
        chk("🔴 63 EFFORT_MAP 写坏 → 按原值发，不抛错",
            body.get("reasoning_effort") == "xhigh",
            json.dumps(body, ensure_ascii=False)[:150])
    finally:
        os.environ.pop("PROVIDER_RELAY_EFFORT_PARAM", None)
        os.environ.pop("PROVIDER_RELAY_EFFORT_MAP", None)

    # ⑥ 类型错误要报 400，不是 500
    try:
        P.normalize_request({"messages": [{"role": "user", "content": "x"}], "effort": 3})
        chk("64 effort 非字符串 → bad_param", False, "没被挡住")
    except P.ProviderError as e:
        chk("64 effort 非字符串 → bad_param（干净 400）", e.code == "bad_param", e.code)

    # ⑦ 档位表必须与设置页画出来的按钮一一对应
    #    少一档的后果：点了那个按钮 → PUT 400 → 看着像"没保存"，很难查。
    ui_levels = {"low", "medium", "high", "xhigh", "max"}
    chk("🔴 64b EFFORT_VALUES 覆盖设置页的全部档位（含 xhigh）",
        ui_levels.issubset(I.EFFORT_VALUES),
        f"ui={sorted(ui_levels)} api={sorted(I.EFFORT_VALUES)}")


def part_b() -> None:
    """真 HTTP 往返：打本地假上游。"""
    import asyncio
    import app_ext.providers as P
    import app_ext.llm_gateway as G

    req = P.normalize_request({
        "messages": [{"role": "system", "content": "你是汐"}, {"role": "user", "content": "在吗"}],
        "max_tokens": 100, "stream": True,
    })
    plain = {**req, "stream": False}
    want_relay = f"[{MODEL}] {MOCK_TEXT}"

    def run(coro):
        return asyncio.run(coro)

    # 22) OpenAI 流式
    got = []
    out = run(G.stream_chat("relay", MODEL, req, on_text=got.append))
    chk("㉒ OpenAI 流式：拼接正确", out["text"] == want_relay, out["text"])
    chk("㉒ OpenAI 流式：逐段回调（段数>1）", len(got) > 1 and "".join(got) == want_relay, str(len(got)))
    chk("㉒ OpenAI 流式：usage 被捡到", bool(out["usage"]), str(out["usage"]))
    chk("㉖ 🔴 假上游收到的 Authorization 就是我们的 key",
        seen.get("auth") == f"Bearer {os.environ['PROVIDER_RELAY_KEY']}", str(seen.get("auth")))
    chk("㉘ 🔴 上游收到的 body：system 在 messages 首位、model 正确",
        seen["body"]["messages"][0]["role"] == "system"
        and seen["body"]["model"] == MODEL
        and seen["body"].get("stream") is True, json.dumps(seen["body"], ensure_ascii=False)[:120])

    # 23) Anthropic 流式（假上游同时演 event: 行与 ping，验证噪音过滤）
    os.environ["PROVIDER_ANTHROPIC_BASE"] = MOCK_BASE + "/v1"
    os.environ["PROVIDER_ANTHROPIC_MODELS"] = "claude-mock"
    want_ant = f"[claude-mock] {MOCK_TEXT}"
    got = []
    out = run(G.stream_chat("anthropic", "claude-mock", req, on_text=got.append))
    chk("㉓ Anthropic 流式：拼接正确（event:/ping 被忽略）", out["text"] == want_ant, out["text"])
    chk("㉓ Anthropic 流式：逐段回调", len(got) > 1 and "".join(got) == want_ant, str(len(got)))
    chk("㉗ 🔴 假上游收到的 x-api-key 正确",
        seen.get("x_api_key") == os.environ["PROVIDER_ANTHROPIC_KEY"], str(seen.get("x_api_key")))
    chk("㉗ 🔴 anthropic-version 头带上了",
        seen.get("anthropic_version") == P.ANTHROPIC_VERSION, str(seen.get("anthropic_version")))
    chk("㉘ Anthropic body：system 在顶层、messages 里没有 system",
        seen["body"].get("system") == "你是汐"
        and all(m["role"] != "system" for m in seen["body"]["messages"]), "")

    # 24) Gemini 流式
    os.environ["PROVIDER_GEMINI_BASE"] = MOCK_BASE + "/v1beta"
    os.environ["PROVIDER_GEMINI_MODELS"] = "gemini-mock"
    want_gem = f"[gemini-mock] {MOCK_TEXT}"
    got = []
    out = run(G.stream_chat("gemini", "gemini-mock", req, on_text=got.append))
    chk("㉔ Gemini 流式：拼接正确", out["text"] == want_gem, out["text"])
    chk("㉔ Gemini 流式：逐段回调", len(got) > 1 and "".join(got) == want_gem, str(len(got)))
    chk("㉔ Gemini：路径里带对了动词与 key",
        ":streamGenerateContent" in seen["path"] and "key=" in seen["path"], seen["path"])

    # 25) 三种格式的非流式
    for pid, mid, want in [("relay", MODEL, want_relay),
                           ("anthropic", "claude-mock", want_ant),
                           ("gemini", "gemini-mock", want_gem)]:
        o = run(G.complete(pid, mid, plain))
        chk(f"㉕ {pid} 非流式：文本正确", o["text"] == want, o["text"])

    # 29-31) 错误翻译
    os.environ["PROVIDER_RELAY_BASE"] = MOCK_BASE + "/bad401"
    o = run(G.probe("relay", MODEL))
    chk("㉙ 上游 401 → 人话含「密钥无效」",
        not o["ok"] and "密钥无效" in json.dumps(o, ensure_ascii=False), str(o)[:150])
    chk("㉙ 401 的 upstream_status 被带出来",
        not o["ok"] and o["error"].get("upstream_status") == 401, str(o.get("error")))

    os.environ["PROVIDER_RELAY_BASE"] = MOCK_BASE + "/bad404"
    o = run(G.probe("relay", MODEL))
    chk("㉚ 上游 404 → 人话含「模型名」",
        not o["ok"] and "模型名" in json.dumps(o, ensure_ascii=False), str(o)[:150])

    os.environ["PROVIDER_RELAY_BASE"] = "http://127.0.0.1:1/v1"
    o = run(G.probe("relay", MODEL))
    chk("㉛ 连不上 → 报 network，不崩",
        not o["ok"] and o["error"]["code"] == "network", str(o)[:150])

    # 32-33) probe 正常路径
    os.environ["PROVIDER_RELAY_BASE"] = MOCK_BASE + "/v1"
    o = run(G.probe("relay", MODEL))
    chk("㉜ probe 成功 → ok=True + sample", o["ok"] and o.get("sample"), str(o)[:150])
    o = run(G.probe_provider("relay"))
    chk("㉝ probe_provider → verified 列出模型",
        o["ok"] and o["verified"] == [MODEL], str(o)[:150])
    o2 = run(G.probe("gemini", "gemini-mock"))
    chk("㉜ 非 openai 格式的 probe 也通（走 generateContent）", o2["ok"], str(o2)[:120])


def seed_legacy_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        " endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,"
        " ua TEXT, created TEXT NOT NULL, last_ok TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        [("2026-09-01T10:00:00Z", "in", "user", "早", '{"api_session":"sess-A"}'),
         ("2026-09-01T10:00:05Z", "out", "reply", "早呀", '{"api_session":"sess-A"}')],
    )
    conn.commit()
    conn.close()


def part_c(tmp: Path) -> None:
    """房子端点：起真实 relay。"""
    db_path = tmp / "relay.db"
    log_path = tmp / "uvicorn.log"
    seed_legacy_db(db_path)

    env = dict(os.environ)
    env.update({
        "RELAY_DB": str(db_path),
        "RELAY_SECRET": SECRET,
        "RELAY_HUMAN_NAME": "Lily",
        "RELAY_PUBLIC_PREFIX": "/relay",
        "RELAY_BACKEND_DIR": str(BACKEND),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "RELAY_UPLOAD_DIR": str(tmp / "uploads"),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        # 只留假上游这一个可用供应商 → 端点测试结果确定
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini",
        "PROVIDER_RELAY_KEY": os.environ["PROVIDER_RELAY_KEY"],
        "PROVIDER_RELAY_BASE": MOCK_BASE + "/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
    })
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--app-dir", str(DEPLOY)],
        env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_port(PORT):
            chk("㉞ relay 能启动", False, "健康检查超时")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return
        chk("㉞ relay 能启动", True)
        base = f"http://127.0.0.1:{PORT}"

        # 34-37) 无密钥一律 401
        for method, path, body in [
            ("GET", "/app/ext/providers", None),
            ("POST", "/app/ext/providers/probe", {"provider_id": "relay"}),
            ("POST", "/app/ext/llm/complete", {"messages": [{"role": "user", "content": "x"}]}),
            ("POST", "/app/ext/llm/chat", {"messages": [{"role": "user", "content": "x"}]}),
        ]:
            st, _ = req(base + path, method=method, body=body)
            chk(f"🔴 ㉞-㊲ 无密钥 {method} {path} → 401", st == 401, f"实际 {st}")

        # 38-39) 允许列表不泄漏
        st, d = req(base + "/app/ext/providers", token=SECRET)
        blob = json.dumps(d, ensure_ascii=False)
        chk("㊳ 带密钥 GET /providers → 200", st == 200, str(st))
        chk("🔴 ㊳ 响应里没有 URL", "http" not in blob, blob[:120])
        chk("🔴 ㊳ 响应里没有环境变量名", "PROVIDER_" not in blob, "")
        chk("🔴 ㊳ 响应里没有完整 key",
            os.environ["PROVIDER_RELAY_KEY"] not in blob, "")
        provs = d.get("providers") or []
        relay = [p for p in provs if p["id"] == "relay"]
        chk("㊴ relay.available=True（假上游配好了）",
            relay and relay[0]["available"] is True and relay[0]["models"] == [MODEL],
            json.dumps(relay, ensure_ascii=False)[:140])
        chk("㊹ 被 PROVIDERS_DISABLED 关掉的供应商不在列表里",
            all(p["id"] == "relay" for p in provs), str([p["id"] for p in provs]))

        # 40) 非流式端点
        st, d = req(base + "/app/ext/llm/complete", method="POST", token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}], "stream": False,
        })
        chk("㊵ 非流式端点 200 + OpenAI 兼容形状",
            st == 200 and d.get("object") == "chat.completion"
            and d["choices"][0]["message"]["content"] == f"[{MODEL}] {MOCK_TEXT}"
            and d.get("provider_id") == "relay",
            json.dumps(d, ensure_ascii=False)[:160])

        # 41) 流式端点
        st, lines = req_sse(base + "/app/ext/llm/chat", token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}], "stream": True,
        })
        frames = [l[5:].strip() for l in lines if l.startswith("data:")]
        text = ""
        for f in frames:
            if f == "[DONE]":
                continue
            try:
                o = json.loads(f)
            except Exception:
                continue
            for ch in (o.get("choices") or []):
                text += (ch.get("delta") or {}).get("content") or ""
        chk("㊶ 流式端点 200", st == 200, str(st))
        chk("🔴 ㊶ SSE 逐段（>1 帧）且拼接正确",
            len(frames) > 2 and text == f"[{MODEL}] {MOCK_TEXT}", f"{len(frames)}帧 text={text!r}")
        chk("🔴 ㊶ 以 [DONE] 收尾", frames and frames[-1] == "[DONE]", str(frames[-1:]))
        chk("🔴 ㊶ finish_reason=stop 有发出",
            any('"finish_reason": "stop"' in l for l in lines), "")

        # 42-43) 开流前就能报错
        st, d = req(base + "/app/ext/llm/chat", method="POST", token=SECRET, body={
            "provider_id": "nope", "messages": [{"role": "user", "content": "x"}]})
        chk("㊷ 未知供应商 → 400（干净 4xx，不是流里报）",
            st == 400 and d["error"]["code"] == "unknown_provider", f"{st} {d}")
        st, d = req(base + "/app/ext/llm/chat", method="POST", token=SECRET, body={
            "provider_id": "relay", "model": "not-in-list",
            "messages": [{"role": "user", "content": "x"}]})
        chk("㊸ 模型不在允许列表 → 400 model_not_allowed",
            st == 400 and d["error"]["code"] == "model_not_allowed", f"{st} {d}")

        # 44-46) settings 的 provider_id
        st, d = req(base + "/app/ext/settings", method="PUT", token=SECRET,
                    body={"provider_id": "relay", "model_id": MODEL, "persona": "你是汐"})
        chk("㊹ PUT settings 存 provider_id + model_id → 200",
            st == 200 and d.get("ok") and "provider_id" in d.get("changed", []),
            json.dumps(d, ensure_ascii=False)[:140])
        st, d = req(base + "/app/ext/settings", token=SECRET)
        chk("㊻ GET settings 读得回 provider_id",
            st == 200 and d.get("provider_id") == "relay" and d.get("model_id") == MODEL,
            f"provider_id={d.get('provider_id')}")
        chk("㊻ 只改 model_id 时 provider_id 被保留（不是整行覆盖）",
            d.get("persona") == "你是汐", str(d.get("persona")))
        st, d = req(base + "/app/ext/settings", method="PUT", token=SECRET,
                    body={"model_id": "not-in-list"})
        chk("㊺ PUT settings 模型不配对 → 400",
            st == 400 and d.get("reason") in ("invalid", None), f"{st} {d}")
        st, d = req(base + "/app/ext/settings", token=SECRET)
        chk("🔴 ㊺ 被拒之后设置**没有被改坏**", d.get("model_id") == MODEL, str(d.get("model_id")))

        # 47) probe 端点
        st, d = req(base + "/app/ext/providers/probe", method="POST", token=SECRET,
                    body={"provider_id": "relay", "all": True})
        chk("㊼ probe 端点 200", st == 200, f"{st} {json.dumps(d, ensure_ascii=False)[:120]}")

        # 48-49) 回归
        st, _ = req(base + "/app/ext/me", token=SECRET)
        chk("🔴 ㊽ 回归：P0 的 /app/ext/me 仍 200", st == 200, str(st))
        st, _ = req(base + "/app/history", token=SECRET)
        chk("🔴 ㊾ 回归：原版 /app/history 仍 200", st == 200, str(st))

        # 迁移是否上了线（schema_report 的字段名是 schema_version，不是 version）
        st, d = req(base + "/app/ext/schema", token=SECRET)
        chk("🔴 schema 版本已到 2（迁移生效）",
            d.get("schema_version") == 2 and d.get("expected_version") == 2
            and d.get("up_to_date") is True,
            f"schema_version={d.get('schema_version')} expected={d.get('expected_version')}")
        chk("🔴 messages 行数未变（2 条种子）+ 四张表齐",
            d.get("messages_rows") == 2 and d.get("tables_ok") is True,
            f"messages={d.get('messages_rows')} tables_ok={d.get('tables_ok')}")

        # ── 51-57) 🆕 OpenAI 路径别名（P3 通车前置，2026-09-15）──────────────
        #
        # 为什么必须验：`examples/api_loop.py`（红线目录，改不了）生成请求时是
        #     route["url"].rstrip("/") + "/chat/completions"
        # 也就是说**身体只会往后拼 `/chat/completions`**，而且它流式/非流式
        # 用的是**同一个路径**，只靠 body 的 `stream` 字段区分。
        # 通车 = 把身体的 LLM_API_BASE 指到房子 → 这条路径认不出来就是第一秒 404。
        V1 = "/app/ext/llm/v1/chat/completions"
        V0 = "/app/ext/llm/chat/completions"

        st, _ = req(base + V1, method="POST", body={"messages": [{"role": "user", "content": "x"}]})
        chk("🔴 51 别名无密钥 → 401（fail-closed）", st == 401, f"实际 {st}")
        st, _ = req(base + V0, method="POST", body={"messages": [{"role": "user", "content": "x"}]})
        chk("🔴 51 少一层 v1 的别名无密钥 → 401", st == 401, f"实际 {st}")

        st, lines = req_sse(base + V1, token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}], "stream": True,
        })
        fr = [l[5:].strip() for l in lines if l.startswith("data:")]
        txt = ""
        for f in fr:
            if f == "[DONE]":
                continue
            try:
                o = json.loads(f)
            except Exception:
                continue
            for ch in (o.get("choices") or []):
                txt += (ch.get("delta") or {}).get("content") or ""
        chk("🔴 52 别名 stream=True → SSE 逐段 + 拼接正确",
            st == 200 and len(fr) > 2 and txt == f"[{MODEL}] {MOCK_TEXT}",
            f"{st} {len(fr)}帧 text={txt!r}")
        chk("🔴 52 别名 stream=True → 以 [DONE] 收尾", fr and fr[-1] == "[DONE]", str(fr[-1:]))

        st, d = req(base + V1, method="POST", token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}], "stream": False,
        })
        chk("🔴 53 别名 stream=False → JSON 非流式（object=chat.completion）",
            st == 200 and d.get("object") == "chat.completion"
            and d["choices"][0]["message"]["content"] == f"[{MODEL}] {MOCK_TEXT}",
            f"{st} {json.dumps(d, ensure_ascii=False)[:140]}")

        st, d = req(base + V1, method="POST", token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}],
        })
        chk("🔴 54 别名不带 stream → 非流式（OpenAI 语义：默认不流）",
            st == 200 and d.get("object") == "chat.completion", f"{st} 类型={type(d).__name__}")

        st, lines = req_sse(base + V0, token=SECRET, body={
            "provider_id": "relay", "model": MODEL,
            "messages": [{"role": "user", "content": "在吗"}], "stream": True,
        })
        chk("🔴 55 少一层 v1 的别名也走通（base 填错一层有救）",
            st == 200 and any(l.startswith("data:") for l in lines), f"{st} {len(lines)}行")

        st, d = req(base + V1, method="POST", token=SECRET, body={
            "provider_id": "relay", "model": "not-in-list",
            "messages": [{"role": "user", "content": "x"}], "stream": True,
        })
        chk("🔴 56 别名开流前错误 → 干净 400（不是流里报）",
            st == 400 and d["error"]["code"] == "model_not_allowed", f"{st} {d}")

        # 57) 🔴 通车仿真：完全照身体的调用方式打一遍
        #     身体的三个动作（examples/api_loop.py）：url = BASE.rstrip("/") + "/chat/completions"
        #     headers = {"Authorization": f"Bearer {key}"}；body 见下（:293-299 / :334-340）
        st, lines = req_sse(base + V1, token=SECRET, body={
            "model": MODEL,
            "messages": [{"role": "system", "content": "你是汐"},
                         {"role": "user", "content": "在吗"}],
            "temperature": 0.7, "max_tokens": 2000, "stream": True,
        })
        body_received = seen.get("body") or {}
        chk("🔴 57 通车仿真：身体形状的请求能被解析并流出文本",
            st == 200 and any(l.startswith("data:") for l in lines), f"{st}")
        chk("🔴 57 通车仿真：上游收到的 system 在 messages 首位",
            (body_received.get("messages") or [{}])[0].get("role") == "system",
            json.dumps(body_received, ensure_ascii=False)[:140])
        chk("🔴 57 通车仿真：max_tokens/temperature 没被网关吃掉",
            body_received.get("max_tokens") == 2000
            and abs((body_received.get("temperature") or 0) - 0.7) < 1e-9,
            f"max_tokens={body_received.get('max_tokens')} temp={body_received.get('temperature')}")

        # ── 65-68) 🆕 P1 收尾：settings 的参数真的下发到上游（2026-09-15）──────
        #
        # 为什么这块非验不可：`settings` 表**早就存得下** max_tokens / temperature / effort，
        # 但网关以前只拿 provider_id / model_id，参数**存了＝没存**。
        # 前端把按钮做真之后，如果这里还这样，表现就是"设置页看着生效了、实际一个字没变" ——
        # 属于最难被发现的那类 bug（界面全程诚实，链路中途掉了）。
        req(base + "/app/ext/settings", method="PUT", token=SECRET,
            body={"max_tokens": 1234, "temperature": 0.25})

        # 65) body 没传 → 用 settings 的
        req(base + V1, method="POST", token=SECRET, body={
            "model": MODEL, "messages": [{"role": "user", "content": "x"}],
        })
        rb = seen.get("body") or {}
        chk("🔴 65 settings 里的参数在 body 没传时被补进请求",
            rb.get("max_tokens") == 1234 and abs((rb.get("temperature") or 0) - 0.25) < 1e-9,
            f"max_tokens={rb.get('max_tokens')} temp={rb.get('temperature')}")

        # 66) body 传了 → body 赢（注意 temperature=0 是"真要 0"，不能被顶掉）
        req(base + V1, method="POST", token=SECRET, body={
            "model": MODEL, "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 77, "temperature": 0,
        })
        rb = seen.get("body") or {}
        chk("🔴 66 body 显式传的参数优先于 settings（temperature=0 不被顶掉）",
            rb.get("max_tokens") == 77 and rb.get("temperature") == 0,
            f"max_tokens={rb.get('max_tokens')} temp={rb.get('temperature')}")

        # 67) effort 落库（与设置页 5 档对齐）+ 默认不下发
        st, d = req(base + "/app/ext/settings", method="PUT", token=SECRET, body={"effort": "xhigh"})
        chk("🔴 67 PUT effort=xhigh → 200 且落库（设置页那一档是合法的）",
            st == 200 and "effort" in (d.get("changed") or []), f"{st} {str(d)[:120]}")
        st, d = req(base + "/app/ext/settings", token=SECRET)
        chk("🔴 67 effort 读得回 xhigh", st == 200 and d.get("effort") == "xhigh", str(d.get("effort")))

        req(base + V1, method="POST", token=SECRET, body={
            "model": MODEL, "messages": [{"role": "user", "content": "x"}],
        })
        rb = seen.get("body") or {}
        chk("🔴 67 effort 存了但没配 EFFORT_PARAM → 照样不下发（不猜字段名）",
            not any(k in rb for k in ("effort", "reasoning_effort", "thinking")),
            json.dumps(rb, ensure_ascii=False)[:150])

        # 68) 身体不传 model 时 → 用 settings 里选的模型（通车后"设置页选了什么就用什么"）
        req(base + V1, method="POST", token=SECRET, body={
            "messages": [{"role": "user", "content": "x"}],
        })
        rb = seen.get("body") or {}
        chk("🔴 68 body 不带 model → 用 settings 里选的那个模型",
            rb.get("model") == MODEL, f"上游收到 model={rb.get('model')!r}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


def part_d() -> None:
    r = subprocess.run(
        ["git", "diff", "--stat", BASE_COMMIT, "--", "backend/", "examples/", "channel/"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    out = (r.stdout or "").strip()
    chk("🔴 ㊿ 红线：git diff 为空（backend/examples/channel 零改动）", out == "", out[:200])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kaelhome_providers_"))
    srv = start_mock()
    try:
        if not wait_port(MOCK_PORT, "/__ping"):
            chk("假上游能启动", False, "")
            return 1
        chk("假上游已就绪（127.0.0.1:8794）", True, "")

        part_a()
        part_a2()      # 🆕 58-64b：effort → 上游字段（纯逻辑，临时配 env 后复原）
        part_b()
        part_c(tmp)
        part_d()
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    ok_n = sum(1 for _, ok, _ in results if ok)
    lines = ["=" * 74, f"P1 模型网关验收　{ok_n}/{len(results)} 通过", "=" * 74]
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        lines.append(f"{mark} {name}")
        if not ok and detail:
            lines.append(f"      ↳ {detail}")
    bad = [n for n, ok, _ in results if not ok]
    lines.append("=" * 74)
    lines.append("全部通过 ✅" if not bad else f"失败 {len(bad)} 项 ❌")
    report = "\n".join(lines)

    print(report)
    (HERE / "providers_report.txt").write_text(report, encoding="utf-8")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
