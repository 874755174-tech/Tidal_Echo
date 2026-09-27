#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P3 通车预演验收 —— **身体（KaelLife）会怎么调这个网关**
==========================================================================

## 这一套守的是什么

P3 要做的第一件事是"通车"：把 `examples/api_loop.py`（身体）的 `LLM_API_BASE`
指向房子（`/app/ext/llm/v1`），于是**两个 Kael 变成一条路**。

通车最怕的不是"接不上"（那会响亮地 404），而是**接上了但悄悄变了样**：

  · 身体每次 JSON 调用都带 `response_format={"type":"json_object"}`，
    网关改之前**一个分支都没有** → 静默丢掉 → 表现是"模型偶尔不吐 JSON"，
    排查方向**永远指向 prompt**，查不到网关。
  · 身体带的参数（温度 / max_tokens）如果某一项被吃掉，
    表现是"这个模型怎么突然变笨了"。

所以这一套的核心手法跟 ⑧ 那套一样：**假上游把收到的 body 原样记下来**，
然后拿它跟"身体发出去的那份"对。断言全部打在**上游真正收到的字节**上，
不打在函数返回值上。

## 断面（用的是 KaelLife 的**真实**调用形状，不是我们编的）

`KaelLife/scheduler.py:1616 _llm_json()` —— 官方 openai SDK：

    client.chat.completions.create(
        model=..., messages=[{system},{user}],
        response_format={"type": "json_object"},     # ← 每次都带
        temperature=..., max_tokens=...,             # max_tokens 有时是 None（SDK 不发）
    )

⚠️ 它**不发 `tools`** —— 工具是提示词驱动的（工具表渲染进 prompt）。
   所以"带 tools"这条路在这一套里是**反着验**的：网关拒得干净（fail-loud），
   而不是静默丢掉一半上下文。

## 覆盖清单

  A. 纯逻辑（不起服务、不联网）
     1    身体的完整形状 → normalize 通过
     2    🔴 `response_format` 进了 params（**这条就是这次修的那个病**）
     3    只保留 type 一个键（不带上别的）
     4    `text` 也放行（OpenAI 合法值）
     5    🔴 不传 → params 里**没有**这个键（零副作用）
     6-8  🔴 形状不对 / 缺 type / `json_schema` → 400，**不是静默丢**
     9    openai 适配：真的出现在上游 body 里
     10   🔴 不传时 body 逐字节回到改动前（不含这个键）
     11   🔴 anthropic + response_format → 400 unsupported_param
     12   🔴 gemini + response_format → 400 unsupported_param
     13-14 anthropic / gemini 不带 → 照常（没被误伤）
     15   身体形状：max_tokens 缺省也能过（SDK 对 None 不发送）
     16-17 🔴 原生工具协议字段 / n>1 → 400（**顶层键曾经是纯静默丢**，C1/C2 红了才照见）

  B. 真房子 + 假上游 · 通车预演（从出口倒着验）
     1-2  🔴 非流式端点 → 200 + OpenAI 兼容形状
     3    🔴 **上游真收到了 `response_format`**
     4    上游真收到了 temperature / max_tokens
     5    上游收到的 messages 形状 = [system, user]
     6    流式端点 → SSE + [DONE]
     7    别名 `/llm/chat/completions` 带 stream → 走流式
     8    别名 `/llm/v1/chat/completions` 不带 stream → 走非流式（OpenAI 语义）

  C. 拒得干净（fail-loud，不是 fail-silent）
     1-2  🔴 `tools` 字段 → 400，且**上游一次都没被调用**
     3    🔴 `role:"tool"` → 400 unsupported_role
     4-5  🔴 `response_format` 非法 → 400 且上游零调用
     6    🔴 无密钥 → 401（每个端点；fail-closed）
     7    未知供应商 → 400

  D. ⑧⑪ 注入与通车**共存**（参数层不能把上下文层吃掉，反之亦然）
     1    🔴 带 response_format 的请求：注入仍然生效（上游 system 里有摘要）
     2    🔴 同一份请求：上游也真收到了 response_format（两边都在）
     3    🔴 上游收到的 messages 里每条只有 role/content（形状没被我们加料）
     4    🔴 上游 body 的键集合 ⊆ 已知白名单（没多发明字段）

  E. usage 照记（通车之后账本不能瞎）
     1    非流式一次 → usage_log 多一行 route=complete
     2    流式一次 → route=chat
     3    🔴 被 400 拒的请求 → **不记账**（没发生就不该有账）

  F. 反向断言（防旧病复发）· 源码扫描
     1    🔴 `providers.py` 里 `response_format` 有处理（不再是零分支）
     2    🔴 anthropic / gemini 两处都显式拒（计数 = 2）
     3    🔴 透传是"有值才加"，**没有**硬编码默认值（网关不主动发明参数）
     4    拒绝路径给的是**可行动**的话（说清工具该走哪一侧）
     5    🔴 原生工具协议字段被显式拒（顶层键不再是静默丢）

共 **44 项**。跑完把**全项**报告落到 `tools/llm_route_report.txt`（与其余各套同形）。

⚠️ 本套用端口 8814（房子）/ 8815（假上游），不与别套抢
   （8794 归 providers、8796/8797 archive、8798/8799 workshop、
    8800-8802 context、8810-8813 generate、8830 distill）。

用法：.venv\\Scripts\\python.exe tools\\llm_route_check.py
"""

import json
import os
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

# 🔴 GBK 陷阱：Windows 重定向 stdout → cp936，一个印不出的字符就整套半路死掉
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

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

# 🔴 本机 shell 里 HTTP_PROXY 常被注进来；httpx/urllib 会因此把 127.0.0.1 塞进代理。
for _v in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(_v, "127.0.0.1,localhost")

SECRET = "test-secret-route-0123456789"
PREFIX = "/relay"
PORT_OK = 8814          # 房子
MOCK_PORT = 8815        # 假上游

MODEL = "mock-route-1"
PERSONA = "你是住在这个房子里的人，说话像平时那样。"
SID = "api-20260927-120000-route"
SUM_CANARY = "他答应周末陪她挑纸胶带"
SUM_SEED = SUM_CANARY + "。她还欠他一张明信片。"
PROBE = "那我们周末就去挑纸胶带吧"
MOCK_REPLY = "灯还亮着的，我随口回了一句。"
TS = "2026-09-27T12:00:00+08:00"

# adapt() 产出的 body 只该有这些键（D4 用）——**白名单，多一个都算加料**
UPSTREAM_BODY_KEYS = {
    "model", "messages", "stream", "max_tokens", "temperature", "top_p",
    "stop", "response_format", "stream_options",
}

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def req(url, *, method="GET", token=None, body=None, timeout=40):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
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
    except Exception as e:
        return -1, {"_raw": f"{type(e).__name__}: {e}"}


def req_sse(url, *, token=None, body=None, timeout=40):
    """收一整条 SSE，返回 (status, 原始文本)。"""
    data = json.dumps(body, ensure_ascii=False).encode()
    r = urllib.request.Request(url, data=data, method="POST")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def wait_port(port, path="/healthz", timeout=45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def port_free(port) -> bool:
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ══════════════════════════════════════════════════════════════════════════
# 假上游：把收到的 body / headers 原样记下来（这一套的"眼睛"）
# ══════════════════════════════════════════════════════════════════════════

seen: dict = {}


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _send(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
            try:
                self.wfile.write(f.encode("utf-8"))
                self.wfile.flush()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return

    def do_GET(self):
        self._send(200 if self.path.startswith("/__ping") else 404, {"ok": True})

    def do_POST(self):
        body = self._read_body()
        seen["body"] = body
        seen["count"] = int(seen.get("count") or 0) + 1
        seen["keys"] = sorted(body.keys())
        seen["path"] = self.path
        seen["auth"] = self.headers.get("Authorization") or ""

        if self.path.rstrip("/").endswith("/chat/completions"):
            if body.get("stream"):
                return self._sse([
                    "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": MOCK_REPLY}}]},
                                          ensure_ascii=False) + "\n\n",
                    "data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                                        "finish_reason": "stop"}],
                                           "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
                                          ensure_ascii=False) + "\n\n",
                    "data: [DONE]\n\n",
                ])
            return self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
                "model": body.get("model") or "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": MOCK_REPLY}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            })
        return self._send(404, {"error": {"message": f"mock 不认：{self.path}"}})


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reset_seen():
    seen.clear()


def upstream_msgs():
    return (seen.get("body") or {}).get("messages") or []


def upstream_system() -> str:
    parts = [str(m.get("content") or "") for m in upstream_msgs()
             if str(m.get("role")) == "system"]
    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════
# 房子
# ══════════════════════════════════════════════════════════════════════════

def house_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "RELAY_DB": str(home / "relay.db"),
        "RELAY_SECRET": SECRET,
        "RELAY_HUMAN_NAME": "Lily",
        "RELAY_PUBLIC_PREFIX": PREFIX,
        "RELAY_BACKEND_DIR": str(BACKEND),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "RELAY_UPLOAD_DIR": str(home / "uploads"),
        "RELAY_WORKSHOP_DIR": str(home / "workshop"),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        # 只留中转站这一个供应商，指到假上游 —— 不碰真网络、不花一分钱
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini",
        "PROVIDER_RELAY_KEY": "sk-mock-route",
        "PROVIDER_RELAY_BASE": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    return env


def start_house(home: Path, port: int):
    log_path = home / f"uvicorn-{port}.log"
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY)],
        env=house_env(home), stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, log_path


def stop_house(proc, logf) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        logf.close()
    except Exception:
        pass


def seed_db(db_path: Path) -> None:
    """造一张跟后端一样的 messages + 一个有摘要的 sessions（⑧ 注入要用）。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            direction TEXT NOT NULL,
            kind      TEXT NOT NULL,
            text      TEXT NOT NULL,
            meta      TEXT NOT NULL DEFAULT '{}'
        )""")
    rows = [
        ("in", "user", "今天想聊聊盐系手帐风", {"api_session": SID}),
        ("out", "reply", "米白底、细线分隔、留白多那种？", {"api_session": SID}),
        ("in", "user", PROBE, {"api_session": SID}),
    ]
    for i, (d, k, t, meta) in enumerate(rows):
        conn.execute("INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
                     (f"2026-09-27T12:00:{i:02d}+08:00", d, k, t,
                      json.dumps(meta, ensure_ascii=False)))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id       TEXT PRIMARY KEY,
            user_id  TEXT NOT NULL,
            title    TEXT,
            since_id INTEGER DEFAULT 0,
            pinned   INTEGER DEFAULT 0,
            summary  TEXT,
            archived INTEGER DEFAULT 0,
            created  TEXT NOT NULL,
            updated  TEXT NOT NULL
        )""")
    conn.execute("INSERT INTO sessions (id,user_id,title,since_id,pinned,summary,archived,"
                 "created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                 (SID, "u_owner", None, 0, 0, SUM_SEED, 0, TS, TS))
    conn.commit()
    conn.close()


def db_path_of(home: Path) -> Path:
    return home / "relay.db"


def q1(db: Path, sql: str, args=()):
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def body_kael_json(probe: str = PROBE, **over) -> dict:
    """**身体（`_llm_json`）真实会发出来的形状** —— 不是我们自己编的。

    ⚠️ 故意**不带** `stream`：官方 SDK 走非流式，字段有没有都按 OpenAI 语义（=非流式）。
    """
    b = {
        "model": MODEL,
        "provider_id": "relay",
        "messages": [
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": probe},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.8,
        "max_tokens": 600,
    }
    b.update(over)
    return b


LLM_V1 = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/v1/chat/completions"
LLM_CHAT = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/chat"
LLM_ALIAS = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/chat/completions"
LLM_COMPLETE = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/complete"


# ══════════════════════════════════════════════════════════════════════════
# A 组：纯逻辑
# ══════════════════════════════════════════════════════════════════════════

def part_a() -> None:
    os.environ["PROVIDER_RELAY_KEY"] = "sk-mock-route"
    os.environ["PROVIDER_RELAY_BASE"] = f"http://127.0.0.1:{MOCK_PORT}/v1"
    os.environ["PROVIDER_RELAY_MODELS"] = MODEL
    os.environ["PROVIDER_ANTHROPIC_KEY"] = "sk-mock-anthropic"
    os.environ["PROVIDER_GEMINI_KEY"] = "sk-mock-gemini"

    import app_ext.providers as P

    # ① 身体的完整形状 → 通过
    try:
        req = P.normalize_request({
            "model": MODEL,
            "messages": [{"role": "system", "content": PERSONA},
                         {"role": "user", "content": PROBE}],
            "response_format": {"type": "json_object"},
            "temperature": 0.8, "max_tokens": 600,
        })
        ok = True
    except Exception as e:
        req, ok = {}, False
        chk("A1 身体（_llm_json）的完整形状 → normalize 通过", False, repr(e))
    if ok:
        chk("A1 身体（_llm_json）的完整形状 → normalize 通过", True, "")

    # ② 🔴 这次修的那个病：response_format 必须被**接住**
    rf = (req.get("params") or {}).get("response_format")
    chk("A2 🔴 response_format 进了 params（这次修的就是这个静默丢）",
        rf == {"type": "json_object"}, json.dumps(rf, ensure_ascii=False))
    chk("A3 只保留 type 一个键（不放行 json_schema 的附属字段）",
        isinstance(rf, dict) and set(rf.keys()) == {"type"},
        json.dumps(rf, ensure_ascii=False))

    # ④ text 也放行
    try:
        r2 = P.normalize_request({"messages": [{"role": "user", "content": "hi"}],
                                  "response_format": {"type": "text"}})
        got = (r2.get("params") or {}).get("response_format")
        chk("A4 response_format.type=text 也放行（OpenAI 合法值）",
            got == {"type": "text"}, json.dumps(got, ensure_ascii=False))
    except Exception as e:
        chk("A4 response_format.type=text 也放行（OpenAI 合法值）", False, repr(e))

    # ⑤ 不传 → 零副作用
    r3 = P.normalize_request({"messages": [{"role": "user", "content": "hi"}]})
    chk("A5 🔴 不传 response_format → params 里没有这个键（零副作用）",
        "response_format" not in (r3.get("params") or {}),
        json.dumps(r3.get("params") or {}, ensure_ascii=False))

    # ⑥⑦⑧ 形状不对 → 400，不是静默丢
    def _reject(body) -> tuple:
        try:
            P.normalize_request(body)
            return False, ""
        except P.ProviderError as e:
            return e.status == 400, e.code

    bad = ({"messages": [{"role": "user", "content": "hi"}], "response_format": "json"},
           {"messages": [{"role": "user", "content": "hi"}], "response_format": {}},
           {"messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "x"}}})
    got = [_reject(b) for b in bad]
    chk("A6 🔴 response_format 非对象 → 400 bad_param（不是静默丢）", got[0] == (True, "bad_param"), str(got[0]))
    chk("A7 🔴 response_format 缺 type → 400 bad_param（不是静默丢）", got[1] == (True, "bad_param"), str(got[1]))
    chk("A8 🔴 json_schema 不在白名单 → 400（网关 v1 不猜）", got[2] == (True, "bad_param"), str(got[2]))

    # ⑨⑩ openai 适配：真透传；不传时逐字节不动
    url, hdrs, b = P.adapt("relay", MODEL, req)
    chk("A9 openai 适配：response_format 真的进了上游 body",
        b.get("response_format") == {"type": "json_object"},
        json.dumps(b.get("response_format"), ensure_ascii=False))
    _, _, b0 = P.adapt("relay", MODEL, r3)
    chk("A10 🔴 不传时上游 body 不含这个键（逐字节回到改动前）",
        "response_format" not in b0, json.dumps(sorted(b0.keys())))

    # ⑪⑫ 原生协议没这个字段的 → 显式拒
    def _adapt_reject(pid: str, model: str) -> tuple:
        try:
            P.adapt(pid, model, req)
            return False, ""
        except P.ProviderError as e:
            return e.code, str(e.message)

    c1, m1 = _adapt_reject("anthropic", "claude-opus-4-6")
    chk("A11 🔴 anthropic + response_format → 400 unsupported_param（不静默丢）",
        c1 == "unsupported_param" and "Anthropic" in m1, f"{c1} {m1[:60]}")
    c2, m2 = _adapt_reject("gemini", "gemini-2.5-pro")
    chk("A12 🔴 gemini + response_format → 400 unsupported_param（不静默丢）",
        c2 == "unsupported_param" and "Gemini" in m2, f"{c2} {m2[:60]}")

    # ⑬⑭ 不带时照常（没被误伤）
    try:
        _, _, ab = P.adapt("anthropic", "claude-opus-4-6", r3)
        ok_a = ab.get("model") == "claude-opus-4-6" and "max_tokens" in ab
    except Exception as e:
        ok_a, ab = False, {"_err": repr(e)}
    chk("A13 anthropic 不带 response_format → 照常出 body（没误伤）", ok_a, str(ab)[:80])
    try:
        _, _, gb = P.adapt("gemini", "gemini-2.5-pro", r3)
        ok_g = "contents" in gb
    except Exception as e:
        ok_g, gb = False, {"_err": repr(e)}
    chk("A14 gemini 不带 response_format → 照常出 body（没误伤）", ok_g, str(gb)[:80])

    # ⑮ max_tokens 缺省（SDK 对 None 不发送）
    try:
        r4 = P.normalize_request({"messages": [{"role": "user", "content": "hi"}],
                                  "response_format": {"type": "json_object"},
                                  "temperature": 0.8})
        _, _, b4 = P.adapt("relay", MODEL, r4)
        chk("A15 身体形状：max_tokens 缺省也能过（兜底 DEFAULT_MAX_TOKENS）",
            b4.get("max_tokens") == P.DEFAULT_MAX_TOKENS,
            str(b4.get("max_tokens")))
    except Exception as e:
        chk("A15 身体形状：max_tokens 缺省也能过（兜底 DEFAULT_MAX_TOKENS）", False, repr(e))

    # ⑯⑰ 🔴 顶层键曾经是**纯静默丢**的（白名单只做在 role 那一层）——
    #      C1/C2 红了才照见。2026-09-27 补：认识但不支持的 → 显式 400。
    _one = {"messages": [{"role": "user", "content": "hi"}]}
    got = [_reject(dict(_one, **x)) for x in
           ({"tools": [{"type": "function"}]},
            {"functions": [{"name": "x"}]},
            {"function_call": "auto"},
            {"tool_choice": "auto"},
            {"n": 2})]
    chk("A16 🔴 顶层 tools / functions / function_call / tool_choice → 400 unsupported_param",
        all(g == (True, "unsupported_param") for g in got[:4]), str(got[:4]))
    chk("A17 🔴 n=2（多候选）→ 400；n=1 放行（默认行为无害）",
        got[4] == (True, "unsupported_param")
        and _reject(dict(_one, n=1)) == (False, ""), f"{got[4]} n=1→{_reject(dict(_one, n=1))}")


# ══════════════════════════════════════════════════════════════════════════
# B 组：真房子 + 假上游 · 通车预演
# ══════════════════════════════════════════════════════════════════════════

def part_b(db: Path) -> None:
    # ① 非流式（身体的主路）
    reset_seen()
    st, out = req(LLM_V1, method="POST", token=SECRET, body=body_kael_json())
    chk("B1 🔴 非流式端点（身体的主路）→ 200 且上游真收到",
        st == 200 and seen.get("count") == 1, f"{st} count={seen.get('count')}")

    # ② OpenAI 兼容形状
    try:
        content = out["choices"][0]["message"]["content"]
        ok_shape = (out.get("object") == "chat.completion" and content == MOCK_REPLY)
    except Exception:
        content, ok_shape = None, False
    chk("B2 响应是 OpenAI 兼容形状（object=chat.completion + message.content）",
        ok_shape, json.dumps(out, ensure_ascii=False)[:120])

    # ③ 🔴 上游真收到了 response_format —— **从出口倒着验**
    up_rf = (seen.get("body") or {}).get("response_format")
    chk("B3 🔴 上游真收到了 response_format（通车之后 JSON 模式没丢）",
        up_rf == {"type": "json_object"}, json.dumps(up_rf, ensure_ascii=False))

    # ④ 参数透传
    ub = seen.get("body") or {}
    chk("B4 上游真收到了 temperature / max_tokens",
        ub.get("temperature") == 0.8 and ub.get("max_tokens") == 600,
        f"t={ub.get('temperature')} m={ub.get('max_tokens')}")

    # ⑤ messages 形状
    up = upstream_msgs()
    chk("B5 上游收到的 messages = [system, user]（人格进 system）",
        len(up) == 2 and up[0].get("role") == "system"
        and PERSONA in str(up[0].get("content")) and up[1].get("content") == PROBE,
        json.dumps(up, ensure_ascii=False)[:120])

    # ⑥ 流式端点
    st, raw = req_sse(LLM_CHAT, token=SECRET,
                      body=body_kael_json(stream=True))
    chk("B6 流式端点 → SSE 逐段 + 以 [DONE] 收尾",
        st == 200 and MOCK_REPLY in raw and "[DONE]" in raw and "data: " in raw,
        raw[:120])

    # ⑦ 别名：带 stream → 走流式
    st, raw = req_sse(LLM_ALIAS, token=SECRET, body=body_kael_json(stream=True))
    chk("B7 别名 /llm/chat/completions 带 stream → 走流式（SSE）",
        st == 200 and "text/event-stream" not in raw and "[DONE]" in raw,
        raw[:100])

    # ⑧ 别名 v1：不带 stream → OpenAI 语义 = 非流式
    reset_seen()
    st, out = req(LLM_V1, method="POST", token=SECRET, body=body_kael_json())
    is_json = isinstance(out, dict) and out.get("object") == "chat.completion"
    chk("B8 别名 /llm/v1/chat/completions 不带 stream → 非流式（OpenAI 语义）",
        st == 200 and is_json, f"{st} {json.dumps(out, ensure_ascii=False)[:80]}")


# ══════════════════════════════════════════════════════════════════════════
# C 组：拒得干净（fail-loud）
# ══════════════════════════════════════════════════════════════════════════

def part_c(db: Path) -> None:
    # ① 带 tools 字段（身体今天不带 —— 工具是提示词驱动的；但"带工具调用"这条路
    #    必须拒得干净。⚠️ 2026-09-27 之前它是**静默丢**的：上游收不到 tools、
    #    模型永远不调工具，而响应看起来完全正常。）
    reset_seen()
    b = body_kael_json()
    b["tools"] = [{"type": "function",
                   "function": {"name": "garden_post", "parameters": {}}}]
    st, out = req(LLM_V1, method="POST", token=SECRET, body=b)
    code = ((out or {}).get("error") or {}).get("code")
    chk("C1 🔴 带 tools → 400 unsupported_param（原生工具协议：不静默丢）",
        st == 400 and code == "unsupported_param",
        f"{st} {code} {json.dumps(out, ensure_ascii=False)[:80]}")
    chk("C2 🔴 被拒时上游**一次都没被调用**（没发生就不该有请求）",
        seen.get("count") is None, f"count={seen.get('count')}")

    # ③ tool role
    reset_seen()
    b = body_kael_json()
    b["messages"] = [{"role": "system", "content": PERSONA},
                     {"role": "user", "content": PROBE},
                     {"role": "tool", "content": "{\"ok\":true}"}]
    st, out = req(LLM_V1, method="POST", token=SECRET, body=b)
    code = ((out or {}).get("error") or {}).get("code")
    chk("C3 🔴 role:\"tool\" → 400 unsupported_role（用户 09-16 定的那条）",
        st == 400 and code == "unsupported_role", f"{st} {code}")

    # ④⑤ 非法 response_format → 400 且上游零调用
    reset_seen()
    st, out = req(LLM_V1, method="POST", token=SECRET,
                  body=body_kael_json(response_format={"type": "json_schema"}))
    code = ((out or {}).get("error") or {}).get("code")
    chk("C4 🔴 json_schema → 400 bad_param（网关 v1 不猜）",
        st == 400 and code == "bad_param", f"{st} {code}")
    chk("C5 🔴 被拒时上游零调用（key 不在上游、钱不花）",
        seen.get("count") is None, f"count={seen.get('count')}")

    # ⑥ 无密钥 → 401（fail-closed）
    got = []
    for u, kw in ((LLM_COMPLETE, {"body": body_kael_json()}),
                  (LLM_CHAT, {"body": body_kael_json(stream=True)}),
                  (LLM_V1, {"body": body_kael_json()}),
                  (LLM_ALIAS, {"body": body_kael_json()})):
        st, _ = req(u, method="POST", token=None, **kw)
        got.append(st)
    chk("C6 🔴 无密钥 → 401（四个端点都 fail-closed）",
        all(s == 401 for s in got), str(got))

    # ⑦ 未知供应商
    st, out = req(LLM_V1, method="POST", token=SECRET,
                  body=body_kael_json(provider_id="nope"))
    code = ((out or {}).get("error") or {}).get("code")
    chk("C7 未知供应商 → 400 unknown_provider",
        st == 400 and code == "unknown_provider", f"{st} {code}")


# ══════════════════════════════════════════════════════════════════════════
# D 组：⑧⑪ 注入与通车共存
# ══════════════════════════════════════════════════════════════════════════

def part_d(db: Path) -> None:
    reset_seen()
    st, out = req(LLM_V1, method="POST", token=SECRET, body=body_kael_json())
    sysmsg = upstream_system()
    chk("D1 🔴 带 response_format 的请求：注入仍然生效（上游 system 里有摘要）",
        st == 200 and SUM_CANARY in sysmsg, f"{st} sys={sysmsg[:80]}")

    ub = seen.get("body") or {}
    chk("D2 🔴 同一份请求：上游也真收到了 response_format（两边都在，互不吃）",
        (ub.get("response_format") or {}).get("type") == "json_object",
        json.dumps(ub.get("response_format"), ensure_ascii=False))

    dirty = [m for m in upstream_msgs()
             if set(m.keys()) != {"role", "content"}]
    chk("D3 🔴 上游收到的 messages 里每条只有 role/content（形状没被加料）",
        not dirty, json.dumps(dirty, ensure_ascii=False)[:100])

    extra = set(ub.keys()) - UPSTREAM_BODY_KEYS
    chk("D4 🔴 上游 body 的键集合 ⊆ 白名单（没多发明字段）",
        not extra, str(sorted(extra)))


# ══════════════════════════════════════════════════════════════════════════
# E 组：usage 照记
# ══════════════════════════════════════════════════════════════════════════

def part_e(db: Path) -> None:
    n0 = int(q1(db, "SELECT COUNT(*) FROM usage_log") or 0)
    reset_seen()
    req(LLM_V1, method="POST", token=SECRET, body=body_kael_json())
    n1 = int(q1(db, "SELECT COUNT(*) FROM usage_log") or 0)
    r1 = q1(db, "SELECT route FROM usage_log ORDER BY id DESC LIMIT 1")
    chk("E1 非流式一次 → usage_log 多一行 route=complete",
        n1 == n0 + 1 and r1 == "complete", f"{n0}->{n1} route={r1}")

    reset_seen()
    req_sse(LLM_CHAT, token=SECRET, body=body_kael_json(stream=True))
    time.sleep(0.5)
    n2 = int(q1(db, "SELECT COUNT(*) FROM usage_log") or 0)
    r2 = q1(db, "SELECT route FROM usage_log ORDER BY id DESC LIMIT 1")
    chk("E2 流式一次 → route=chat",
        n2 == n1 + 1 and r2 == "chat", f"{n1}->{n2} route={r2}")

    # ③ 被拒的请求不记账
    n3 = int(q1(db, "SELECT COUNT(*) FROM usage_log") or 0)
    req(LLM_V1, method="POST", token=SECRET,
        body=body_kael_json(response_format={"type": "json_schema"}))
    n4 = int(q1(db, "SELECT COUNT(*) FROM usage_log") or 0)
    chk("E3 🔴 被 400 拒的请求 → 不记账（没发生就不该有账）", n4 == n3, f"{n3}->{n4}")


# ══════════════════════════════════════════════════════════════════════════
# F 组：反向断言（源码扫描）
# ══════════════════════════════════════════════════════════════════════════

def part_f() -> None:
    src = (DEPLOY / "app_ext" / "providers.py").read_text(encoding="utf-8")

    n = src.count("response_format")
    chk("F1 🔴 providers.py 里 response_format 有处理（不再是零分支）",
        n >= 4, f"出现 {n} 次")
    chk("F2 🔴 anthropic / gemini 两处都显式拒（不静默丢）",
        src.count("_no_response_format(params,") == 2,
        str(src.count("_no_response_format(params,")))
    chk("F3 🔴 透传是「有值才加」—— 网关不主动发明参数（没有硬编码默认值）",
        'body["response_format"] = params["response_format"]' in src
        and 'body["response_format"] = {"type"' not in src, "")
    chk("F4 拒绝路径给的是**可行动**的话（说清换什么供应商）",
        "OpenAI 系供应商" in src, "")
    chk("F5 🔴 原生工具协议字段被显式拒（顶层键不再是静默丢）",
        "_TOOL_PROTOCOL_KEYS" in src and "tools" in src
        and src.count("unsupported_param") >= 3,
        str(src.count("unsupported_param")))


# ══════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    for p in (PORT_OK, MOCK_PORT):
        if not port_free(p):
            print(f"[!!] 端口 {p} 被占 —— 先关掉占用的进程再跑（否则整套全红且看不出原因）")
            return 1

    tmp = Path(tempfile.mkdtemp(prefix="llmroute-"))
    home = tmp / "house"
    home.mkdir(parents=True, exist_ok=True)
    db = db_path_of(home)
    seed_db(db)

    srv = start_mock()
    proc, logf, log_path = start_house(home, PORT_OK)
    try:
        if not wait_port(PORT_OK):
            print(f"[!!] 房子没起来（{PORT_OK}），日志尾部：")
            try:
                print("\n".join(log_path.read_text(encoding="utf-8", errors="replace")
                                .splitlines()[-25:]))
            except Exception:
                pass
            return 1

        print("=" * 74)
        print("A 组：纯逻辑（不起服务 · 通车形状）")
        print("=" * 74)
        part_a()

        print("=" * 74)
        print("B 组：真房子 + 假上游 · 通车预演（从出口倒着验）")
        print("=" * 74)
        part_b(db)

        print("=" * 74)
        print("C 组：拒得干净（fail-loud，不是 fail-silent）")
        print("=" * 74)
        part_c(db)

        print("=" * 74)
        print("D 组：⑧⑪ 注入与通车共存")
        print("=" * 74)
        part_d(db)

        print("=" * 74)
        print("E 组：usage 照记")
        print("=" * 74)
        part_e(db)

        print("=" * 74)
        print("F 组：反向断言（源码扫描）")
        print("=" * 74)
        part_f()
    finally:
        stop_house(proc, logf)
        try:
            srv.shutdown()
        except Exception:
            pass

    # 与其余各套同形：把**全项**落一份 tools/llm_route_report.txt
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    lines = [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   {d}" if (d and not ok) else "")
             for name, ok, d in results]
    report = ("P3 通车预演验收（第 17 套）\n"
              "工具：tools/llm_route_check.py\n"
              "断面：examples/api_loop.py:293（聊天身体）"
              " + KaelLife/scheduler.py:1616 _llm_json（自主醒来）\n\n"
              + "\n".join(lines)
              + f"\n\n共 {len(results)} 项，通过 {passed}，失败 {failed}\n")
    (HERE / "llm_route_report.txt").write_text(report, encoding="utf-8")
    print(report)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
