#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上下文管理验收 —— P2 ⑧（热区 + 滚动摘要）
==========================================================================

## 这一套到底在守什么

⑧ 改的是**读法** —— 最容易出的一种病叫「**内部都对、出口不对**」：
函数返回 200、日志一切正常、库里摘要也写进去了，可**上游真正收到的那份请求**
压根没带上摘要（或者带上了但把它当 user 说了一句话）。

P2-0 已经吃过一次同款的亏：`iter_jsonl` 每条都对，`StreamingResponse` 出去却
**18 条粘成 1 行** —— 53 项验收全绿，因为她真导一份才发现。
（教训已写进 `kael-kernel-verify` 技能：**断言要打在真实出口的字节上**。）

所以这一套的核心手法只有一个：**假上游把收到的 body 原样记下来**，
然后拿它跟"没注入时的那份"逐字段比：

  · 摘要**在**上游收到的 system 里吗？（不是在函数的返回值里）
  · 人格仍在摘要**前面**吗？
  · `messages` 数组**与没注入时逐字节相同**吗？（网关不裁历史）
  · 别的会话的内容**串进来没有**？
  · 上游**没**多收到我们发明的字段吗？

## 覆盖清单

  A. 纯逻辑（不起 HTTP、不碰库）
     1-3   估算 token：中文 / 英文 / 空
     4-6   分界线：keep 很大→全在热区；keep=0→全可压；中段→切分正确
     7     🔴 热区 token 真的 ≤ keep（规则不是"大概齐"）
     8     注入文本带 `MARK`（防重复插入的依据）
     8b    🔴 注入包装语说清"这是你自己的记忆"（不是"系统提示"）
     9     🔴 材料人称 = **他本人**（out→「我」、in→「你」），不是 user / assistant
     9b    材料里不出现 user / assistant / 用户
     9c    材料头部把人称讲明白（不讲，压缩器就自己猜 → 旧版就是这么漂成档案腔的）
     10    🔴 压缩提示里带着"不许补充/推测"那条红线
     11    🔴 人称规约**写进了系统提示**（只改材料标签模型会漂回去）
     12    🔴 系统提示点名禁用第三人称档案腔（用户 / 对方 / assistant）
     13    系统提示禁止写成信件 / 独白 / 对话

  B. 注入（起真房子 + 假上游）—— **从出口倒着验**
     1     🔴 200 且上游真收到了请求
     2     🔴 摘要进了**上游收到的 system**
     3     🔴 人格在摘要**前面**（system 内部顺序：人格 → 上下文）
     4     🔴 `messages` 与没注入时**逐条相同、顺序不变**（只插不删）
     5     🔴 没有摘要 → 上游收到的 body 与发出去的一模一样
     6     🔴 库里查不到的文本 → 不注入（**不猜**）
     7     已带 MARK → 不重复插入
     8     🔴 上游没多收我们发明的字段（键集合不变）
     9     🔴 **别的会话的内容不串味**
     10    流式端点（`/llm/chat`）同样注入
     11    🔴 认的是 `api_session`：另一会话的探测文本取不到本会话摘要

  C. summarize 端点
     1-2   status 无密钥 401；有密钥 200 且报阈值来源
     3     🔴 `dry` 不调上游、不写库
     4     🔴 没到阈值 → 不触发、**一次上游都不调**
     5-6   `force` 真跑：摘要写进 `sessions.summary`
     7     `summary_upto` = 最后一条被压消息的 id
     8     🔴 **原文一条不删**（行数与 id 集合都不变）
     9     init 与压缩都**没碰** messages 行
     10    🔴 第二次是增量：已压过的消息不再喂
     11    端到端：压完 → 再注入 → 上游 system 里是新摘要
     12    🔴 `rebuild` 的 dry：忽略 `summary_upto`、忽略老摘要（对比 §10 的 0 条）
     13    🔴 `rebuild` 真跑：**老摘要不被喂回去**、老消息重新进来、摘要被覆盖
     14    🔴 `rebuild` 之后原文仍然一条没删（"覆盖写"最容易被怀疑丢东西）

  D. 接线 / 迁移 / 红线 / 开关
     1     register 摘要含 context；`_ROUTES` 两条都在
     2-3   🔴 **不是房间**：不 import mcp、不碰 KaelLife
     4     🔴 原文不删：源码里没有对 messages 的写语句
     5     🔴 v2 老库 → 起服务自动补 `summary_upto` 且 user_version=3
     6     🔴 老的 `settings.provider_id` 迁移仍在（别把上一版弄丢）
     7-8   逃生开关在；verify_all 里接了本套
     9     🔴 关掉开关的房子：注入不发生、端点 404

⚠️ 本套用端口 8800 / 8801（假上游）/ 8802（关开关的房子），不与别套抢。
用法：.venv\\Scripts\\python.exe tools\\context_check.py
"""
import json
import os
import re
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

# 🔴 本机 shell 里 HTTP_PROXY 常被注进来；httpx/urllib 会因此把 127.0.0.1 塞进代理。
for _v in ("NO_PROXY", "no_proxy"):
    os.environ.setdefault(_v, "127.0.0.1,localhost")

SECRET = "test-secret-context-0123456789"
PREFIX = "/relay"
PORT_OK = 8800          # 有数据的库（正常路径）
MOCK_PORT = 8801        # 假上游
PORT_OFF = 8802         # 关掉上下文层的房子

MODEL = "mock-ctx-1"
PERSONA = "你是住在这个房子里的人，说话像平时那样。"
SID = "api-20260919-120000-context"
SID2 = "api-20260919-130000-other"
SUM_CANARY = "他答应周末陪她挑纸胶带"
SUM_SEED = SUM_CANARY + "。她还欠他一张明信片。"
OTHER_CANARY = "另一个会话里的暗号：雾绿与雾蓝"
PROBE = "那我们周末就去挑纸胶带吧"
PROBE2 = "这条属于另一个会话"
# ⚠️ 假上游返回的摘要必须与种子摘要**不共享子串** —— 否则 C11 那条
#    "新摘要进来了、旧摘要走了" 会因为她俩长得像而永远绿（假绿）。真踩过。
MOCK_REPLY = "摘要已更新：偏好、约定与待办都记下了。"
MOCK_CANARY = "摘要已更新"
MARK_SNIPPET = "本会话较早部分的摘要"
TS = "2026-09-19T12:00:00+08:00"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def num(d, key):
    """取一个数字字段（字段不存在/不是数字 → None）。

    🔴 **别写 `d.get(k) or -1`**：合法的 **0**（比如 `foldable_rows=0`、
    `prev_summary_chars=0`）是 falsy，会被当成"缺失"→ 断言假红。
    2026-09-19 真踩（C10/C12 两条红全是这个，被测对象是好的）。
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
# 假上游：把收到的 body 原样记下来（这一套的"眼睛"）
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

        if self.path.rstrip("/").endswith("/chat/completions"):
            text = MOCK_REPLY
            if body.get("stream"):
                return self._sse([
                    "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]},
                                          ensure_ascii=False) + "\n\n",
                    "data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                                        "finish_reason": "stop"}],
                                           "usage": {"prompt_tokens": 9, "completion_tokens": 9}},
                                          ensure_ascii=False) + "\n\n",
                    "data: [DONE]\n\n",
                ])
            return self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
                "model": body.get("model") or "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 9},
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
    for m in upstream_msgs():
        if str(m.get("role")) == "system":
            return str(m.get("content") or "")
    return ""


# ══════════════════════════════════════════════════════════════════════════
# 房子
# ══════════════════════════════════════════════════════════════════════════

def house_dir(tmp: Path) -> Path:
    return tmp / "house"


def house_env(home: Path, *, context_disabled=False) -> dict:
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
        "PROVIDER_RELAY_KEY": "sk-mock-context",
        "PROVIDER_RELAY_BASE": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    if context_disabled:
        env["APP_EXT_CONTEXT_DISABLED"] = "1"
    return env


def start_house(home: Path, port: int, *, context_disabled=False):
    log_path = home / f"uvicorn-{port}.log"
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY)],
        env=house_env(home, context_disabled=context_disabled),
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


def seed_db(db_path: Path) -> None:
    """造一张跟后端一样的 messages + 一个**老形状**的 sessions（故意不带 summary_upto）。

    🔴 故意按 v1 的形状建 sessions —— 顺便把"v2 老库自动补列"这条迁移一起验了。
    """
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
        ("in", "user", "对，低饱和墨蓝配赭石，别太亮", {"api_session": SID}),
        ("out", "reply", "记住这个偏好了，做页面就照这个来", {"api_session": SID}),
        ("in", "user", "还有那本手帐，我想用来抄诗", {"api_session": SID}),
        ("out", "reply", "记得，你说过要挑一本纸不洇墨的", {"api_session": SID}),
        ("in", "user", PROBE, {"api_session": SID}),
        # 另一个会话（验"不串味"）
        ("in", "user", OTHER_CANARY, {"api_session": SID2}),
        ("in", "user", PROBE2, {"api_session": SID2}),
        # 无归属的那类（身体把它当 __legacy__ 桶）
        ("in", "user", "这条没有会话归属", {}),
    ]
    for i, (d, k, t, meta) in enumerate(rows):
        conn.execute("INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
                     (f"2026-09-19T12:00:{i:02d}+08:00", d, k, t,
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
    conn.execute("INSERT INTO sessions (id,user_id,title,since_id,pinned,summary,archived,"
                 "created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                 (SID2, "u_owner", None, 0, 0, None, 0, TS, TS))
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


def base_body(probe: str) -> dict:
    """一份"身体会发出来的"OpenAI 风格 body（最后一条 user = 探测文本）。"""
    return {
        "model": MODEL,
        "provider_id": "relay",
        "stream": False,
        "messages": [
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": "今天想聊聊盐系手帐风"},
            {"role": "assistant", "content": "米白底、细线分隔、留白多那种？"},
            {"role": "user", "content": probe},
        ],
    }


LLM_URL = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/v1/chat/completions"
CTX_STATUS = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/context/status"
CTX_SUM = f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/context/summarize"


# ══════════════════════════════════════════════════════════════════════════
# A 组：纯逻辑
# ══════════════════════════════════════════════════════════════════════════

def part_a() -> None:
    import app_ext.context as C

    chk("A1 估算 token：8 个汉字 = 8",
        C.estimate_tokens("盐系手帐风格温柔") == 8 and len("盐系手帐风格温柔") == 8,
        str(C.estimate_tokens("盐系手帐风格温柔")))
    chk("A2 估算 token：8 个拉丁字母 = 2",
        C.estimate_tokens("abcdefgh") == 2, str(C.estimate_tokens("abcdefgh")))
    chk("A3 估算 token：空串 = 0",
        C.estimate_tokens("") == 0 and C.estimate_tokens(None) == 0, "")

    rows = [{"id": i + 1, "text": "一二三四五六七八九十"} for i in range(10)]   # 每条 10 token
    b, hot = C.plan_boundary(rows, 1000)
    chk("A4 keep 很大 → 全在热区（boundary=0）", b == 0 and hot == 100, f"b={b} hot={hot}")
    b, hot = C.plan_boundary(rows, 0)
    chk("A5 keep=0 → 全可压（boundary=len）", b == 10 and hot == 0, f"b={b} hot={hot}")
    b, hot = C.plan_boundary(rows, 35)
    chk("A6 中段切分：keep=35 → 热区 3 条、可压 7 条",
        len(rows) - b == 3 and b == 7, f"b={b} hot={hot}")
    chk("A7 🔴 热区 token 真的 ≤ keep（不是大概齐）",
        C.plan_boundary(rows, 35)[1] <= 35 and C.plan_boundary(rows, 35)[1] + 10 > 35,
        str(C.plan_boundary(rows, 35)))

    txt = C.inject_text(SUM_SEED)
    chk("A8 注入文本带 MARK（防重复插入的依据）",
        C.MARK in txt and SUM_CANARY in txt, txt[:60])
    chk("A8b 🔴 注入包装语说清「这是你自己的记忆」",
        "你自己的记忆" in txt, txt[:60])

    mat = C.build_summary_material("", [{"id": 1, "direction": "in", "text": "你好"},
                                        {"id": 2, "direction": "out", "text": "在的"}])
    chk("A9 🔴 材料人称 = 他本人（out→「我」、in→「你」）",
        "我：在的" in mat and "你：你好" in mat, mat[:120])
    chk("A9b 材料里不出现 user / assistant / 用户",
        "assistant" not in mat and "user" not in mat and "用户" not in mat, mat[:120])
    chk("A9c 材料头部把人称讲明白（不讲 → 压缩器自己猜 → 旧版漂成档案腔）",
        "「我」= Kael" in mat and "「你」=" in mat, mat[:80])
    chk("A10 🔴 压缩提示里有「不许补充/推测」那条红线",
        "不许补充" in C.SUMMARY_SYSTEM and "不许推测" in C.SUMMARY_SYSTEM, "")
    chk("A11 🔴 人称规约**写进了系统提示**（只改材料标签，模型会漂回去）",
        "「我」指 Kael" in C.SUMMARY_SYSTEM and "「你」指" in C.SUMMARY_SYSTEM, "")
    chk("A12 🔴 系统提示点名禁用第三人称档案腔",
        "用户 / 对方 / assistant" in C.SUMMARY_SYSTEM, "")
    chk("A13 系统提示禁止写成信件 / 独白 / 对话",
        "信件" in C.SUMMARY_SYSTEM and "独白" in C.SUMMARY_SYSTEM, "")


# ══════════════════════════════════════════════════════════════════════════
# B 组：注入（从出口倒着验）
# ══════════════════════════════════════════════════════════════════════════

def part_b(db: Path) -> None:
    body = base_body(PROBE)
    reset_seen()
    st, out = req(LLM_URL, method="POST", token=SECRET, body=body)
    chk("B1 🔴 走一次流式端点的非流式分支：200 且上游真收到",
        st == 200 and seen.get("count") == 1, f"{st} count={seen.get('count')}")

    sysmsg = upstream_system()
    chk("B2 🔴 摘要在**上游收到的 system** 里（不是函数返回值里）",
        SUM_CANARY in sysmsg, sysmsg[:100])
    chk("B3 🔴 人格在摘要**之前**（顺序 = 人格 → 上下文）",
        PERSONA in sysmsg and 0 <= sysmsg.find(PERSONA) < sysmsg.find(SUM_CANARY),
        f"p={sysmsg.find(PERSONA)} s={sysmsg.find(SUM_CANARY)}")

    up = upstream_msgs()
    sent_wo_sys = [m for m in body["messages"] if m["role"] != "system"]
    up_wo_sys = [m for m in up if str(m.get("role")) != "system"]
    chk("B4 🔴 messages 与没注入时**逐条相同、顺序不变**（只插不删）",
        up_wo_sys == sent_wo_sys, f"up={len(up_wo_sys)} sent={len(sent_wo_sys)}")

    # ⑤ 没有摘要 → 一个字段都不改
    b2 = base_body(PROBE2)
    reset_seen()
    st, _ = req(LLM_URL, method="POST", token=SECRET, body=b2)
    baseline_keys = seen.get("keys")
    same = upstream_msgs() == [{"role": "system", "content": PERSONA}] + b2["messages"][1:]
    chk("B5 🔴 没有摘要 → 上游收到的与发出去的**一模一样**",
        st == 200 and same, json.dumps(upstream_msgs(), ensure_ascii=False)[:120])

    # ⑥ 认不出会话 → 不注入（不猜）
    b3 = base_body("库里根本没有这句话")
    reset_seen()
    st, _ = req(LLM_URL, method="POST", token=SECRET, body=b3)
    chk("B6 🔴 库里查不到的文本 → 不注入（**不猜**）",
        st == 200 and SUM_CANARY not in upstream_system(),
        upstream_system()[:80])

    # ⑦ 已带 MARK → 不重复插
    b4 = base_body(PROBE)
    b4["messages"].insert(1, {"role": "system", "content": f"[{MARK_SNIPPET}] 早就有了"})
    reset_seen()
    st, _ = req(LLM_URL, method="POST", token=SECRET, body=b4)
    chk("B7 已带 MARK → 不重复插入（MARK 只出现一次）",
        st == 200 and upstream_system().count(MARK_SNIPPET) == 1,
        str(upstream_system().count(MARK_SNIPPET)))

    chk("B8 🔴 上游没多收我们发明的字段（键集合不变）",
        seen.get("keys") == baseline_keys,
        f"{seen.get('keys')} vs {baseline_keys}")

    chk("B9 🔴 别的会话的内容不串味（system 里没有 SID2 的暗号）",
        OTHER_CANARY not in upstream_system(), "")

    # ⑩ 流式端点
    b5 = base_body(PROBE)
    b5["stream"] = True
    reset_seen()
    st, out = req(f"http://127.0.0.1:{PORT_OK}{PREFIX}/app/ext/llm/chat",
                  method="POST", token=SECRET, body=b5)
    chk("B10 流式端点（/llm/chat）同样注入",
        st == 200 and SUM_CANARY in upstream_system(), str(st))

    # ⑪ 认的是 api_session：拿 SID2 的探测文本取不到 SID 的摘要
    b6 = base_body(PROBE2)
    reset_seen()
    req(LLM_URL, method="POST", token=SECRET, body=b6)
    chk("B11 🔴 会话按 `api_session` 认（SID2 的文本取不到 SID 的摘要）",
        SUM_CANARY not in upstream_system(), "")


# ══════════════════════════════════════════════════════════════════════════
# C 组：summarize 端点
# ══════════════════════════════════════════════════════════════════════════

def part_c(db: Path) -> None:
    ids_before = [r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT id FROM messages ORDER BY id").fetchall()]
    n_before = len(ids_before)

    st, out = req(CTX_STATUS, token=None)
    chk("C1 status 无密钥 → 401/403", st in (401, 403), str(st))
    st, out = req(CTX_STATUS + f"?session_id={SID}", token=SECRET)
    chk("C2 status 有密钥 → 200，报出阈值与来源",
        st == 200 and out.get("ok") and out["session"]["trigger_tokens"] > 0
        and out.get("thresholds_from"), json.dumps(out, ensure_ascii=False)[:120])

    # ③ dry：不调上游、不写库
    #    ⚠️ 必须显式给 `keep` —— `force` 只越过**触发线**，不越过**热区分界线**。
    #       默认 keep=8000 token 远大于这个小会话，所以"没有东西可压"是**对的**，
    #       不是 bug（这条语义写在 `summarize` 的 docstring 里）。
    reset_seen()
    st, out = req(CTX_SUM, method="POST", token=SECRET,
                  body={"session_id": SID, "force": True, "dry": True, "keep": 30})
    chk("C3 🔴 dry 只算不写：不调上游、不写摘要",
        st == 200 and out.get("reason") == "dry_run" and seen.get("count") is None
        and q1(db, "SELECT summary FROM sessions WHERE id=?", (SID,)) == SUM_SEED,
        f"{st} {out.get('reason')} count={seen.get('count')}")

    # ④ 没到阈值 → 不触发
    reset_seen()
    st, out = req(CTX_SUM, method="POST", token=SECRET,
                  body={"session_id": SID, "trigger": 10 ** 9})
    chk("C4 🔴 没到阈值 → 不触发、**一次上游都不调**",
        out.get("reason") == "under_trigger" and seen.get("count") is None,
        f"{out.get('reason')} count={seen.get('count')}")

    # ⑤⑥ 真跑
    reset_seen()
    st, out = req(CTX_SUM, method="POST", token=SECRET,
                  body={"session_id": SID, "force": True, "keep": 30})
    got = q1(db, "SELECT summary FROM sessions WHERE id=?", (SID,))
    chk("C5 🔴 force 真跑：上游被调了，且摘要写进 sessions.summary",
        st == 200 and seen.get("count") == 1 and got == MOCK_REPLY,
        f"{st} count={seen.get('count')} got={str(got)[:50]}")
    chk("C6 压缩时喂进去的是旧消息 + 已有摘要（不是空手要）",
        SUM_CANARY in json.dumps(seen.get("body"), ensure_ascii=False),
        "")
    upto = q1(db, "SELECT summary_upto FROM sessions WHERE id=?", (SID,))
    chk("C7 `summary_upto` = 最后一条被压消息的 id",
        int(upto or 0) > 0 and int(upto) == num(out, "upto_id"),
        f"upto={upto} out={out.get('upto_id')}")

    ids_after = [r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT id FROM messages ORDER BY id").fetchall()]
    chk("C8 🔴 **原文一条不删**（行数与 id 集合都不变）",
        ids_after == ids_before and len(ids_after) == n_before,
        f"{n_before} → {len(ids_after)}")
    chk("C9 压缩过程没动 messages 的正文",
        q1(db, "SELECT text FROM messages WHERE id=?", (ids_before[0],)) == "今天想聊聊盐系手帐风", "")

    # ⑩ 增量：第二遍不再喂已压过的
    reset_seen()
    st, out = req(CTX_SUM, method="POST", token=SECRET,
                  body={"session_id": SID, "force": True, "keep": 30})
    blob = json.dumps(seen.get("body"), ensure_ascii=False)
    chk("C10 🔴 第二次是增量：已压过的旧消息不再喂（foldable=0）",
        st == 200 and out.get("reason") == "nothing_new_to_fold"
        and num(out, "foldable_rows") == 0 and out.get("rebuild") is False,
        f"{out.get('reason')} fold={out.get('foldable_rows')} "
        f"prev_upto={out.get('prev_upto')} rebuild={out.get('rebuild')!r}")

    # ⑫ rebuild 的 dry：忽略 summary_upto、也忽略老摘要
    st, r_dry = req(CTX_SUM, method="POST", token=SECRET,
                    body={"session_id": SID, "force": True, "keep": 30, "dry": True,
                          "rebuild": True})
    # ⚠️ 条数不写死：口径是「重建时 foldable = 热区外的**全部**行」，
    #    这与 §4 的"没到阈值"、§10 的"增量 0 条"形成对照。
    chk("C12 🔴 `rebuild` 的 dry：忽略 summary_upto、忽略老摘要"
        "（foldable = 热区外全部，而增量时是 0）",
        st == 200 and r_dry.get("rebuild") is True
        and num(r_dry, "foldable_rows") == num(r_dry, "rows") - num(r_dry, "hot_rows")
        and num(r_dry, "foldable_rows") > 0
        and num(r_dry, "prev_summary_chars") == 0,
        f"fold={r_dry.get('foldable_rows')} rows={r_dry.get('rows')} "
        f"hot={r_dry.get('hot_rows')} prev_chars={r_dry.get('prev_summary_chars')}")

    # ⑬ rebuild 真跑：老摘要**不进材料**，老消息**重新进来**，摘要被覆盖
    reset_seen()
    st, r_new = req(CTX_SUM, method="POST", token=SECRET,
                    body={"session_id": SID, "force": True, "keep": 30, "rebuild": True})
    blob2 = json.dumps(seen.get("body"), ensure_ascii=False)
    chk("C13 🔴 `rebuild` 重压：老摘要**不被喂回去**、老消息重新进来、摘要被覆盖",
        st == 200 and seen.get("count") == 1
        and SUM_CANARY not in blob2 and "今天想聊聊盐系手帐风" in blob2
        and q1(db, "SELECT summary FROM sessions WHERE id=?", (SID,)) == MOCK_REPLY,
        f"{st} cnt={seen.get('count')} old_in_material={SUM_CANARY in blob2}")

    ids_after2 = [r[0] for r in sqlite3.connect(str(db)).execute(
        "SELECT id FROM messages ORDER BY id").fetchall()]
    chk("C14 🔴 `rebuild` 之后原文仍然一条没删（覆盖写最容易被人怀疑丢东西）",
        ids_after2 == ids_before and len(ids_after2) == n_before
        and q1(db, "SELECT text FROM messages WHERE id=?", (ids_before[0],))
        == "今天想聊聊盐系手帐风",
        f"{n_before} → {len(ids_after2)}")

    # ⑪ 端到端：压完 → 再注入 → 上游 system 里是新摘要
    b = base_body(PROBE)
    reset_seen()
    req(LLM_URL, method="POST", token=SECRET, body=b)
    chk("C11 端到端：压缩后注入的是**新**摘要",
        MOCK_CANARY in upstream_system() and SUM_CANARY not in upstream_system(),
        upstream_system()[:100])
    del blob, out, st


# ══════════════════════════════════════════════════════════════════════════
# D 组：接线 / 迁移 / 红线 / 开关
# ══════════════════════════════════════════════════════════════════════════

def part_d(db: Path, log_path: Path, off_port=PORT_OFF) -> None:
    src = (DEPLOY / "app_ext" / "context.py").read_text(encoding="utf-8")
    init_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")

    log = log_path.read_text(encoding="utf-8", errors="replace")
    chk("D1 register 摘要里报了上下文层",
        "上下文管理就绪" in log, log[-200:])
    chk("D2 `_ROUTES` 两条都在",
        "/app/ext/context/summarize" in init_src and "/app/ext/context/status" in init_src
        and "context.install" in init_src, "")
    chk("D3 🔴 **不是房间**：context.py 不 import mcp",
        not re.search(r"^\s*(from|import)\s+.*\bmcp\b", src, re.M), "")
    chk("D4 🔴 原文不删：context.py 里没有对 messages 的写语句",
        not re.search(r"(UPDATE|DELETE\s+FROM|INSERT\s+INTO)\s+messages", src, re.I), "")
    ver = q1(db, "PRAGMA user_version")
    cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(sessions)")]
    chk("D5 🔴 v2 老库起服务后自动补 `summary_upto` 且 user_version=3",
        int(ver or 0) == 3 and "summary_upto" in cols, f"ver={ver} cols={cols}")
    scols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(settings)")]
    chk("D6 🔴 上一版的 `settings.provider_id` 迁移仍在",
        "provider_id" in scols, str(scols))

    chk("D7 逃生开关在（`APP_EXT_CONTEXT_DISABLED`）",
        "APP_EXT_CONTEXT_DISABLED" in init_src, "")
    va = (HERE / "verify_all.py")
    chk("D8 verify_all 里接了本套",
        va.exists() and "context_check" in va.read_text(encoding="utf-8"), "")

    # ⑨ 关掉开关的房子：注入不发生、端点 404
    reset_seen()
    b = base_body(PROBE)
    st, _ = req(f"http://127.0.0.1:{off_port}{PREFIX}/app/ext/llm/v1/chat/completions",
                method="POST", token=SECRET, body=b)
    st2, _ = req(f"http://127.0.0.1:{off_port}{PREFIX}/app/ext/context/status", token=SECRET)
    chk("D9 🔴 关掉开关：说话照常、但**不注入**、上下文端点 404",
        st == 200 and SUM_CANARY not in upstream_system() and st2 == 404,
        f"llm={st} ctx={st2}")


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kael-ctx-"))
    home = house_dir(tmp)
    home.mkdir(parents=True, exist_ok=True)
    (home / "uploads").mkdir(parents=True, exist_ok=True)
    (home / "workshop").mkdir(parents=True, exist_ok=True)
    db = db_path_of(home)
    seed_db(db)

    srv = start_mock()
    proc = logf = None
    proc_off = logf_off = None
    try:
        proc, logf, log_path = start_house(home, PORT_OK)
        if not wait_port(PORT_OK):
            print("[FAIL] 房子没起来；最后日志：")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1

        part_a()
        part_b(db)
        part_c(db)

        # 🔴 先把第一间房子停掉，再起"关了开关"的那间 —— 两个进程同时开一个
        #    SQLite 会抢写锁（`register()` 每次启动都要写迁移/投影），
        #    那种红是环境噪声，不是被测对象的问题。停干净再起，红就一定是真的。
        stop_house(proc, logf)
        proc = logf = None

        proc_off, logf_off, _ = start_house(home, PORT_OFF, context_disabled=True)
        wait_port(PORT_OFF)
        part_d(db, log_path)
    finally:
        stop_house(proc, logf)
        stop_house(proc_off, logf_off)
        try:
            srv.shutdown()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    lines = [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   {d}" if (d and not ok) else "")
             for name, ok, d in results]
    report = "\n".join(lines) + f"\n\n共 {len(results)} 项，通过 {passed}，失败 {failed}\n"
    (HERE / "context_report.txt").write_text(report, encoding="utf-8")
    print(report)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
