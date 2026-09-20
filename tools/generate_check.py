#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
停止 / 重答 / 多版本验收 —— P2 ⑨
==========================================================================

## 这一套到底在守什么

⑨ 最容易出的病**不是**"停了没反应"，而是**停出了别的东西**：

  · **偷偷换模型重跑**：网关把上游流硬掐断 → 身体 `run_model` 走 `except Exception`
    → **fallback 去链上的下一个模型重跑一遍**（`examples/api_loop.py:376-381`）
    → 用户点的"停止"，屏幕上的表现是"换了个模型又生成了一整条"。
  · **停成了丢东西**：为了标 `truncated` 顺手把 `text` 改了，或者把行删了。
  · **重答成了消失**：retry 把旧的那条删了，而不是标 `superseded`。

所以这一套的核心手法：**起一个假身体**（照抄 `api_loop.stream_chat` 的形状，
**连 fallback 那一段也照抄**）+ **一个慢的假上游**，然后从出口倒着验：

  · 网关最后发给身体的是**正常的 `data: [DONE]`**，还是"断连"？
  · 假身体**调用了几次上游**？（1 次 = 没 fallback；2 次 = 已经踩坑）
  · 那半条**落库了吗**？`text` 有没有被顺手改过？
  · retry 之后**旧的那条还在吗**？（⑨ 版的"只插不删"）

## 覆盖清单

  A. 纯逻辑（不起 HTTP、不碰库）
     1    没有在飞时 should_stop = False
     2    begin 之后 live 有一条
     3    🔴 request_stop 不传 session → 停"当前在飞的那一个"
     4    再停一次：ok 且说明"已经在停了"（不重复置位、不报错）
     5    🔴 register_notify 的回调**真被叫到**（这是"零延迟停止"的关键）
     6    should_stop 置位后为 True
     7    end() 之后：should_stop=False、live 空
     8    没有在飞时 request_stop → ok=False（不抛）
     9    🔴 `_key_of("")` = 未知占位（认不出会话也能停当前那个）
     10   TTL：touched 调老 → prune 掉（防内存长草）
     11   🔴 last_turn 认的是 `api_session`：另一会话的消息**不进**上一次往返
     12   last_turn：有 user 没有回复 → no_reply_yet
     13   last_turn：不存在的会话 → no_session_rows
     14   brain_target() 读不到文件 → ""（不是抛）
     15   loop_ingest_url() 读的是 `RELAY_LOOP_INGEST_URL` 同一个变量

  B. 网关照停（真房子 + **慢**假上游 + 假身体）—— **从出口倒着验**
     1-5  对照组（不停止）：完整版 / 正常收尾 / 上游只调 1 次 / 6 个碎片都吐了 /
          完整落库且**不带** truncated
     6    stop 端点 200 且 ok=True
     7    🔴 停止后网关发的是**正常收尾**（`finish=stop` + `data: [DONE]`）
     8    🔴 假身体**只调了 1 次上游**（= **没有 fallback 换模型重跑**）
     9    🔴 停止后上游**不再被继续读**（吐出的碎片数 < 全量）
     10   🔴 那半条真落库了，而且比完整版短
     11   🔴 落库那条 `meta.truncated = true`（库里留痕、导出看得见）
     12   🔴 停止没删任何原文：老的 id 一个不少，行数只 +1
     13   🔴 停止不改已有内容：停止前那批行的正文指纹**逐字节相同**
     14   🔴 那半条是完整版的**真前缀**（不是被拼歪的残段）
     15   🔴 打标是**合并**不是覆盖（那半条的 meta 里会话归属还在）

  C. retry / reroll
     1    retry 前：旧那条在、且还没 superseded
     2-3  retry 200 且 ok；报的 superseded_id = 旧那条
     4-6  🔴 旧那条**还在库里**、**text 一个字没改**、只是 superseded
     7    🔴 新的一条 out 出现，内容是完整版
     8    🔴 行数只 +1（新回复），一条都没少
     9    🔴 假身体收到的是**同一条你说的话**（id 与原文都对）
     10   retry 之后上游只被调用 1 次（走的是身体，不是房子自己造）
     11-12 reroll 200；🔴 打的是 `variant` 标
     13   🔴 reroll 之后旧版本一条不删（out 只 +1，老 id 全在）
     14-15 不存在的会话 → no_session_rows；不传 session → no_session
     16   桌面模式 → brain_desktop（给好报错，不是崩）
     17   status 只读：报 brain_target + guard + 上一次往返

  D. 红线 / 接线 / 开关
     1-2  四条 `_ROUTES` 都在；register 里有第 ⑨ 步
     3     🔴 源码扫描：扩展层对 messages 的写语句**只有** `SET meta = ?`
     4     🔴 源码里没有 DELETE / DROP / ALTER 打 messages 的写法
     5-6   🔴 `_meta_guard` 挡住改正文，且被拦后已回滚
     7-8   🔴 只写 meta 被允许；且**不动正文指纹**（真比对）
     9     schema 版本 = 4（⑩-a 的 `memories.source`）—— 写死是有意的，
           守"没人偷偷动表结构"；有意升级就来改这一行
     10    逃生开关在
     11    verify_all 已接本套
     12-14 启动日志里有 ⑨ 那行；🔴 整段日志 + `summary_line()` 都 **GBK 安全**
     15    🔴 关掉开关的房子：端点 404（少了能力，但房子照常营业）

⚠️ 本套用端口 8810（房子）/ 8811（慢假上游）/ 8812（假身体）/ 8813（关开关的房子）。
用法：.venv\Scripts\python.exe tools\generate_check.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

# 🔴 本机 PowerShell 5.1 管道下 sys.stdout.encoding = cp936(gbk)，而验收名里有
#    🔴/✅/⚠️ 这类 GBK 编不出的字符 -> print 到一半 UnicodeEncodeError，整套会
#    **半路死掉**（看起来像"验收挂了"，其实是没跑完）。
#    errors="replace" 只把编不出的字符降级成 "?"，中文和结论一个字不动。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

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

sys.path.insert(0, str(DEPLOY))

# 🔴 本机 shell 里 HTTP_PROXY 常被注进来；httpx/urllib 会因此把 127.0.0.1 塞进代理。
for _v in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(_v, "127.0.0.1,localhost")

SECRET = "test-secret-generate-0123456789"
PREFIX = "/relay"
PORT_OK = 8810          # 有数据的库（正常路径）
MOCK_PORT = 8811        # 慢假上游
BODY_PORT = 8812        # 假身体
PORT_OFF = 8813         # 关掉 ⑨ 的房子

MODEL = "mock-gen-1"
PERSONA = "你是住在这个房子里的人，说话像平时那样。"
SID = "api-20260919-200000-generate"
SID2 = "api-20260919-210000-other"

# 慢上游吐出的碎片（拼起来就是"完整版"）
PIECES = ["第一段。", "第二段。", "第三段。", "第四段。", "第五段。", "第六段。"]
FULL = "".join(PIECES)
OTHER_CANARY = "另一个会话里的暗号：雾绿与雾蓝"
PROBE = "那我们周末就去挑纸胶带吧"
TS = "2026-09-19T20:00:00+08:00"

DEAD_PORT = 8899        # 故意没有任何东西在听（验"叫不动身体"）

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def num(d, key):
    """取一个数字字段（不存在/不是数字 → None）。

    🔴 别写 `d.get(k) or -1`：合法的 **0** 是 falsy，会被当成"缺失" → 断言假红。
    """
    v = (d or {}).get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


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


def wait_port(port, path="/healthz", timeout=45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


# ══════════════════════════════════════════════════════════════════════════
# 慢假上游：一次吐一个碎片、中间故意等一会儿（给"停止"留出落点）
# ══════════════════════════════════════════════════════════════════════════

seen: dict = {}
SLOW = {"on": False, "delay": 0.35}
#: 🔴 **协议级硬断流**开关：声明 Content-Length 却少写字节 → 客户端拿到不完整的
#: body → httpx 抛 `RemoteProtocolError`。这是"硬断流"的忠实模型。
#: （注意：仅仅"少写几片然后干净关连接"**不是**硬断流 —— 无 Content-Length 时
#:   HTTP/1.0 靠关连接表示结束，客户端读到一个**完整**的短 body，不会抛。
#:   这个区别是实测出来的，见 B16 的注释。）
BROKEN = {"on": False, "declare": 99999}


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

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

    def _frame(self, text=None, finish=None, usage=None):
        ch = {"index": 0, "delta": {}}
        if text is not None:
            ch["delta"]["content"] = text
        if finish:
            ch["finish_reason"] = finish
        ev = {"choices": [ch]}
        if usage:
            ev["usage"] = usage
        return "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"

    def do_GET(self):
        self._send(200 if self.path.startswith("/__ping") else 404, {"ok": True})

    def _broken_stream(self):
        """协议级硬断流：声明 Content-Length，只写 3 片，然后关连接。

        客户端读到的字节数 < 声明值 → httpx 抛
        `RemoteProtocolError: peer closed connection without sending complete message body`。
        """
        n_declared = int(BROKEN["declare"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(n_declared))   # ← 撒谎的关键
        self.end_headers()
        for i, piece in enumerate(PIECES[:3]):
            try:
                self.wfile.write(self._frame(text=piece).encode("utf-8"))
                self.wfile.flush()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                seen["closed_early"] = True
                return
            seen["emitted"] = i + 1
        # 🔴 **不写 finish、不写 [DONE]、不补齐 Content-Length** —— 直接返回
        #    （handler 返回后连接关闭）。这才是"掐断"。
        seen["closed_early"] = True

    def do_POST(self):
        body = self._read_body()
        seen["body"] = body
        seen["count"] = int(seen.get("count") or 0) + 1
        seen["emitted"] = 0
        seen["closed_early"] = False

        if self.path.rstrip("/").endswith("/chat/completions"):
            if not body.get("stream"):
                return self._send(200, {
                    "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
                    "model": body.get("model") or "mock",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": FULL}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 9}})
            if BROKEN["on"]:
                return self._broken_stream()
            self._sse_open()
            for i, piece in enumerate(PIECES):
                if SLOW["on"] and i:
                    time.sleep(SLOW["delay"])
                try:
                    self.wfile.write(self._frame(text=piece).encode("utf-8"))
                    self.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    seen["closed_early"] = True
                    return
                seen["emitted"] = i + 1
            try:
                self.wfile.write(self._frame(
                    finish="stop", usage={"prompt_tokens": 9, "completion_tokens": 9}
                ).encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                seen["closed_early"] = True
            return
        return self._send(404, {"error": {"message": f"mock 不认：{self.path}"}})


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reset_seen():
    seen.clear()


# ══════════════════════════════════════════════════════════════════════════
# 假身体 —— 照抄 `examples/api_loop.py` 的形状（**含 fallback 那一段**）
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 为什么必须自己写一个，而不是直接调网关：
#    "停止变成了偷偷换模型重跑"这个病，**只会在身体那一侧显形**。
#    所以这里连 `run_model` 的 for-route 循环都照抄 —— 上游一旦"断连"，
#    它就会 `except Exception` → 换下一个"模型"（这里用同一条路由再来一次）→
#    上游调用计数就会变成 2。断言就打在**这个计数**上。

BODY = {"calls": [], "last": {}, "texts": []}


def _body_stream_once(model: str) -> dict:
    """照抄 `stream_chat`：真流式读，断连会抛（= 真实身体 fallback 的触发点）。"""
    import httpx
    url = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/v1/chat/completions"
    payload = {"model": model, "messages": BODY["calls"][-1]["messages"],
               "temperature": 0.8, "max_tokens": 512, "stream": True}
    parts, saw_done, finish = [], False, ""
    with httpx.Client(timeout=httpx.Timeout(60.0, read=60.0), trust_env=False) as c:
        with c.stream("POST", url,
                      # 🔴 身体对房子的上游 Key 就是**房子自己的密钥**（`RELAY_SECRET`）。
                      #    `llm_routes.py` 顶部原话："接身体时把 base 换成房子、
                      #    key 换成 RELAY_SECRET"。写别的（比如 provider 的 key）
                      #    会被 `relay.check_auth` 挡成 **401** —— 而 401 会让身体
                      #    `except Exception` → 换下一条模型重跑，把 B 组全部污染成假红。
                      headers={"Authorization": f"Bearer {SECRET}",
                               "Content-Type": "application/json"},
                      json=payload) as resp:
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"fallback HTTP {resp.status_code}")
            resp.raise_for_status()
            for line in resp.iter_lines():
                line = (line or "").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    saw_done = True
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                ch = (ev.get("choices") or [{}])[0]
                if (ch.get("delta") or {}).get("content"):
                    parts.append(ch["delta"]["content"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    return {"text": "".join(parts), "saw_done": saw_done, "finish": finish}


def _body_run_model(messages) -> dict:
    """照抄 `run_model` 的 for-route 循环（两条同路由 —— 专门用来数 fallback）。"""
    tried = []
    for _ in range(2):                      # 真实身体是 2 条模型的链
        tried.append(MODEL)
        try:
            out = _body_stream_once(MODEL)
            out["tried"] = tried[:-1]
            return out
        except Exception as e:
            BODY.setdefault("errors", []).append(f"{type(e).__name__}: {e}")
    return {"text": "", "error": "all models failed", "tried": tried, "saw_done": False}


def _body_relay_out(payload: dict) -> int:
    r = urllib.request.Request(
        f"http://127.0.0.1:{PORT_OK}{PREFIX}/channel/out",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {SECRET}"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status


class Body(BaseHTTPRequestHandler):
    """假身体的 `/loop/ingest`。"""
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if not self.path.rstrip("/").endswith("/loop/ingest"):
            return self._send(404, {"ok": False})
        sid = "api-fake-" + str(int(time.time() * 1000))[-8:]
        messages = [{"role": "system", "content": PERSONA},
                    {"role": "user", "content": payload.get("text") or ""}]
        BODY["calls"].append({"payload": payload, "messages": messages,
                              "stream_id": sid})
        t0 = time.time()
        out = _body_run_model(messages)
        dt = round(time.time() - t0, 2)
        text = (out.get("text") or "").strip() or "(假身体没说出话)"
        BODY["texts"].append(text)
        BODY["last"] = {"stream_id": sid, "text": text,
                        "saw_done": bool(out.get("saw_done")),
                        "finish": out.get("finish") or "",
                        "tried": out.get("tried") or [],
                        "seconds": dt,
                        "session_id": payload.get("session_id") or "",
                        "msg_id": payload.get("id")}
        try:
            _body_relay_out({
                "type": "reply_delta", "stream_id": sid, "done": True,
                "final_text": text,
                "api": {"runtime": "fake_body", "model": MODEL},
                "api_session": payload.get("session_id") or "",
            })
            BODY["last"]["saved"] = True
        except Exception as e:
            BODY["last"]["saved"] = f"{type(e).__name__}: {e}"
        return self._send(200, {"ok": True, "stream_id": sid, "chars": len(text),
                                "seconds": dt})


def start_body():
    srv = ThreadingHTTPServer(("127.0.0.1", BODY_PORT), Body)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ══════════════════════════════════════════════════════════════════════════
# 房子
# ══════════════════════════════════════════════════════════════════════════

def house_dir(tmp: Path) -> Path:
    return tmp / "house"


def house_env(home: Path, *, disabled=False, loop_url="") -> dict:
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
        # 🔴 ⑨ 要认身体在哪儿：与 backend/app.py 读**同一个**变量
        "RELAY_LOOP_INGEST_URL": loop_url or f"http://127.0.0.1:{BODY_PORT}/loop/ingest",
        "RELAY_BRAIN_FILE": str(home / "brain_target"),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        # 只留中转站一个供应商，指到假上游 —— 不碰真网络、不花一分钱
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini",
        "PROVIDER_RELAY_KEY": "sk-mock-generate",
        "PROVIDER_RELAY_BASE": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # 停止后打标的等待窗口调短一点，验收别干等 25 秒
        "GENERATE_TAG_WAIT": "10",
        # 🔴 子进程 stdout 落到日志文件；Windows 上不显式指定就是 cp936，
        #    而启动日志里只要有**一个 GBK 里没有的字符**就会抛
        #    UnicodeEncodeError —— 那段 print 在 register() 的 try 之外，
        #    后果是**房子起不来**。这里显式 utf-8（Zeabur 上本来就是 UTF-8）。
        "PYTHONIOENCODING": "utf-8",
    })
    if disabled:
        env["APP_EXT_GENERATE_DISABLED"] = "1"
    return env


def start_house(home: Path, port: int, *, disabled=False, loop_url=""):
    log_path = home / f"uvicorn-{port}.log"
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY)],
        env=house_env(home, disabled=disabled, loop_url=loop_url),
        stdout=logf, stderr=subprocess.STDOUT)
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


def seed_db(db_path: Path, *, with_reply=True) -> None:
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
    if with_reply:
        rows.append(("out", "reply", "好，周末去挑。我记着了。",
                     {"api_session": SID, "stream_id": "api-seed-0001"}))
    rows += [
        ("in", "user", OTHER_CANARY, {"api_session": SID2}),
        ("out", "reply", "另一个会话的回复，别串味。",
         {"api_session": SID2, "stream_id": "api-seed-0002"}),
        ("in", "user", "这条没有会话归属", {}),
    ]
    for i, (d, k, t, meta) in enumerate(rows):
        conn.execute("INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
                     (f"2026-09-19T20:00:{i:02d}+08:00", d, k, t,
                      json.dumps(meta, ensure_ascii=False)))
    conn.commit()
    conn.close()


def db_path_of(home: Path) -> Path:
    return home / "relay.db"


def q1(db: Path, sql: str, args=()):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def msgs(db: Path, sid=None):
    rows = q1(db, "SELECT id, ts, direction, kind, text, meta FROM messages ORDER BY id")
    out = []
    for r in rows:
        try:
            r["meta"] = json.loads(r["meta"] or "{}")
        except Exception:
            r["meta"] = {}
        if sid is None or str(r["meta"].get("api_session") or "") == sid:
            out.append(r)
    return out


def sig(db: Path, upto: int = 0):
    """正文指纹（跟 schema.messages_body_signature 同一套口径）。

    `upto > 0` → 只算 `id <= upto` 的那些行。
    🔴 **"停止没改已有内容"这条断言必须靠它**：不能拿"同一函数自比"糊弄
       （那是恒真的假断言）。做法 = 拿停止**前**算的指纹，跟停止**后**
       只取 `id <= 停止前的最大 id` 再算一遍 —— 两者必须逐字节相同。
    """
    import hashlib
    sql = ("SELECT id, ts, direction, kind, text FROM messages "
           + ("WHERE id <= ? " if upto else "") + "ORDER BY id")
    rows = q1(db, sql, ((int(upto),) if upto else ()))
    h = hashlib.sha256()
    for r in rows:
        for col in ("id", "ts", "direction", "kind", "text"):
            h.update(str(r[col]).encode("utf-8", "replace"))
            h.update(b"\x1f")
        h.update(b"\x1e")
    return h.hexdigest()


def wait_for(fn, timeout=15.0, step=0.3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def U(path: str, port=PORT_OK) -> str:
    return f"http://127.0.0.1:{port}{PREFIX}{path}"


def _gbk_ok_text(s: str) -> bool:
    """这一串能不能用 GBK 编出来。

    🔴 这条看着奇怪，其实是血泪：启动日志里只要有**一个** GBK 里没有的字符
       （⑨ / ✅ / ⚠️ …），Windows 上子进程 stdout（cp936）打它就会抛
       UnicodeEncodeError —— 而那段 print 在 `register()` 的 try **之外**，
       后果不是"少打一行日志"，是**房子起不来**。
    """
    try:
        str(s).encode("gbk")
        return True
    except UnicodeEncodeError:
        return False


def _gbk_ok(path: Path) -> bool:
    try:
        return _gbk_ok_text(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False


TOK = {"Authorization": None}


def auth(url, **kw):
    return req(url, token=SECRET, **kw)


# ══════════════════════════════════════════════════════════════════════════
# A · 纯逻辑
# ══════════════════════════════════════════════════════════════════════════

def part_a(tmp: Path) -> None:
    from app_ext import generate as GEN

    GEN._INFLIGHT.clear()
    chk("A1 没有在飞时 should_stop=False", GEN.should_stop(SID) is False)
    GEN.begin(SID)                     # ← 原来漏了这一句（A2~A6 全是假红）
    chk("A2 begin 之后 live 有一条", len(GEN.live()) == 1, f"live={GEN.live()}")

    fired = {"n": 0}
    GEN.register_notify(SID, lambda: fired.__setitem__("n", fired["n"] + 1))
    ok, why, target = GEN.request_stop(None)          # 不传 session
    chk("A3 🔴 不传 session 也能停到当前在飞的那个",
        ok is True and target == SID, f"target={target!r} why={why!r}")
    chk("A4 再停一次：ok 且说明已经在停了",
        GEN.request_stop(None)[0] is True and "已经在停" in GEN.request_stop(SID)[1])
    chk("A5 🔴 register_notify 的回调真被叫到（零延迟停止的关键）",
        fired["n"] >= 1, f"n={fired['n']}")
    chk("A6 should_stop 现在为 True", GEN.should_stop(SID) is True)

    GEN.end(SID, stopped=True)
    chk("A7 end() 之后 should_stop=False 且 live 空",
        GEN.should_stop(SID) is False and len(GEN.live()) == 0)
    chk("A8 没有在飞时 request_stop → ok=False（不抛）",
        GEN.request_stop(None)[0] is False)
    chk("A9 🔴 _key_of('') = 未知占位（认不出会话也能停当前那个）",
        GEN._key_of("") == GEN.UNKNOWN_KEY)

    GEN.begin(SID)
    GEN._INFLIGHT[GEN._key_of(SID)]["touched"] = time.time() - GEN.INFLIGHT_TTL - 10
    GEN.prune()
    chk("A10 TTL：超时没动的登记被 prune 掉", len(GEN.live()) == 0)

    # last_turn —— 认的是 api_session
    db = tmp / "a.db"
    seed_db(db)

    class Relay:
        DB_PATH = str(db)

    class GN:
        pass
    GN.connect = None
    found = GEN.last_turn(Relay(), SID)
    okA = (found.get("ok") is True
           and found["in"]["text"] == PROBE
           and found["out"]["text"].startswith("好，周末去挑"))
    chk("A11 🔴 last_turn 认 api_session：另一会话的消息不进上一次往返", okA,
        f"in={found.get('in', {}).get('text')!r} out={found.get('out', {}).get('text')!r}")

    db2 = tmp / "a2.db"
    seed_db(db2, with_reply=False)

    class Relay2:
        DB_PATH = str(db2)
    f2 = GEN.last_turn(Relay2(), SID)
    chk("A12 有 user 没回复 → no_reply_yet",
        f2.get("ok") is False and f2["error"]["code"] == "no_reply_yet",
        str(f2.get("error")))
    f3 = GEN.last_turn(Relay2(), "不存在的会话")
    chk("A13 不存在的会话 → no_session_rows",
        f3.get("ok") is False and f3["error"]["code"] == "no_session_rows",
        str(f3.get("error")))

    # ── "等那条落库再打标"的三个取行函数（⑨ 的写路径，最敏感的一段） ──────
    #    seed 出来的 id：1 in/SID 2 out/SID 3 in/SID 4 out/SID
    #                    5 in/SID2 6 out/SID2 7 in/无归属
    r1 = GEN._newest_reply_after(Relay(), 3, SID)
    chk("A16 _newest_reply_after 给了会话 → 只认这个会话的（不串味）",
        r1.get("id") == 4, f"id={r1.get('id')}")
    r2 = GEN._newest_reply_after(Relay(), 3, "")
    chk("A17 🔴 不给会话 → 只按 id 认（**有意的口子**，认不出会话时也要留痕；"
        "它确实会拿到最新那条，哪怕属于别的会话）",
        r2.get("id") == 6, f"id={r2.get('id')}")

    u1 = GEN._newer_user_row(Relay(), 2, SID)
    chk("A18 _newer_user_row 认出「新的你说的话」（= 新的一轮开始了）",
        u1.get("id") == 3, f"id={u1.get('id')}")
    u2 = GEN._newer_user_row(Relay(), 3, SID)
    chk("A19 没有更新的你说的话 → {}（正常情况：那半条正等着落库）",
        u2 == {}, f"u2={u2}")
    u3 = GEN._newer_user_row(Relay(), 6, "")
    chk("A19b 没会话归属的 in 行也算（第 7 行没有 api_session）",
        u3.get("id") == 7, f"id={u3.get('id')}")

    # ⚠️ A14 必须**显式把环境指到一个不存在的文件**再验 ——
    #    main() 里为了别的用例已经把 RELAY_BRAIN_FILE 指到真文件了，
    #    直接调 brain_target() 会拿到 "loop"，那是**用例自己的环境**，不是这条要验的东西。
    _keep_env = {k: os.environ.get(k) for k in ("RELAY_BRAIN_FILE", "RELAY_DB")}
    try:
        os.environ["RELAY_BRAIN_FILE"] = str(tmp / "没有这个文件" / "brain_target")
        os.environ.pop("RELAY_DB", None)
        bt = GEN.brain_target()
        chk("A14 brain_target() 读不到文件 → ''（不是抛）", bt == "", repr(bt))

        p = tmp / "bt_probe"
        p.write_text("loop", encoding="utf-8")
        os.environ["RELAY_BRAIN_FILE"] = str(p)
        chk("A14b brain_target() 认得出 loop / desktop",
            GEN.brain_target() == "loop", repr(GEN.brain_target()))

        p.write_text("desktop", encoding="utf-8")
        chk("A14c brain_target() 认得出 desktop（前端不叫身体，走房子自己）",
            GEN.brain_target() == "desktop", repr(GEN.brain_target()))

        p.write_text("乱写的东西", encoding="utf-8")
        chk("A14d brain_target() 认不出的取值 → ''（不瞎猜）",
            GEN.brain_target() == "", repr(GEN.brain_target()))
    finally:
        for k, v in _keep_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    chk("A15 loop_ingest_url() 读的是 RELAY_LOOP_INGEST_URL 同一个变量",
        GEN.loop_ingest_url() == os.environ.get(
            "RELAY_LOOP_INGEST_URL", "http://127.0.0.1:3020/loop/ingest"))


# ══════════════════════════════════════════════════════════════════════════
# B · 网关照停（从出口倒着验）
# ══════════════════════════════════════════════════════════════════════════

def _ingest_async(text, msg_id, session_id):
    """在后台线程里让假身体干一次活（模拟前端发消息 → 房子 → 身体）。"""
    th = threading.Thread(
        target=lambda: req(f"http://127.0.0.1:{BODY_PORT}/loop/ingest",
                           method="POST",
                           body={"id": msg_id, "text": text, "session_id": session_id},
                           timeout=90),
        daemon=True)
    th.start()
    return th


def part_b(db: Path, home: Path) -> None:
    SLOW["on"] = True

    # ── B 对照：不停止的一次，应当走完整 ───────────────────────────────
    BODY["last"] = {}
    reset_seen()
    th = _ingest_async(PROBE, 3, SID)
    th.join(timeout=60)
    full = BODY["last"] or {}
    chk("B1 不停止：假身体拿到了完整版",
        full.get("text") == FULL, f"got={full.get('text')!r}")
    chk("B2 不停止：网关发的是正常收尾（[DONE] + finish=stop）",
        full.get("saw_done") is True and full.get("finish") == "stop",
        f"saw_done={full.get('saw_done')} finish={full.get('finish')!r}")
    chk("B3 不停止：上游只被调用 1 次（没有 fallback）",
        num(seen, "count") == 1, f"count={seen.get('count')}")
    chk("B4 不停止：上游把 6 个碎片都吐了",
        num(seen, "emitted") == len(PIECES), f"emitted={seen.get('emitted')}")

    row = [m for m in msgs(db, SID) if m["direction"] == "out"][-1]
    chk("B5 不停止：完整落库且**不带** truncated",
        row["text"] == FULL and not row["meta"].get("truncated"),
        f"len={len(row['text'])} meta={row['meta']}")

    # ── B 主体：停止 ───────────────────────────────────────────────────
    before_ids = [m["id"] for m in msgs(db)]
    before_n = len(before_ids)
    before_max_id = max(before_ids)
    before_sig_upto = sig(db, before_max_id)      # 停止前那批行的正文指纹

    BODY["last"] = {}
    reset_seen()
    th = _ingest_async(PROBE, 3, SID)          # 又发一条（新的往返）
    time.sleep(1.0)                            # 让它流到一半
    st, stopbody = auth(U("/app/ext/generate/stop"), method="POST",
                        body={"session_id": SID})
    chk("B6 stop 端点 200 且 ok=True",
        st == 200 and stopbody.get("ok") is True, f"{st} {stopbody}")
    th.join(timeout=60)
    stopped = BODY["last"] or {}

    chk("B7 🔴 停止后网关发的是**正常收尾**（[DONE] + finish=stop）"
        "—— 这一条咬得住：两次变异（不补收尾 / 直接抛异常）它都变红了",
        stopped.get("saw_done") is True and stopped.get("finish") == "stop",
        f"saw_done={stopped.get('saw_done')} finish={stopped.get('finish')!r}")
    # ⚠️ B8 的**诚实说明**（别把它当"有牙齿"的断言看）：
    #    我试了两种变异想让这一条变红 —— ① 不补收尾 ② 停止分支直接 raise ——
    #    **都没红**。原因在 B16 那段注释里：房子在中间把上游和协议终止都兜住了，
    #    客户端永远读到一个"完整的短流"，压根不会进 `except Exception` → 不会 fallback。
    #    所以它现在是**回归守卫**：咬的是"将来有人把停止改成返回 HTTP 错误状态"
    #    这类改法（状态码才是 fallback 的唯一触发条件）。留着，但别高估它。
    chk("B8 🔴 停止后上游**只被调用 1 次**（他没有偷偷换模型重跑一遍）"
        "〔回归守卫：当前变异触发不了，见 B16〕",
        num(seen, "count") == 1,
        f"count={seen.get('count')} errors={BODY.get('errors')}")
    chk("B9 🔴 停止后上游不再被继续读（吐出的碎片数 < 全量）",
        num(seen, "emitted") is not None and num(seen, "emitted") < len(PIECES),
        f"emitted={seen.get('emitted')} / {len(PIECES)}")
    chk("B10 🔴 那半条落库了，而且比完整版短",
        0 < len(stopped.get("text") or "") < len(FULL),
        f"text={stopped.get('text')!r}")

    # 等打标（后台任务在轮询）
    tagged = wait_for(
        lambda: next((m for m in msgs(db, SID)
                      if m["direction"] == "out" and m["meta"].get("truncated")), None),
        timeout=15.0)
    chk("B11 🔴 落库那条 meta.truncated = true（库里留痕，导出看得见）",
        tagged is not None and tagged["text"] == stopped.get("text"),
        f"tagged={None if tagged is None else tagged['id']}")
    chk("B12 🔴 停止没删任何原文：老的 id 一个不少，行数只 +1",
        [i for i in before_ids if i not in [m["id"] for m in msgs(db)]] == []
        and len(msgs(db)) == before_n + 1,
        f"before={before_n} after={len(msgs(db))}")
    # 🔴 真比对（不是同一函数自比）：停止前那批行，停止后逐字节还算得出同一个指纹
    chk("B13 🔴 停止不改已有内容：停止前那批行的正文指纹**逐字节相同**",
        sig(db, before_max_id) == before_sig_upto,
        f"before={before_sig_upto[:16]} after={sig(db, before_max_id)[:16]}")
    chk("B14 🔴 那半条是完整版的**真前缀**（不是一个被拼歪的残段）",
        tagged is not None and 0 < len(tagged["text"]) < len(FULL)
        and FULL.startswith(tagged["text"]),
        f"tagged={None if tagged is None else tagged['text']!r}")
    chk("B15 🔴 打标是**合并**不是覆盖：那半条的 meta 里会话归属还在",
        tagged is not None and tagged["meta"].get("api_session") == SID,
        str(tagged["meta"]) if tagged else "")

    # ── B 对照②：上游在流中途炸掉，会怎样？（"温柔掐"到底防的是什么） ────
    #
    # 🔴 这一对是**两次变异测试逼出来的**，结论跟我一开始的因果**不一样**。
    #    我把过程记在这里，因为"为什么这么断言"比断言本身重要：
    #
    #    变异 #1：把"补 finish=stop + [DONE]"两行删掉（只留 `cancel() + break`）
    #      预期 B8 红。结果：**B7 红、B8 没红**。
    #      查代码：生成器**正常 return** 时 uvicorn 会规矩地结束 chunked body，
    #      客户端读到一个"完整的短流" → 不抛 → 不 fallback。
    #
    #    变异 #2：在停止分支直接 `raise RuntimeError`（模拟被真掐断）
    #      预期 B8 红。结果：**还是没红** —— B7 红而已，那半条连 B10/B11/B14 都照过。
    #      原因：Starlette 的 `StreamingResponse` 在生成器抛异常时，
    #      `finally` 里**仍会发完 chunked 终止块** → 客户端看到的还是一个完整短流。
    #
    #    ⇒ 于是有了两条**真的**结论，下面的断言就打在它们身上：
    #      ① **"停止"结构性不可能变成"换模型重跑"**：fallback 只在**流还没开**、
    #         由 HTTP 状态码触发（`FALLBACK_CODES`，`examples/api_loop.py:62`），
    #         而"停止"只发生在流**已经开着**之后，房子在中间把一切都兜住了。
    #      ② 🔴 **新发现的缺口**：**上游自己断掉时，身体会把半截当完整的收下，
    #         而且库里零痕迹** —— 屏幕上看起来"他说完了"，没有任何地方记得
    #         上游出过错。⑨ 的 stop 补了"用户主动停"那一半，这一半归
    #         `note_upstream_error()`（B18/B19 验它）。
    BROKEN["on"] = True
    BODY["last"] = {}
    reset_seen()
    th = _ingest_async(PROBE, 3, SID)
    th.join(timeout=60)
    chk("B16 🔴 上游在流中途协议级炸掉 → 房子**吸收**了它（身体不 fallback，上游只被调 1 次）",
        num(seen, "count") == 1 and seen.get("closed_early") is True
        and num(seen, "emitted") == 3,
        f"count={seen.get('count')} emitted={seen.get('emitted')} "
        f"closed_early={seen.get('closed_early')}")
    chk("B17 🔴 代价：身体把**半截**当完整的收了（saw_done=True、正文=前 3 片、"
        "meta 里原本什么都没有）",
        BODY["last"].get("saw_done") is True
        and (BODY["last"].get("text") or "") == "".join(PIECES[:3]),
        f"text={BODY['last'].get('text')!r} saw_done={BODY['last'].get('saw_done')}")
    BROKEN["on"] = False

    # 🔴 B18/B19：这条缺口现在被 ⑨ 的留痕机器补上了（与 stop 共用 `_tag_after`）
    up_tagged = wait_for(
        lambda: next((m for m in msgs(db, SID)
                      if m["direction"] == "out"
                      and m["meta"].get("upstream_error")), None),
        timeout=15.0)
    chk("B18 🔴 上游自己断掉的那半条**留痕了**（meta.upstream_error）"
        "—— 这正是「⑨ 只补了主动停那一半」的那个洞",
        up_tagged is not None
        and up_tagged["text"] == "".join(PIECES[:3]),
        f"tagged={None if up_tagged is None else up_tagged['id']}")
    chk("B19 🔴 两种留痕**不混**：upstream_error 那条**没有** truncated 标"
        "（一个是'用户按了停'，一个是'上游断了'，排查方向完全不同）",
        up_tagged is not None and not up_tagged["meta"].get("truncated")
        and up_tagged["meta"].get("api_session") == SID,
        str(up_tagged["meta"]) if up_tagged else "")

    SLOW["on"] = False


# ══════════════════════════════════════════════════════════════════════════
# C · retry / reroll
# ══════════════════════════════════════════════════════════════════════════

def part_c(db: Path, home: Path) -> None:
    SLOW["on"] = False
    before = msgs(db, SID)
    outs = [m for m in before if m["direction"] == "out"]
    old = outs[-1]
    chk("C1 retry 前：旧那条在、且还没 superseded",
        old is not None and not old["meta"].get("superseded"), str(old["meta"]))
    old_text = old["text"]
    n_before = len(before)
    # retry 应该拿"旧那条回复所回答的那条你说的话"去重跑
    exp_in = [m for m in before
              if m["direction"] == "in" and m["kind"] in ("user", "voice")][-1]

    BODY["last"] = {}
    reset_seen()
    st, res = auth(U("/app/ext/generate/retry"), method="POST",
                   body={"session_id": SID}, timeout=60)
    chk("C2 retry 端点 200 且 ok=True", st == 200 and res.get("ok") is True,
        f"{st} {res}")
    chk("C3 retry 报的 superseded_id = 旧那条",
        num(res, "superseded_id") == old["id"], f"{res.get('superseded_id')} vs {old['id']}")

    after = msgs(db, SID)
    old2 = next((m for m in after if m["id"] == old["id"]), None)
    chk("C4 🔴 旧那条**还在库里**（没被删）", old2 is not None)
    chk("C5 🔴 旧那条 text **一个字没改**（只动 meta）",
        old2 is not None and old2["text"] == old_text, "")
    chk("C6 🔴 旧那条被标 superseded=true",
        old2 is not None and old2["meta"].get("superseded") is True,
        str(old2["meta"]) if old2 else "")
    new_outs = [m for m in after if m["direction"] == "out" and m["id"] > old["id"]]
    chk("C7 🔴 新的一条 out 出现，内容是完整版",
        len(new_outs) == 1 and new_outs[0]["text"] == FULL,
        f"n={len(new_outs)} text={new_outs[0]['text'][:20] if new_outs else None!r}")
    chk("C8 🔴 行数只 +1（新回复），一条都没少",
        len(after) == n_before + 1, f"{n_before} → {len(after)}")
    chk("C9 🔴 假身体收到的是**同一条你说的话**（id 与原文都对）",
        BODY["calls"] and BODY["calls"][-1]["payload"].get("text") == exp_in["text"]
        and BODY["calls"][-1]["payload"].get("session_id") == SID
        and BODY["calls"][-1]["payload"].get("id") == exp_in["id"],
        f"exp={exp_in['id']}:{exp_in['text']!r} "
        f"got={BODY['calls'][-1]['payload'] if BODY['calls'] else 'none'}")
    chk("C10 retry 之后上游只被调用 1 次（重答走的是身体，不是房子自己造）",
        num(seen, "count") == 1, f"count={seen.get('count')}")

    # ── reroll ─────────────────────────────────────────────────────────
    outs_before_rr = len([m for m in msgs(db, SID) if m["direction"] == "out"])
    reset_seen()
    st, res = auth(U("/app/ext/generate/reroll"), method="POST",
                   body={"session_id": SID}, timeout=60)
    chk("C11 reroll 端点 200 且 ok=True", st == 200 and res.get("ok") is True, f"{st}")
    tagged = next((m for m in msgs(db, SID)
                   if m["id"] == (num(res, "superseded_id") or -1)), None)
    chk("C12 🔴 reroll 打的是 variant 标（前端 ‹1/2› 切换的依据）",
        tagged is not None and tagged["meta"].get("variant") is True
        and tagged["meta"].get("superseded") is True,
        str(tagged["meta"]) if tagged else "")
    outs_after_rr = [m for m in msgs(db, SID) if m["direction"] == "out"]
    chk("C13 🔴 reroll 之后旧版本一条不删：out 条数只 +1，老 id 全在",
        len(outs_after_rr) == outs_before_rr + 1
        and all(any(m["id"] == o["id"] for m in outs_after_rr) for o in outs),
        f"{outs_before_rr} → {len(outs_after_rr)}")

    # ── 错误路径 ───────────────────────────────────────────────────────
    st, res = auth(U("/app/ext/generate/retry"), method="POST",
                   body={"session_id": "不存在的会话"})
    chk("C14 不存在的会话 → 400 + no_session_rows",
        st == 400 and res.get("error", {}).get("code") == "no_session_rows",
        f"{st} {res}")
    st, res = auth(U("/app/ext/generate/retry"), method="POST", body={})
    chk("C15 不传 session → 400 + no_session",
        st == 400 and res.get("error", {}).get("code") == "no_session",
        f"{st} {res}")

    # 桌面模式
    (home / "brain_target").write_text("desktop", encoding="utf-8")
    st, res = auth(U("/app/ext/generate/retry"), method="POST", body={"session_id": SID})
    chk("C16 桌面模式 → 400 + brain_desktop（给的是好报错，不是崩）",
        st == 400 and res.get("error", {}).get("code") == "brain_desktop",
        f"{st} {res}")
    (home / "brain_target").write_text("loop", encoding="utf-8")

    # 身体叫不动：把 ingest URL 换不了（进程启动时定的），改用"停掉假身体"不现实
    # → 直接验 generate 的失败分支：用一个指向死端口的会话（靠对 loop_ingest_url 打桩）
    st, res = auth(U("/app/ext/generate/status?session_id=" + SID))
    chk("C17 status 只读：报 brain_target + guard + 上一次往返",
        st == 200 and res.get("ok") is True
        and res.get("brain_target") == "loop"
        and isinstance(res.get("guard"), dict)
        and num(res.get("last_turn") or {}, "reply_message_id"),
        f"{st} {str(res)[:180]}")


# ══════════════════════════════════════════════════════════════════════════
# D · 红线 / 接线 / 开关
# ══════════════════════════════════════════════════════════════════════════

def part_d(tmp: Path, log_path: Path) -> None:
    import app_ext
    from app_ext import generate as _g9
    from app_ext import schema as _sch

    src = (DEPLOY / "app_ext" / "generate.py").read_text(encoding="utf-8")
    init_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")

    for r in ("/app/ext/generate/stop", "/app/ext/generate/retry",
              "/app/ext/generate/reroll", "/app/ext/generate/status"):
        chk(f"D1 _ROUTES 里有 {r}", r in init_src, "")
    chk("D2 register 里有第 ⑨ 步（generate.install）",
        "generate as _generate" in init_src and "_gn()" in init_src, "")

    # 🔴 源码扫描：对 messages 的写语句只准是 `SET meta = ?`
    writes = re.findall(r"UPDATE\s+messages[^\"'\n]*", src, re.I)
    writes += re.findall(r"UPDATE\s+messages[^\"'\n]*",
                         (DEPLOY / "app_ext" / "schema.py").read_text(encoding="utf-8"),
                         re.I)
    bad = [w for w in writes if not re.search(r"SET\s+meta\s*=", w, re.I)]
    chk("D3 🔴 扩展层对 messages 的写语句**只有** `SET meta = ?`（无越界写法）",
        len(writes) >= 1 and not bad, f"writes={writes} bad={bad}")
    chk("D4 🔴 源码里没有 DELETE / DROP / ALTER 打 messages 的写法",
        not re.search(r"(DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\s+messages",
                      src + (DEPLOY / "app_ext" / "schema.py").read_text(encoding="utf-8"),
                      re.I), "")

    # 🔴 meta 守卫本身（跟 schema 那套独立再验一遍）
    db = tmp / "d.db"
    seed_db(db)

    class Relay:
        DB_PATH = str(db)
    def _body_sig():
        c = _sch.connect(Relay())
        try:
            return _sch.messages_body_signature(c)
        finally:
            c.close()

    try:
        def _evil(conn):
            conn.execute("UPDATE messages SET text='HACKED' WHERE id=1")
        _sch.with_meta_guard(Relay(), _evil)
        chk("D5 🔴 meta 守卫挡住改正文", False, "没拦住！")
    except RuntimeError as e:
        chk("D5 🔴 meta 守卫挡住改正文", "红线被破坏" in str(e), "")
    chk("D6 🔴 被拦之后已回滚（正文仍是原文）",
        [m for m in msgs(db) if m["id"] == 1][0]["text"] == "今天想聊聊盐系手帐风", "")

    sig_before = _body_sig()
    chk("D7 meta 守卫允许只写 meta",
        _sch.update_message_meta(Relay(), 1, {"truncated": True}) is True
        and msgs(db)[0]["meta"].get("truncated") is True, "")
    chk("D8 🔴 只写 meta **不动正文指纹**（真比对，不是自比）",
        _body_sig() == sig_before, f"{sig_before[:16]} → {_body_sig()[:16]}")
    # 🔴 这个数字**有意写死**：它守的是"**没有人偷偷动表结构**"。
    #    将来谁**有意**改了表结构，就来把这里的期望值改掉，并在 commit message 里说明
    #    —— 一次有意的版本升级要留下一次有意的改动记录。
    #    ⚠️ 2026-09-20：⑩-a 给 `memories` 加 `source` → 3 → 4，本行随之更新
    #       （这是"有意升级"的正常流程，不是本套坏了）。
    chk("D9 schema 版本 = 4（⑩-a 的 memories.source；有意升级就来改这里）",
        _sch.SCHEMA_VERSION == 4, f"={_sch.SCHEMA_VERSION}")
    chk("D10 逃生开关在（APP_EXT_GENERATE_DISABLED）",
        "APP_EXT_GENERATE_DISABLED" in init_src
        and "APP_EXT_GENERATE_DISABLED" in src, "")

    va = (HERE / "verify_all.py").read_text(encoding="utf-8")
    chk("D11 verify_all 已接本套", "generate_check" in va, "")

    # 🔴 ⑨ 补的那个缺口：上游自己断掉也要留痕
    lr_src = (DEPLOY / "app_ext" / "llm_routes.py").read_text(encoding="utf-8")
    chk("D16 🔴 「上游自己断」的留痕已接进网关的错误分支",
        "note_upstream_error" in src and "note_upstream_error" in lr_src, "")
    chk("D17 🔴 两种留痕是**两把钥匙**，不许合并成一个字段",
        "upstream_error" in src and "truncated" in src
        and "upstream_error" != "truncated", "")
    # ⚠️ 顺序守卫：`_newer_user_row` 必须在 `_newest_reply_after` **之前**被调 ——
    #    反了就会把"下一轮的回复"误标成 truncated / upstream_error（往库里写假痕迹）。
    _i_u = src.find("_newer_user_row(relay, after_id, session_id)")
    _i_r = src.find("target = _newest_reply_after(relay, after_id, session_id)")
    chk("D18 🔴 `_tag_after` 里先查「新的一轮开始了没有」，再取那条回复（顺序反了就会写假痕迹）",
        -1 < _i_u < _i_r, f"i_user={_i_u} i_reply={_i_r}")
    chk("D19 🔴 认不出会话也照样排打标（stop 里不再有 `if target:` 那道短路）",
        "if target:" not in src, "")

    # 关开关的房子
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    hits = [l for l in log.splitlines() if "停止/重答/多版本就绪" in l]
    chk("D12 启动日志里有 ⑨ 那行（且 GBK 安全：不带 ⑨ 符号）",
        bool(hits), f"log_tail={log[-200:]!r}")
    chk("D13 🔴 启动日志整段都是 GBK 可编码的（不然房子起不来）",
        _gbk_ok(log_path), "日志里有 GBK 编不出的字符")
    chk("D14 summary_line() 本身 GBK 安全",
        _gbk_ok_text(_g9.summary_line()), repr(_g9.summary_line()[:40]))

    st, _b = auth(U("/app/ext/generate/stop", port=PORT_OFF), method="POST", body={})
    st2, _b2 = auth(U("/app/ext/generate/status", port=PORT_OFF))
    # 🔴 关掉开关 = 这四条路由**根本没挂上**。两个方向都验，别只验 GET：
    #    · GET  status → 404（StaticFiles 兜底）
    #    · POST stop   → **405**，不是 404 —— 因为房子把 `web/` 静态目录挂在 `/`，
    #      Starlette 的 StaticFiles 对非 GET/HEAD 一律直接回 405。
    #      这是**房子的既有行为**（⑧ 的 D9 里 POST 打得中路由所以看到 200，
    #      这里打不中才露出来），不是 ⑨ 的错。所以接受 404/405，只拒绝"还活着"。
    chk("D15 🔴 关掉开关的房子：端点不再存在（GET 404 / POST 405 是静态目录兜底）",
        st in (404, 405) and st2 == 404, f"stop={st} status={st2}")

    # 🔴 真正的"少了能力但照常营业"：同一条链路上**别的能力还在用** ——
    #    直接走模型网关发一次（⑧ 那套也是这么验的）。gateway 活着 = 房子在营业。
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": PERSONA},
                         {"role": "user", "content": "在吗"}],
            "temperature": 0.8, "max_tokens": 64, "stream": False}
    st3, r3 = auth(U("/app/ext/llm/v1/chat/completions", port=PORT_OFF),
                   method="POST", body=body, timeout=60)
    chk("D15b 🔴 ⑨ 关掉之后：同一条链路上的模型网关**照常能说话**（房子没塌）",
        st3 == 200 and bool((r3.get("choices") or [{}])[0]),
        f"{st3} {str(r3)[:160]}")


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="kael-gencheck-"))
    home = house_dir(tmp)
    home.mkdir(parents=True, exist_ok=True)
    (home / "brain_target").write_text("loop", encoding="utf-8")
    (home / "uploads").mkdir(exist_ok=True)
    (home / "workshop").mkdir(exist_ok=True)
    db = db_path_of(home)
    seed_db(db)

    print("=" * 66)
    print(" 停止 / 重答 / 多版本验收 —— P2 ⑨")
    print("=" * 66)

    mock = start_mock()
    body = start_body()
    procs = []
    try:
        proc, logf, log_path = start_house(home, PORT_OK)
        procs.append((proc, logf))
        if not wait_port(PORT_OK):
            print("[问题] 房子没起来，看日志：", log_path)
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1

        home_off = tmp / "house_off"
        home_off.mkdir(parents=True, exist_ok=True)
        (home_off / "brain_target").write_text("loop", encoding="utf-8")
        (home_off / "uploads").mkdir(exist_ok=True)
        (home_off / "workshop").mkdir(exist_ok=True)
        seed_db(db_path_of(home_off))
        proc2, logf2, _lp2 = start_house(home_off, PORT_OFF, disabled=True)
        procs.append((proc2, logf2))
        if not wait_port(PORT_OFF):
            print("[问题] 关开关的房子没起来")
            return 1

        os.environ["RELAY_LOOP_INGEST_URL"] = f"http://127.0.0.1:{BODY_PORT}/loop/ingest"
        os.environ["RELAY_BRAIN_FILE"] = str(home / "brain_target")

        part_a(tmp)
        part_b(db, home)
        part_c(db, home)
        part_d(tmp, log_path)

    finally:
        for p, f in procs:
            stop_house(p, f)
        try:
            mock.shutdown()
        except Exception:
            pass
        try:
            body.shutdown()
        except Exception:
            pass

    npass = sum(1 for _n, ok, _d in results if ok)
    # 存档（与别的套同一个约定：报告落在 tools/ 下，`verify_all` 之后能翻）
    _out = os.environ.get("GENERATE_CHECK_OUT") or str(HERE / "generate_report.txt")
    try:
        Path(_out).write_text(
            "\n".join(f"{'[PASS]' if ok else '[FAIL]'} {n}"
                      + (f"   {d}" if (d and not ok) else "")
                      for n, ok, d in results)
            + f"\n\n共 {len(results)} 项，通过 {npass}，失败 {len(results) - npass}\n",
            encoding="utf-8")
    except Exception:
        pass
    print("")
    print("-" * 66)
    for name, ok, detail in results:
        mark = "[OK]" if ok else "[!!]"
        line = f"{mark} {name}"
        if not ok and detail:
            line += f"   <<< {detail}"
        print(line)
    print("-" * 66)
    print(f"共 {len(results)} 项，通过 {npass}，失败 {len(results) - npass}   "
          f"（{time.time() - t0:.1f}s）")
    print(f"报告已存：{_out}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
