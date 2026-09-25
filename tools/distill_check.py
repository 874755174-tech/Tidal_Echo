#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
蒸馏管道验收 —— P2 ⑩-b（从对话抽出记忆 + 水位线 + 软作废）
==========================================================================

## 这一套到底在守什么

⑩-b 是**唯一一层会真的调 LLM 花钱**、也是**唯一一层会把模型的话写进库**的地方。
所以它的病不是"功能不对"这种性格，而是：

  · **编造来源**：模型自己造一个 `source_msg`，代码照单全收
    ⇒ 库里出现一批"看起来能回溯、其实指向无关消息"的记忆。
    **这比没有来源更坏** —— 它让人以为查得清。这一套有一整节在钉它。
  · **静默失败当成功**：解析不出来也把水位线推过去 ⇒ 那一段**永远没人蒸**，
    而且看起来一切正常。
  · **反过来也坏**：解析成功但抽出来是空的，水位线**不动** ⇒ 那一小段
    永远卡在队头，每次跑都白花一次钱。**这两个结局必须分得开。**
  · **重跑靠删**：⑩-a 定死"没有 delete"。重跑如果走 `DELETE` 就把那条作废了。
  · **重跑把"废掉的"又算进去**：`mark_superseded` 若不带
    `superseded_by IS NULL`，第二次重跑会改写批号 ⇒ "最早是谁废的"这个事实消失。
  · **被标废的记忆还在喂模型**：`top()` 要是不排除它们，"重跑"等于没重跑。
  · **redo 越界到别的会话**：`memories` 表**没有 session 列**，
    记忆与会话的联系只有 `source_msg` 这一条指针 ⇒ 不 JOIN `messages`
    就会把别的会话从更早的 id 开始重蒸一遍。这条有专门一节在钉。
  · **花钱不留痕**：⑩-b 每次调用都该进账本（`route=distill`）——
    不写 = 缺口不可见 = 账本说谎。
  · **偷偷有了后台循环**：一旦有定时器，"跑不跑由人说了算"这句话就作废了。

## 手法：**假上游可编程** + 房子走进程内 ASGI

跟 `context_check.py` 一样，起一个本地假上游当"眼睛"（记下每次收到的 body），
但这一套多一样东西：**回什么由测试决定**（好 JSON / 坏 JSON / 编造的 id / 空数组）。
因为这一层要验的恰恰是"模型说了不该说的话时会发生什么"。

房子本身**不起端口**（`TestClient` 进进程），所以不跟别的套抢端口。
假上游占 **8830**（本套自用）。

## 覆盖清单

  A. 纯逻辑（不读库、不发网络）
     1-7    build_material：人称 `out→我` / `in→你`（与 ⑧ 一致）；
            used_ids **只含真拼进去的 id**；超上限就截断（宁少喂不超预算）
     8-24   parse_items 的宽容边界：围栏 / 前后有解释 / 裸数组 / `memories` 键 /
            空数组（**不是失败**）/ 空输出 / 非 JSON / items 不是数组
            🔴 宽容**有边界**：半截话不抢救
     25-38  validate_items：编造 id 丢、缺 id 丢、空 text 丢、
            超长 text **丢而不截断**、非法 kind 归 fact、批内去重（不掺 kind）、
            超上限丢弃并计数、字符串数字宽容、非 dict 元素丢弃
  B. 迁移 v5 → v6（**真造一个 v5 老库来升**）
     1-4    起点正确（是 v5、确实没有那两列）
     5-12   升到 6；两列被补上；老行一字未改；`superseded_by` 是 NULL；
            `distill_upto` 是 0；`summary` 没被碰；`messages` DDL/行数未变
     13-18  🔴 两列的 DEFAULT 各自正确；幂等；新老两库列集合一致、顺序不同
  C. store 层（`memories_store`）
     1-5    add_many 正常写 / 形状 / 落库能读回
     6-11   🔴 幂等（同批/批内/跨来源）；空 text 与超长 text 不计入；None 号允许
     12-21  🔴 软作废：标了 N 条 / **一行没少** / 默认读不到 / `top()` 也排除 /
            只动指定 source / 幂等且批号不被改写 / 区间限定 / **只动那一列**
     22-25  stats 自洽；`public()` 带 superseded_by、不带 salience；喂模型只留 kind/text
     26-28  🔴 源码里没有 `DELETE FROM` / `DELETE` 语句 / `def *delete*()`
  D. 端点 + 真上游（假上游可编程）—— **这一套的主场**
     1-6    鉴权 401；status 两种形态；坏 JSON 400；缺 session_id 400；会话不存在 404
     7-14   🔴 `dry=true` **一次上游都不碰**、库一行不写，但给出区间与材料长度；
            真跑：抽出条目 / 每条 source_msg 都在区间内 / source=chat / 水位线前进
     15-18  🔴 **编造的 source_msg 被丢**（不入库 + `dropped_bad_source` 计数）
     19-20  🔴 没有新内容 → `nothing_new` 且**一次上游都不碰**
     21-24  🔴 坏 JSON：重试一次能救回（`retried=true`）＋ **两次调用都记账**
     25-28  🔴 两次都坏 → `parse_failed` ＋ **水位线一格不动** ＋ 库没写
            ⇒ 修好上游后**同一段能重跑成功**（这就是水位线不动的价值）
     29-31  🔴 空数组 = **成功**：水位线照推、`note=extracted_nothing`
     32-36  🔴 redo：旧批被标废 / 行还在 / 默认读不到 / 批号是本次 run_id
     37-38  🔴 redo 跑失败时**旧的还在**（先拿到新结果才废旧的）
     39-40  🔴 redo 只影响**本会话**（另一会话更早的记忆不被卷进来）
     41-44  `nothing_to_redo` / `too_few_rows` / `min_rows=1` 能强跑 / 没有定时器
  E. 红线 / 接线 / 开关
     1-3    两条路由在 `_ROUTES`；`register()` 里有第 ⑩-b 步 + 开关
     4-5    🔴 `distill.py` 不 import mcp、不注册 MCP 工具（它不是房间）
     6-8    schema 版本 = 当前 `SCHEMA_VERSION`；`summary_line()` GBK 安全；
            🔴 进程里没有定时器（源码扫描）
     9-10   🔴 关掉开关：端点 404，**表 / 两列 / 水位线一样都不少**
     11-12  🔴 ⑧ 摘要那一路也记账了（`route="summary"`）—— 它以前漏了
     13     `verify_all` 已接本套

用法：.venv\Scripts\python.exe tools\distill_check.py
（跑完把**全项**报告落到 tools/distill_report.txt，与其余各套同形）
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 🔴 本机 PowerShell 5.1 管道下 sys.stdout.encoding = cp936(gbk)，而验收名里有
#    🔴/✅/⚠️ 这类 GBK 编不出的字符 -> print 到一半 UnicodeEncodeError，整套会
#    **半路死掉**（看起来像"验收挂了"，其实是没跑完）。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(DEPLOY))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
warnings.filterwarnings("ignore")

SECRET = "test-secret-distill-0123456789"
PREFIX = "/relay"
MOCK_PORT = 8830            # 假上游（本套自用，不与别套抢）
MODEL = "claude-opus-4-6"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sect(title: str) -> None:
    print("")
    print("-" * 66)
    print(title)
    print("-" * 66)


# ══════════════════════════════════════════════════════════════════════════
# 假上游：**回什么由测试决定**（这一套的核心手法）
# ══════════════════════════════════════════════════════════════════════════

seen: dict = {"calls": []}
#: 下一条回复。可以是 str（原样当 text）或 dict（会被 json.dumps）。
#: 给多条 = 按顺序吐；只剩一条时会**重复吐它**（方便测"两次都坏"）。
REPLIES: list = []
USAGE = {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133}


def set_replies(*replies) -> None:
    """设置回复序列，并**清空调用记录** —— 每段断言只关心自己那几次调用。"""
    REPLIES[:] = list(replies)
    seen["calls"].clear()


def next_reply() -> str:
    if not REPLIES:
        return json.dumps({"items": []}, ensure_ascii=False)
    r = REPLIES.pop(0) if len(REPLIES) > 1 else REPLIES[0]
    return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def items_payload(*items) -> str:
    return json.dumps({"items": list(items)}, ensure_ascii=False)


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        text = next_reply()
        msgs = body.get("messages") or []
        seen["calls"].append({
            "body": body,
            "text": text,
            "system": (msgs[0].get("content") if msgs else ""),
            "user": (msgs[1].get("content") if len(msgs) > 1 else ""),
        })
        if self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
                "model": body.get("model") or "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": dict(USAGE),
            })
        return self._send(404, {"error": {"message": f"mock 不认：{self.path}"}})


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def upstream_calls() -> list:
    return list(seen["calls"])


# ══════════════════════════════════════════════════════════════════════════
# 房子（进程内，不起端口）
# ══════════════════════════════════════════════════════════════════════════

MESSAGES_DDL = """
    CREATE TABLE IF NOT EXISTS messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT NOT NULL,
        direction TEXT NOT NULL,
        kind      TEXT NOT NULL,
        text      TEXT NOT NULL,
        meta      TEXT NOT NULL DEFAULT '{}'
    )
"""


class FakeRelay:
    def __init__(self, db_path: str):
        from fastapi import FastAPI
        self.DB_PATH = db_path
        self.SECRET = SECRET
        self.app = FastAPI()

    def check_auth(self, request):
        from fastapi import HTTPException
        import hmac
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        if not tok:
            tok = request.query_params.get("token", "")
        if not tok or not hmac.compare_digest(tok, SECRET):
            raise HTTPException(status_code=401, detail="unauthorized")


def _setup_env(home: Path, *, disabled: bool = False) -> None:
    os.environ.update({
        "RELAY_DB": str(home / "relay.db"),
        "RELAY_SECRET": SECRET,
        "RELAY_HUMAN_NAME": "Lily",
        "RELAY_BACKEND_DIR": str(REPO / "backend"),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "RELAY_UPLOAD_DIR": str(home / "uploads"),
        "RELAY_WORKSHOP_DIR": str(home / "workshop"),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(REPO / "backend"),
        # 只留中转站，指到假上游 —— 不碰真网络、不花一分钱
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini",
        "PROVIDER_RELAY_KEY": "sk-mock-distill",
        "PROVIDER_RELAY_BASE": f"http://127.0.0.1:{MOCK_PORT}/v1",
        "PROVIDER_RELAY_MODELS": MODEL,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    os.environ.pop("APP_EXT_DISTILL_DISABLED", None)
    if disabled:
        os.environ["APP_EXT_DISTILL_DISABLED"] = "1"


def seed_messages(db: str, session_id: str, pairs) -> list:
    """塞消息。`pairs` = [(direction, text), …]（in=她说 / out=他说）。

    返回插入的 **id 列表（升序）** —— 后面每一处"区间"断言都用它，不靠数数。
    """
    conn = sqlite3.connect(db)
    ids = []
    for direction, text in pairs:
        kind = "user" if direction == "in" else "reply"
        cur = conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
            ("2026-09-25T00:00:00+00:00", direction, kind, text,
             json.dumps({"api_session": session_id}, ensure_ascii=False)),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    conn.close()
    return ids


def add_session(db: str, session_id: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, user_id, title, created, updated) "
        "VALUES (?,?,?,?,?)",
        (session_id, "u_owner", "测试会话", "2026-09-25T00:00:00+00:00",
         "2026-09-25T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _reset_installs() -> None:
    """把各层的 `_INSTALLED` 幂等标记复位。

    🔴 **同一进程里要起第二个房子时必须做这件事。** `install()` 的幂等标记是
       **模块级**的（`if _INSTALLED: return`），第二个 FastAPI app 会被它们
       挡在门外 —— 于是 E9 那条"关掉开关 → 端点 404"会在一个
       **根本没装过任何路由**的 app 上通过：看着是绿的，其实什么都没验到。
       （这是"测试自己骗自己"里最难发现的一类：断言为真，但它测的不是那件事。）
    """
    import importlib
    for name in ("memory", "distill", "activity", "usage", "archive",
                 "generate", "mcp", "identity", "context"):
        try:
            m = importlib.import_module(f"app_ext.{name}")
        except Exception:
            continue
        if hasattr(m, "_INSTALLED"):
            m._INSTALLED = False


def make_house(home: Path, *, disabled: bool = False):
    """全新房子（**真走 `register()`**）。返回 `(relay, client, register摘要)`。"""
    home.mkdir(parents=True, exist_ok=True)
    _reset_installs()
    _setup_env(home, disabled=disabled)
    from fastapi.testclient import TestClient
    import app_ext

    relay = FakeRelay(str(home / "relay.db"))
    # `messages` 表在真环境里由 backend 建；替身自带一份（列与 backend 一致）
    conn = sqlite3.connect(relay.DB_PATH)
    conn.execute(MESSAGES_DDL)
    conn.commit()
    conn.close()

    out = app_ext.register(relay, public_prefix=PREFIX)
    return relay, TestClient(relay.app), out


def H() -> dict:
    return {"Authorization": f"Bearer {SECRET}"}


def rows_of(db: str) -> list:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = [dict(x) for x in conn.execute(
        "SELECT id,kind,text,source,source_msg,superseded_by FROM memories "
        "ORDER BY rowid ASC").fetchall()]
    conn.close()
    return r


def alive(db: str) -> list:
    return [r for r in rows_of(db) if r["superseded_by"] is None]


def watermark(db: str, session_id: str) -> int:
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT distill_upto FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    conn.close()
    return int((row[0] if row else 0) or 0)


def usage_routes(db: str) -> list:
    conn = sqlite3.connect(db)
    r = [x[0] for x in conn.execute(
        "SELECT COALESCE(route,'') FROM usage_log ORDER BY id ASC").fetchall()]
    conn.close()
    return r


# ══════════════════════════════════════════════════════════════════════════
# A. 纯逻辑
# ══════════════════════════════════════════════════════════════════════════

def part_a():
    from app_ext import distill as D

    sect("A. 纯逻辑：材料 / 解析 / 校验（不读库、不发网络）")

    rows = [{"id": 11, "direction": "in", "text": "我今天做了个清单"},
            {"id": 12, "direction": "out", "text": "好，我把配色也调一版"},
            {"id": 13, "direction": "in", "text": "   "}]     # 空正文
    material, ids = D.build_material(rows)
    chk("A1  build_material 拼出材料", "[11]" in material and "[12]" in material,
        material[:60])
    chk("A2  🔴 人称与 ⑧ 一致：in →「你」", "[11] 你：" in material, material)
    chk("A3  🔴 人称与 ⑧ 一致：out →「我」", "[12] 我：" in material, material)
    chk("A4  🔴 used_ids 只含真拼进去的 id（空正文的 13 不在）",
        ids == [11, 12], str(ids))

    saved = D.MAX_MATERIAL_CHARS
    try:
        D.MAX_MATERIAL_CHARS = 30
        _m, ids2 = D.build_material(rows)
        chk("A5  材料超上限就截断（不再往后拼）", len(ids2) < 2, str(ids2))
        D.MAX_MATERIAL_CHARS = 0
        m3, ids3 = D.build_material(rows)
        chk("A6  🔴 截断后 used_ids 跟着少（不会出现'引用了没喂的 id'）",
            m3 == "" and ids3 == [], f"{m3!r} / {ids3!r}")
        chk("A7  上限为 0 时不 panic、返回空", D.build_material([]) == ("", []), "")
    finally:
        D.MAX_MATERIAL_CHARS = saved

    chk("A8  parse_items：纯 JSON",
        D.parse_items('{"items":[{"kind":"fact","text":"x","source_msg":3}]}')[1] is None)
    item = D.parse_items('{"items":[{"kind":"fact","text":"x","source_msg":3}]}')[0]
    chk("A9  解析出的条数与内容对", len(item) == 1 and item[0]["text"] == "x", str(item))
    chk("A10 剥 ```json 围栏",
        D.parse_items('```json\n{"items":[{"kind":"fact","text":"x","source_msg":3}]}\n```')[1] is None)
    chk("A11 剥无语言名的围栏",
        D.parse_items('```\n{"items":[]}\n```')[1] is None)
    chk("A12 前后有解释文字也认",
        D.parse_items('好的，结果如下：\n{"items":[{"kind":"fact","text":"x","source_msg":3}]}\n希望有用')[1] is None)
    got13 = D.parse_items('[{"kind":"fact","text":"x","source_msg":3}]')[0]
    chk("A13 顶层直接是数组也认", got13 and len(got13) == 1, str(got13))
    got14 = D.parse_items('{"memories":[{"kind":"fact","text":"x","source_msg":3}]}')[0]
    chk("A14 `{\"memories\": [...]}` 也认", got14 and len(got14) == 1, str(got14))
    chk("A15 🔴 空数组 = 成功（err 为 None），不是失败",
        D.parse_items('{"items":[]}') == ([], None), str(D.parse_items('{"items":[]}')))
    chk("A16 空输出 → empty_output",
        D.parse_items("")[1] == "empty_output", str(D.parse_items("")[1]))
    chk("A17 纯散文 → not_json",
        D.parse_items("我抽不出来")[1] == "not_json", str(D.parse_items("我抽不出来")[1]))
    chk("A18 `items` 不是数组 → items_not_list",
        D.parse_items('{"items":"nope"}')[1] == "items_not_list",
        str(D.parse_items('{"items":"nope"}')[1]))
    chk("A19 有 items 键但值为 null → no_items_key",
        D.parse_items('{"items":null}')[1] in ("no_items_key", "items_not_list"),
        str(D.parse_items('{"items":null}')[1]))
    chk("A20 顶层数组前后有杂字也能认",
        D.parse_items('前言 [{"kind":"fact","text":"x","source_msg":3}] 后记')[1] is None)
    chk("A21 正文里的转义引号不会把解析搞崩",
        D.parse_items('{"items":[{"kind":"fact","text":"他说\\"好\\"","source_msg":3}]}')[1] is None)
    chk("A22 🔴 宽容**有边界**：半截话不抢救（`{` 没闭合 → not_json）",
        D.parse_items('{"items":[{"kind":"fact","text":"x"')[1] == "not_json",
        str(D.parse_items('{"items":[{"kind":"fact","text":"x"')[1]))
    chk("A23 🔴 同样不抢救：只有裸引号碎片",
        D.parse_items('里面应该有 "kind":"fact" 这样的东西')[1] == "not_json",
        str(D.parse_items('里面应该有 "kind":"fact" 这样的东西')[1]))
    chk("A24 `items` 是空字符串 → items_not_list",
        D.parse_items('{"items":""}')[1] == "items_not_list",
        str(D.parse_items('{"items":""}')[1]))

    v = D.validate_items([{"kind": "fact", "text": "她喜欢低饱和的颜色", "source_msg": 11},
                          {"kind": "preference", "text": "她讨厌繁复的欧式风格",
                           "source_msg": 12}], {11, 12, 13})
    chk("A25 全合法 → 全收", len(v["good"]) == 2 and not v["dropped_bad_source"], str(v))
    v = D.validate_items([{"kind": "fact", "text": "编的", "source_msg": 999}], {11, 12})
    chk("A26 🔴 编造的 source_msg 被丢 + 计数", len(v["good"]) == 0
        and v["dropped_bad_source"] == 1, str(v))
    v = D.validate_items([{"kind": "fact", "text": "没号"}], {11, 12})
    chk("A27 🔴 缺 source_msg 也算 bad_source（不兜底、不猜）",
        len(v["good"]) == 0 and v["dropped_bad_source"] == 1, str(v))
    v = D.validate_items([{"kind": "fact", "text": "号不是数", "source_msg": "abc"}], {11})
    chk("A28 source_msg 不是整数 → bad_source", v["dropped_bad_source"] == 1, str(v))
    v = D.validate_items([{"kind": "fact", "text": "", "source_msg": 11}], {11})
    chk("A29 空 text → bad_shape（不写空记忆）",
        len(v["good"]) == 0 and v["dropped_bad_shape"] == 1, str(v))
    v = D.validate_items([{"kind": "fact", "text": "很" * 50, "source_msg": 11}],
                         {11}, text_max=20)
    chk("A30 🔴 超长 text **丢**而不是截断收纳（截歪的记忆会长期待在库里）",
        len(v["good"]) == 0 and v["dropped_bad_shape"] == 1, str(v))
    v = D.validate_items([{"kind": "nonsense", "text": "归 fact", "source_msg": 11}], {11})
    chk("A31 非法 kind → 归 fact（与 ⑩-a 的 add() 同一条规矩）",
        len(v["good"]) == 1 and v["good"][0]["kind"] == "fact", str(v))
    v = D.validate_items([{"kind": "fact", "text": "同一句", "source_msg": 11},
                          {"kind": "fact", "text": "同一句", "source_msg": 11}], {11})
    chk("A32 批内重复只留一条", len(v["good"]) == 1, str(v))
    v = D.validate_items([{"kind": "fact", "text": "同一句", "source_msg": 11},
                          {"kind": "preference", "text": "同一句", "source_msg": 11}], {11})
    chk("A33 🔴 去重键不掺 kind（改归类不该让它多长一条）", len(v["good"]) == 1, str(v))
    many = [{"kind": "fact", "text": f"第{i}条", "source_msg": 11} for i in range(6)]
    v = D.validate_items(many, {11}, max_items=3)
    chk("A34 超上限的丢弃并**计数**（不静默）",
        len(v["good"]) == 3 and v["dropped_over_cap"] == 3,
        str({k: (len(x) if k == "good" else x) for k, x in v.items()}))
    v = D.validate_items([{"kind": "fact", "text": "x", "source_msg": "11"}], {11})
    chk("A35 字符串 '11' 能转成整数（宽容这一侧，不宽容编造那一侧）",
        len(v["good"]) == 1 and v["good"][0]["source_msg"] == 11, str(v))
    v = D.validate_items([None, 3, "x"], {11})
    chk("A36 非 dict 元素 → bad_shape", v["dropped_bad_shape"] == 3, str(v))
    v = D.validate_items([{"kind": "fact", "text": " x ", "source_msg": 11}], {11})
    chk("A37 text 会被 strip（不留首尾空白）",
        v["good"][0]["text"] == "x", repr(v["good"][0]["text"]))
    v = D.validate_items([{"kind": "fact", "text": "x", "source_msg": True}], {7})
    chk("A38 🔴 布尔 True 会变成 1 —— 1 不在区间里时必须被丢",
        len(v["good"]) == 0 and v["dropped_bad_source"] == 1, str(v))


# ══════════════════════════════════════════════════════════════════════════
# B. 迁移 v5 → v6
# ══════════════════════════════════════════════════════════════════════════

def make_old_v5_db(tmp: Path) -> str:
    """造一个**真的 v5 老库**：没有 `distill_upto` / `superseded_by`。

    做法：用当前 DDL 建库 → 把那两列 `DROP COLUMN` 掉 → 版本写回 5。
    其余部分与真实老库逐字一致（`source` 在、`summary_upto` 在）。
    """
    from app_ext import schema as S
    db = str(tmp / "old_v5.db")
    relay = FakeRelay(db)
    conn = sqlite3.connect(db)
    conn.execute(MESSAGES_DDL)
    conn.commit()
    conn.close()
    S.ensure_schema(relay)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE sessions DROP COLUMN distill_upto")
    conn.execute("ALTER TABLE memories DROP COLUMN superseded_by")
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO users (id,handle,display_name,secret_hash,role,created) "
                 "VALUES ('u_owner','owner','Lily','x','owner','2026-09-01T00:00:00+00:00')")
    conn.execute("INSERT INTO sessions (id,user_id,summary,summary_upto,created,updated) "
                 "VALUES ('s_old','u_owner','老的摘要',7,'t','t')")
    conn.execute("INSERT INTO memories "
                 "(id,user_id,kind,text,source_msg,source,salience,created) "
                 "VALUES ('m_old','u_owner','fact','老的记忆',3,'chat',0.5,'t')")
    conn.execute("INSERT INTO messages (ts,direction,kind,text,meta) "
                 "VALUES ('t','in','user','老的消息','{\"api_session\":\"s_old\"}')")
    conn.commit()
    conn.close()
    return db


def part_b(tmp: Path):
    from app_ext import schema as S
    sect("B. 迁移 v5 → v6（真造一个 v5 老库来升）")

    db = make_old_v5_db(tmp)
    conn = sqlite3.connect(db)
    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    scols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    sig_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
    n_before = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    conn.close()

    chk("B1  起点确实是一个 v5 老库", ver == 5, f"ver={ver}")
    chk("B2  sessions 确实没有 distill_upto", "distill_upto" not in scols,
        str(sorted(scols)))
    chk("B3  memories 确实没有 superseded_by", "superseded_by" not in mcols,
        str(sorted(mcols)))
    chk("B4  （v5 的 source 缝在，起点对）", "source" in mcols, str(sorted(mcols)))

    relay = FakeRelay(db)
    out = S.ensure_schema(relay)
    chk("B5  升到 v6", out["version"] == 6, str(out["version"]))
    chk("B6  migrated 点名补了这两列",
        set(out["migrated"]) == {"sessions.distill_upto", "memories.superseded_by"},
        str(out["migrated"]))
    chk("B7  🔴 messages DDL 逐字未变 + 行数未变（红线断言真跑了）",
        out["messages_untouched"] and out["messages_rows"] == n_before,
        f"{out['messages_rows']} vs {n_before}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM memories WHERE id='m_old'").fetchone())
    srow = dict(conn.execute("SELECT * FROM sessions WHERE id='s_old'").fetchone())
    sig_after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
    dflt_s = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(sessions)")}
    dflt_m = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(memories)")}
    conn.close()

    chk("B8  老记忆正文一个字没改 + source 还在",
        row["text"] == "老的记忆" and row["source"] == "chat", str(row))
    chk("B9  🔴 老行的 superseded_by 是 NULL（不是 ''、不是批次号）",
        row["superseded_by"] is None, repr(row["superseded_by"]))
    chk("B10 老会话的 distill_upto 是 0（= 没蒸过）",
        int(srow["distill_upto"] or 0) == 0, repr(srow["distill_upto"]))
    chk("B11 老会话的 summary / summary_upto 没被碰",
        srow["summary"] == "老的摘要" and int(srow["summary_upto"]) == 7, str(srow))
    chk("B12 messages DDL 逐字节一致", sig_before == sig_after, "")
    chk("B13 🔴 `superseded_by` 上**没有 DEFAULT**（否则老行会报出假值）",
        dflt_m.get("superseded_by") is None, repr(dflt_m.get("superseded_by")))
    chk("B14 `distill_upto` 的 DEFAULT 是 0（有意与 summary_upto 同形）",
        str(dflt_s.get("distill_upto")) == "0", repr(dflt_s.get("distill_upto")))

    out2 = S.ensure_schema(relay)
    chk("B15 幂等：再跑一次 migrated 为空、版本不动",
        out2["migrated"] == [] and out2["version"] == 6, str(out2["migrated"]))

    fresh = str(tmp / "fresh_v6.db")
    S.ensure_schema(FakeRelay(fresh))
    conn = sqlite3.connect(fresh)
    f_m = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    f_s = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    dflt_m2 = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(memories)")}
    conn.close()
    conn = sqlite3.connect(db)
    o_m = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    o_s = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    conn.close()
    chk("B16 新老两库 memories 列集合一致", set(f_m) == set(o_m),
        f"{sorted(f_m)} vs {sorted(o_m)}")
    chk("B17 新老两库 sessions 列集合一致", set(f_s) == set(o_s),
        f"{sorted(f_s)} vs {sorted(o_s)}")
    chk("B18 新库列顺序与老库不同（ALTER 只追加）—— 所以上面那条才值得测",
        f_m != o_m, f"{f_m} vs {o_m}")
    chk("B19 🔴 新库那条列上也没有 DEFAULT（新老行为必须一致）",
        dflt_m2.get("superseded_by") is None, repr(dflt_m2.get("superseded_by")))


# ══════════════════════════════════════════════════════════════════════════
# C. store 层
# ══════════════════════════════════════════════════════════════════════════

def part_c(tmp: Path):
    from app_ext import schema as S, memories_store as M, identity as I
    sect("C. store 层：批量写 / 幂等 / 软作废 / 有效行过滤")

    db = str(tmp / "store.db")
    relay = FakeRelay(db)
    S.ensure_schema(relay)
    I.ensure_owner(relay)

    batch = [{"kind": "fact", "text": "她喜欢低饱和的颜色", "source_msg": 11},
             {"kind": "preference", "text": "她讨厌繁复的欧式风格", "source_msg": 12},
             {"kind": "nonsense", "text": "归类会被修正", "source_msg": 13}]
    w = M.add_many(relay, batch, source="chat")
    chk("C1  add_many 写入 3 条", w["added"] == 3, str(w["added"]))
    chk("C2  返回的 items 带 id/kind/text/source_msg",
        all({"id", "kind", "text", "source_msg"} <= set(x) for x in w["items"]),
        str(w["items"][:1]))
    got = rows_of(db)
    chk("C3  落库能读回、source=chat 且 superseded_by 是 NULL",
        len(got) == 3 and all(r["source"] == "chat" and r["superseded_by"] is None
                              for r in got), str(got[:1]))
    chk("C4  非法 kind 在库里是 fact",
        any(r["kind"] == "fact" and r["text"] == "归类会被修正" for r in got), str(got))
    chk("C5  source_msg 原样存（不重写）",
        sorted(r["source_msg"] for r in got) == [11, 12, 13],
        str(sorted(r["source_msg"] for r in got)))

    w2 = M.add_many(relay, batch, source="chat")
    chk("C6  🔴 幂等：同一批重跑 added=0",
        w2["added"] == 0 and w2["skipped_duplicate"] == 3, str(w2))
    chk("C7  库里还是 3 条（没长重复）", len(rows_of(db)) == 3, str(len(rows_of(db))))
    one = {"kind": "fact", "text": "她喜欢低饱和的颜色", "source_msg": 11}
    w3 = M.add_many(relay, [one, dict(one)], source="chat")
    chk("C8  批内重复只写一条 / 只计一次 duplicate",
        w3["added"] == 0 and w3["skipped_duplicate"] == 2, str(w3))
    w4 = M.add_many(relay, [dict(one)], source="reading")
    chk("C9  🔴 去重键含 source：换个来源就是另一条（书房不该被对话吃掉）",
        w4["added"] == 1, str(w4))
    w5 = M.add_many(relay, [{"kind": "fact", "text": "", "source_msg": 20},
                            {"kind": "fact", "text": "x" * 50, "source_msg": 21}],
                    source="chat", text_max=10)
    chk("C10 空 text / 超长 text 都不计入（bad）",
        w5["added"] == 0 and w5["skipped_invalid"] == 2, str(w5))
    w6 = M.add_many(relay, [{"kind": "fact", "text": "没号也能手动写",
                             "source_msg": None}], source="manual")
    chk("C11 `source_msg=None` 允许（手动写的记忆本来就没来源消息）",
        w6["added"] == 1, str(w6))

    mk = M.mark_superseded(relay, lo=10, hi=13, by="d_first", source="chat")
    chk("C12 mark_superseded 标了 3 条（chat 那三条）", mk["marked"] == 3, str(mk))
    chk("C13 🔴 库里**一条都没少**（3 chat + 1 reading + 1 manual = 5）",
        len(rows_of(db)) == 5, str(len(rows_of(db))))
    chk("C14 🔴 默认读不到被标废的",
        len(M.list_recent(relay)) == 2,
        str([r["text"] for r in M.list_recent(relay)]))
    chk("C15 `include_superseded=True` 才看得到全部",
        len(M.list_recent(relay, include_superseded=True)) == 5,
        str(len(M.list_recent(relay, include_superseded=True))))
    chk("C16 🔴 `top()` 默认也排除被标废的（**它喂模型**，漏了等于没重跑）",
        len(M.top(relay)) == 2, str(len(M.top(relay))))
    chk("C17 🔴 只动指定 source：reading / manual 那两条没被卷进来",
        all(r["superseded_by"] is None for r in rows_of(db)
            if r["source"] in ("reading", "manual")),
        str([(r["source"], r["superseded_by"]) for r in rows_of(db)]))
    mk2 = M.mark_superseded(relay, lo=10, hi=13, by="d_second", source="chat")
    chk("C18 🔴 幂等：第二次 marked=0，且**批号不被改写**",
        mk2["marked"] == 0 and all(r["superseded_by"] == "d_first"
                                   for r in rows_of(db) if r["source"] == "chat"),
        str(mk2))
    chk("C19 区间限定生效（12 那一段已被标废 → 0）",
        M.mark_superseded(relay, lo=12, hi=12, by="d_x", source="chat")["marked"] == 0,
        "")
    chk("C20 区间外的行不受影响（1..9 里没有 chat 记忆）",
        M.mark_superseded(relay, lo=1, hi=9, by="d_y", source="chat")["marked"] == 0, "")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
    conn.close()
    M.mark_superseded(relay, lo=1, hi=999, by="d_all", source=None)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
    conn.close()
    same = all(
        {k: v for k, v in before[i].items() if k != "superseded_by"}
        == {k: v for k, v in after[i].items() if k != "superseded_by"}
        for i in before)
    chk("C21 🔴 只动 superseded_by 一列（其余列逐字不变）", same, "")

    st = M.stats(relay)
    # C21 那次 `source=None` 的标废把 reading 也卷进来了；`manual` 那条没有 source_msg
    # ⇒ 天然标不到（`source_msg IS NOT NULL` 在 WHERE 里）→ alive 恰好剩它一条。
    chk("C22 stats 三个数分开报且自洽（total = alive + superseded，4 被废 / 1 有效）",
        st["total"] == st["alive"] + st["superseded"]
        and st["superseded"] == 4 and st["alive"] == 1,
        str({k: st[k] for k in ("total", "alive", "superseded")}))
    p = M.public(rows_of(db)[0])
    chk("C23 🔴 `public()` 带 superseded_by（审计指针）",
        "superseded_by" in p, str(sorted(p)))
    chk("C24 🔴 `public()` **不带 salience**（内部权重永不外泄）",
        "salience" not in p, str(sorted(p)))
    prompt_blob = json.dumps(M.as_extra_for_prompt(relay, include_superseded=False)
                             if False else M.as_extra_for_prompt(relay),
                             ensure_ascii=False)
    chk("C25 `as_extra_for_prompt` 只留 kind/text（连 source 都不给模型）",
        "source" not in prompt_blob and "salience" not in prompt_blob
        and "kind" in prompt_blob, prompt_blob[:120])

    src = (DEPLOY / "app_ext" / "memories_store.py").read_text(encoding="utf-8")
    dsr = (DEPLOY / "app_ext" / "distill.py").read_text(encoding="utf-8")
    chk("C26 🔴 两个文件里都没有 `DELETE FROM` / `DROP TABLE`（结构性无删除）",
        not re.search(r"\b(DELETE\s+FROM|DROP\s+TABLE)\b", src + dsr, re.I), "")
    chk("C27 🔴 没有 `def *delete*()` / `def *remove*()` 这类函数",
        not re.search(r"def\s+\w*(delete|remove|drop)\w*\s*\(", src + dsr, re.I), "")
    chk("C28 🟡 `ALTER TABLE` 只出现在注释里（真正改表只在 schema.py）",
        not re.search(r"^\s*[\"']ALTER\s+TABLE", src + dsr, re.I | re.M), "")


# ══════════════════════════════════════════════════════════════════════════
# D. 端点 + 真上游（假上游可编程）—— 主场
# ══════════════════════════════════════════════════════════════════════════

def part_d(tmp: Path):
    sect("D. 端点 + 真上游（假上游可编程）")

    home = tmp / "house_d"
    home.mkdir(parents=True, exist_ok=True)
    relay, c, _reg = make_house(home)
    db = relay.DB_PATH
    # 🔴 路由**不带 public_prefix**（前缀由外层挂载加，见 memory_check 同一手法）
    B = "/app/ext/distill"
    SID = "api-20260925-a"

    chk("D1  无密钥 → 401（GET 与 POST 都拦）",
        c.get(B + "/status").status_code == 401
        and c.post(B, json={"session_id": "x"}).status_code == 401, "")

    add_session(db, SID)
    ids = seed_messages(db, SID, [
        ("in", "我今天做了个眼馋清单"),
        ("out", "好，我把配色也调一版"),
        ("in", "你上次说的那个低饱和的墨蓝我很喜欢"),
        ("out", "记住了，以后都用那一路"),
    ])
    lo, hi = ids[0], ids[-1]

    st = c.get(B + "/status", headers=H()).json()
    chk("D2  status 不带 session_id → 列出会话（含水位与待蒸）",
        st.get("ok") and any(s["session_id"] == SID for s in st.get("sessions", [])),
        str(st.get("sessions"))[:160])
    st1 = c.get(B + "/status", headers=H(), params={"session_id": SID}).json()
    chk("D3  status 带 session_id → 该会话两条水位线分开报",
        st1.get("ok") and st1["session"]["distill_upto"] == 0
        and st1["session"]["summary_upto"] == 0
        and st1["session"]["pending_rows"] == 4, str(st1.get("session")))
    chk("D4  坏 JSON → 400",
        c.post(B, headers=H(), content=b"{not json").status_code == 400, "")
    chk("D5  缺 session_id → 400",
        c.post(B, headers=H(), json={}).status_code == 400, "")
    r404 = c.post(B, headers=H(), json={"session_id": "no-such"})
    chk("D6  会话不存在 → 404（不是 502）", r404.status_code == 404, str(r404.status_code))

    # ---- dry：一次上游都不碰 ----
    set_replies(items_payload({"kind": "fact", "text": "x", "source_msg": lo}))
    d = c.post(B, headers=H(), json={"session_id": SID, "dry": True}).json()
    chk("D7  🔴 dry=true **一次上游都不碰**", len(upstream_calls()) == 0,
        str(len(upstream_calls())))
    chk("D8  🔴 dry=true **一行都不写**（memories 还是 0）", len(rows_of(db)) == 0,
        str(len(rows_of(db))))
    chk("D9  dry 给出区间 / 行数 / 材料长度 / 原因",
        d.get("reason") == "dry_run" and d.get("range") == [lo, hi]
        and d.get("rows") == 4 and d.get("material_chars", 0) > 0, str(d))
    chk("D10 dry 不推水位线", watermark(db, SID) == 0, str(watermark(db, SID)))

    # ---- 第一次真跑 ----
    set_replies(items_payload(
        {"kind": "fact", "text": "她做了一个眼馋清单", "source_msg": lo},
        {"kind": "preference", "text": "她喜欢低饱和的墨蓝", "source_msg": ids[2]},
    ))
    r = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D11 真跑成功并抽出 2 条",
        r.get("ok") and r.get("extracted") == 2 and r.get("written") == 2,
        str({k: r.get(k) for k in ("ok", "extracted", "written", "reason")}))
    chk("D12 🔴 这次真的调了上游一次", len(upstream_calls()) == 1,
        str(len(upstream_calls())))
    chk("D13 🔴 每条抽出来的 source_msg 都落在本次区间内",
        sorted(x["source_msg"] for x in rows_of(db)) == sorted([lo, ids[2]]),
        str(sorted(x["source_msg"] for x in rows_of(db))))
    chk("D14 source 是 chat、superseded_by 是 NULL",
        all(x["source"] == "chat" and x["superseded_by"] is None for x in rows_of(db)),
        str(rows_of(db)[:1]))
    chk("D15 水位线推到最后一条消息（不是最后一个被引用的 id）",
        watermark(db, SID) == hi, f"{watermark(db, SID)} vs {hi}")
    chk("D16 🔴 记账：route=distill 的行在", "distill" in usage_routes(db),
        str(usage_routes(db)))
    chk("D17 返回里给了 run_id",
        str(r.get("run_id") or "").startswith("d_"), str(r.get("run_id")))

    # ---- 编造 source_msg ----
    ids2 = seed_messages(db, SID, [("in", "我最近在写一个故事"), ("out", "什么故事")])
    set_replies(items_payload(
        {"kind": "fact", "text": "编造来源的一条", "source_msg": 99999},
        {"kind": "event", "text": "她在写一个故事", "source_msg": ids2[0]},
    ))
    r2 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D18 🔴 编造的 source_msg 被丢 + 如实计数",
        r2.get("dropped_bad_source") == 1 and r2.get("written") == 1,
        str({k: r2.get(k) for k in ("dropped_bad_source", "written", "extracted")}))
    chk("D19 🔴 编造的那条**没有进库**",
        all("编造" not in x["text"] for x in rows_of(db)),
        str([x["text"] for x in rows_of(db)]))
    chk("D20 库里现在 3 条、水位线推到新的一条",
        len(rows_of(db)) == 3 and watermark(db, SID) == ids2[-1],
        f"{len(rows_of(db))} / {watermark(db, SID)}")

    # ---- 没有新内容：不碰上游 ----
    set_replies(items_payload({"kind": "fact", "text": "不该被写进来",
                               "source_msg": ids2[-1]}))
    r3 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D21 🔴 没有新内容 → nothing_new",
        r3.get("reason") == "nothing_new", str(r3.get("reason")))
    chk("D22 🔴 而且**一次上游都不碰**（省钱靠不调用）",
        len(upstream_calls()) == 0 and len(rows_of(db)) == 3,
        f"calls={len(upstream_calls())} rows={len(rows_of(db))}")

    # ---- 坏 JSON → 重试救回 ----
    ids3 = seed_messages(db, SID, [("in", "明天我们去看展吧"), ("out", "好，几点")])
    set_replies("这不是 JSON，我随便说点话",
                items_payload({"kind": "event", "text": "约了明天去看展",
                               "source_msg": ids3[0]}))
    r4 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D23 🔴 坏 JSON 重试一次能救回（retried=true）",
        r4.get("ok") and r4.get("retried") is True and r4.get("written") == 1,
        str({k: r4.get(k) for k in ("ok", "retried", "written", "reason")}))
    chk("D24 重试共调了上游 2 次", len(upstream_calls()) == 2,
        str(len(upstream_calls())))
    chk("D25 🔴 两次调用**都记账**（distill + distill_retry）",
        usage_routes(db).count("distill_retry") == 1
        and usage_routes(db).count("distill") >= 3, str(usage_routes(db)))
    chk("D26 重试用了同一份材料 + 补了一句纠错提示",
        upstream_calls()[1]["user"] != upstream_calls()[0]["user"]
        and "JSON" in upstream_calls()[1]["user"], "")

    # ---- 两次都坏：水位线不动 ----
    ids4 = seed_messages(db, SID, [("in", "我有点困"), ("out", "那早点睡")])
    hi_before = watermark(db, SID)
    rows_before = len(rows_of(db))
    set_replies("坏", "更坏")
    r5 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D27 🔴 两次都坏 → parse_failed（502，不是 200）",
        r5.get("reason") == "parse_failed" and r5.get("ok") is False,
        str(r5.get("reason")))
    chk("D28 🔴 **水位线一格不动**（因为这段真的没蒸成功）",
        watermark(db, SID) == hi_before and len(rows_of(db)) == rows_before,
        f"{watermark(db, SID)} vs {hi_before}")
    chk("D29 失败也留痕（累计调用数上去了）", len(upstream_calls()) >= 2,
        str(len(upstream_calls())))

    set_replies(items_payload({"kind": "event", "text": "她说有点困",
                               "source_msg": ids4[0]}))
    r6 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D30 🔴 修好上游后**同一段能重跑成功**（水位线不动的价值）",
        r6.get("ok") and r6.get("written") == 1
        and watermark(db, SID) == ids4[-1],
        str({k: r6.get(k) for k in ("ok", "written")}) + f" wm={watermark(db, SID)}")

    # ---- 空数组 = 成功 ----
    ids5 = seed_messages(db, SID, [("in", "嗯"), ("out", "嗯")])
    set_replies(items_payload())
    r7 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D31 🔴 空数组 = **成功**（reason=distilled，不是 parse_failed）",
        r7.get("ok") and r7.get("written") == 0
        and r7.get("reason") == "distilled",
        str({k: r7.get(k) for k in ("ok", "written", "reason")}))
    chk("D32 🔴 空数组**也推水位线**（否则那一小段永远卡队头）",
        watermark(db, SID) == ids5[-1], f"{watermark(db, SID)} vs {ids5[-1]}")
    chk("D33 空数组时 note 说明是「这段没什么可记的」",
        r7.get("note") == "extracted_nothing", str(r7.get("note")))

    # ---- redo ----
    alive_before = [x["id"] for x in alive(db)]
    ids6 = seed_messages(db, SID, [("in", "我改主意了"), ("out", "听你的")])
    set_replies(items_payload({"kind": "relationship", "text": "她改主意了，我听着",
                               "source_msg": ids6[0]}))
    r8 = c.post(B, headers=H(), json={"session_id": SID, "redo": True}).json()
    chk("D34 🔴 redo 真的标废了旧的（marked > 0）",
        r8.get("ok") and (r8.get("redone") or {}).get("marked", 0) > 0,
        str(r8.get("redone")))
    alive_now = [x["id"] for x in alive(db)]
    chk("D35 🔴 旧行**一条都没删**（总行数只增不减）",
        len(rows_of(db)) > len(alive_before), f"{len(rows_of(db))} vs {len(alive_before)}")
    chk("D36 🔴 被标废的批号 = 本次 run_id",
        all(x["superseded_by"] == r8.get("run_id")
            for x in rows_of(db) if x["id"] in alive_before),
        str([(x["id"], x["superseded_by"]) for x in rows_of(db)][:3]))
    chk("D37 🔴 默认读只看到新的那一批",
        len(alive(db)) == 1 and alive(db)[0]["text"] == "她改主意了，我听着",
        str([(x["text"], x["superseded_by"]) for x in rows_of(db)]))
    chk("D38 `include_superseded=1` 在端点上能看到全部",
        c.get("/app/ext/memories", headers=H(),
              params={"include_superseded": 1}).json()["count"] == len(rows_of(db)),
        str(c.get("/app/ext/memories", headers=H(),
                  params={"include_superseded": 1}).json().get("count")))

    # ---- redo 失败时旧的还在（先拿新结果才废旧的）----
    alive_ids = [x["id"] for x in alive(db)]
    ids7 = seed_messages(db, SID, [("in", "随便说说"), ("out", "嗯嗯")])
    set_replies("坏", "还是坏")
    r9 = c.post(B, headers=H(), json={"session_id": SID, "redo": True}).json()
    chk("D39 🔴 redo 跑失败时**旧的一条都没被废**（先拿新结果才废旧的）",
        r9.get("ok") is False
        and [x["id"] for x in alive(db)] == alive_ids
        and all(x["superseded_by"] is None for x in rows_of(db)
                if x["id"] in alive_ids),
        str(r9.get("reason")))

    # ---- 门槛 ----
    # 🔴 先排掉 D39（那次**失败**的 redo）留下的积压。
    #    失败的 redo **故意不推水位线**（那是它的正确行为），所以 ids6..ids7
    #    那几条还在队列里 —— 不排掉的话，"只给 1 条新消息"实测会凑出 4 条待蒸，
    #    too_few_rows 永远不触发。**这条是这套里"测试自己写错"的典型：**
    #    第一版直接断言 too_few_rows，结果实测拿到 ready（它真的跑了一次上游）。
    set_replies(items_payload({"kind": "fact", "text": "排积压的一条",
                               "source_msg": ids7[0]}))
    rd = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D40 排掉积压后水位线追上最后一条（失败的 redo 没偷偷推水位）",
        rd.get("ok") and watermark(db, SID) == ids7[-1],
        f"wm={watermark(db, SID)} vs {ids7[-1]}")

    ids8 = seed_messages(db, SID, [("in", "只有一条新消息")])
    set_replies(items_payload({"kind": "fact", "text": "x", "source_msg": ids8[0]}))
    r10 = c.post(B, headers=H(), json={"session_id": SID}).json()
    chk("D41 只够 1 条 → too_few_rows，且**不碰上游**（默认 min_rows=2）",
        r10.get("reason") == "too_few_rows" and len(upstream_calls()) == 0,
        f'{r10.get("reason")} calls={len(upstream_calls())}')
    r11 = c.post(B, headers=H(), json={"session_id": SID, "min_rows": 1}).json()
    chk("D42 `min_rows=1` 能强跑（不想被门槛拦时就显式说）",
        r11.get("ok") and r11.get("written") == 1,
        str({k: r11.get(k) for k in ("ok", "written", "reason")}))

    # ---- 空会话 redo ----
    SID_EMPTY = "api-20260925-empty"
    add_session(db, SID_EMPTY)
    r12 = c.post(B, headers=H(),
                 json={"session_id": SID_EMPTY, "redo": True}).json()
    chk("D43 🔴 没有可重的东西 → nothing_to_redo（不偷偷降级成'从头蒸'）",
        r12.get("ok") and r12.get("reason") == "nothing_to_redo", str(r12.get("reason")))

    # ---- 会话隔离：redo 不越界 ----
    home2 = tmp / "house_iso"
    home2.mkdir(parents=True, exist_ok=True)
    relay2, c2, _ = make_house(home2)
    db2 = relay2.DB_PATH
    B2 = "/app/ext/distill"
    SID_A = "api-iso-a"
    SID_B = "api-iso-b"
    add_session(db2, SID_A)
    add_session(db2, SID_B)
    # A 先蒸（id 更小）—— 这样"不 JOIN messages"的实现会把 A 也卷进来
    a_ids = seed_messages(db2, SID_A, [("in", "甲会话的第一句"), ("out", "甲会话的回复")])
    set_replies(items_payload({"kind": "fact", "text": "甲会话的记忆",
                               "source_msg": a_ids[0]}))
    ra = c2.post(B2, headers=H(), json={"session_id": SID_A}).json()
    b_ids = seed_messages(db2, SID_B, [("in", "乙会话的第一句"), ("out", "乙会话的回复")])
    set_replies(items_payload({"kind": "fact", "text": "乙会话的记忆",
                               "source_msg": b_ids[0]}))
    rb = c2.post(B2, headers=H(), json={"session_id": SID_B}).json()
    chk("D44 两个会话各自蒸出各的记忆",
        ra.get("written") == 1 and rb.get("written") == 1, f"{ra.get('written')}/{rb.get('written')}")
    set_replies(items_payload({"kind": "fact", "text": "乙会话重蒸的记忆",
                               "source_msg": b_ids[0]}))
    rr = c2.post(B2, headers=H(), json={"session_id": SID_B, "redo": True}).json()
    a_mem = [x for x in rows_of(db2) if x["text"] == "甲会话的记忆"]
    chk("D45 🔴 redo 只影响本会话（甲会话那条没被卷进来 / 没被标废）",
        a_mem and a_mem[0]["superseded_by"] is None
        and (rr.get("redone") or {}).get("marked") == 1,
        str([(x["text"], x["superseded_by"]) for x in rows_of(db2)]))

    # ---- 没有定时器 ----
    dsr = (DEPLOY / "app_ext" / "distill.py").read_text(encoding="utf-8")
    chk("D46 🔴 进程里没有定时器（源码扫描：没有 sleep 循环 / 后台任务痕迹）",
        not re.search(r"(while\s+True|asyncio\.sleep|threading\.Timer|"
                      r"add_event_handler|create_task|repeat_every)", dsr),
        "")


# ══════════════════════════════════════════════════════════════════════════
# E. 红线 / 接线 / 开关
# ══════════════════════════════════════════════════════════════════════════

def part_e(tmp: Path):
    import app_ext as AE
    from app_ext import schema as S
    sect("E. 红线 / 接线 / 开关")

    chk("E1  _ROUTES 里有 /app/ext/distill",
        "/app/ext/distill" in AE._ROUTES,
        str([r for r in AE._ROUTES if "distill" in r]))
    chk("E2  _ROUTES 里有 /app/ext/distill/status",
        "/app/ext/distill/status" in AE._ROUTES, "")
    ae_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("E3  register() 里有第 ⑩-b 步（distill.install + 开关）",
        "distill.install" in ae_src and "APP_EXT_DISTILL_DISABLED" in ae_src, "")

    dsr = (DEPLOY / "app_ext" / "distill.py").read_text(encoding="utf-8")
    chk("E4  🔴 distill.py **不 import mcp**、不注册 MCP 工具（它不是房间）",
        "import mcp" not in dsr and "_mcp" not in dsr
        and "register_tool" not in dsr and "modules" not in dsr, "")
    # 🔴 判据用**代码形态**（`"hold"` 这种带引号的调用写法），不查裸词：
    #    文档里写着"他自己的记忆走 OB 的 hold"是**对的**，不该被这条当成越界。
    chk("E5  🔴 也没有往 OB 写（P3 之前不许碰他的记忆）",
        not re.search(r'''["']hold["']|letter_write|ombre_brain|import\s+ombre''',
                      dsr, re.I), "")

    chk("E6  schema 版本 = 当前 SCHEMA_VERSION（= 6）",
        S.SCHEMA_VERSION == 6, str(S.SCHEMA_VERSION))
    from app_ext import distill as D
    sl = D.summary_line()
    bad = [c for c in "🔴✅⚠️" if c in sl]
    chk("E7  🔴 summary_line() GBK 安全（不许有编不出的符号）", not bad, f"{bad} / {sl}")
    chk("E8  summary_line() 说清了三条关键性质",
        all(k in sl for k in ("人触发", "source_msg", "软作废")), sl)

    # 关掉开关
    home = tmp / "house_off"
    home.mkdir(parents=True, exist_ok=True)
    relay_off, c_off, reg_off = make_house(home, disabled=True)
    chk("E9  🔴 关掉开关 → 两条端点 404",
        c_off.get("/app/ext/distill/status", headers=H()).status_code == 404
        and c_off.post("/app/ext/distill", headers=H(),
                       json={"session_id": "x"}).status_code == 404,
        str(reg_off.get("distill")))
    conn = sqlite3.connect(relay_off.DB_PATH)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    scols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    conn.close()
    chk("E10 🔴 关掉开关时**表与两列一样都不少**（能力没了 ≠ 数据没了）",
        "superseded_by" in cols and "distill_upto" in scols and S.SCHEMA_VERSION == 6,
        str(sorted(cols)))

    ctx_src = (DEPLOY / "app_ext" / "context.py").read_text(encoding="utf-8")
    chk("E11 🔴 ⑧ 摘要那一路也记账了（route=\"summary\"）",
        'route="summary"' in ctx_src, "")
    chk("E12 ⑧ 的记账是 fail-open（自己吞异常，不把压缩搞挂）",
        "_bill" in ctx_src and "except Exception" in ctx_src, "")

    va = (REPO / "tools" / "verify_all.py").read_text(encoding="utf-8")
    chk("E13 verify_all 已接本套（distill_check）", "distill_check" in va, "")

    _ = AE


# ══════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="distill_check_"))
    start_mock()

    print("=" * 66)
    print("蒸馏管道验收 —— P2 ⑩-b")
    print("=" * 66)

    part_a()
    part_b(tmp)
    part_c(tmp)
    part_d(tmp)
    part_e(tmp)

    ok = sum(1 for _n, o, _d in results if o)
    total = len(results)

    # 与其余各套同形：把**全项**落一份 tools/distill_report.txt。
    # 🔴 以前本套没落报告（其余 15 套都有）—— 2026-09-25 补齐这处不一致。
    #    报告是给"回头看某一条到底验的是什么"用的，所以**成功的项也要进**。
    lines = ["蒸馏管道验收 —— P2 ⑩-b",
             f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"仓库：{REPO}",
             f"临时目录：{tmp}",
             ""]
    for name, good, detail in results:
        lines.append(f"[{'OK' if good else 'XX'}] {name}"
                     + (f"   -> {detail}" if detail and not good else ""))
    n_bad = total - ok
    lines.append("")
    lines.append(f"共 {total} 项，通过 {ok}，失败 {n_bad}")
    lines.append("总检查：全部通过 ✅" if n_bad == 0 else "总检查：有失败 ❌")
    report = "\n".join(lines)
    (HERE / "distill_report.txt").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
