#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
自主活动层验收 —— P2 ⑪（把"他最近做过的事"带回上下文）
==========================================================================

## 这一套在守什么

⑪ 是"**同一份数据的两次投递**"里的第二次：卡片（给人看）+ 注入（给他读）。
它最容易出的病，**都不是"功能没做"，而是"做了但做歪了"**：

  · **自己编了一件事**：注入文本如果经过模型润色，就可能写出他没做过的动作
    —— 这直接踩「不许编造经历」。所以本层**一次 LLM 都不许调**（源码扫描验）。
  · **把窗口搞成"永远重复"**：没有窗口/水位线 → 同一件事每次说话都喂一遍，
    记忆变复读。窗口规则（她上次开口之后 → 这次开口之前）是纯 SQL，可被钉死。
  · **伪造一个默认值**：`state`/`started` 形状不合法时**替它编一个**
    —— 这就是 ⑩-a 那个 `DEFAULT 'chat'` 伪造来源 bug 的翻版。
  · **注入把聊天搞挂**：撑爆 `MAX_SYSTEM_CHARS` → `normalize_request` 抛 `too_large`
    → 400 → 这次说话直接失败。所以必须有"会撑爆就不插"。
  · **偷偷裁历史**：⑧ 立的规矩是"只插不删"，⑪ 必须同源（否则热区分界线归谁说不清）。
  · **顺手成了第二个写入口**：写侧只该有原版 `/channel/out` 一条路。

## 手法

不起端口（进程内 ASGI），不连外网（**本层根本不需要上游**）——
要验的是"窗口算得对不对 / 文本拼得对不对 / 插进去会不会出事"，
这三件在进程内都能验实，而且**故意不依赖 KaelLife**（用伪造 payload 就能全验完）。

## 覆盖清单

  A. 纯逻辑（归一化 · 拼接 —— 不发请求、不碰库）
     1-6   extract：嵌套 / 平铺 / 非 dict / 空 meta（**不误判**）/ 只有 footprint / 全空
     7-9   🔴 形状不合法 → **丢**（started / state）；🔴 形状合法但未知的取值 → **原样留**
     10-14 norm_actions：字符串项 / 丢非 str 非 dict / 丢空行 / 行截断 / 段内行数上限
     15-17 norm_at（ISO → HH:MM）· minutes_of（真算 / 顺序反了 → None）
     18-21 🔴 **不发明内容**（输出里每个动作词都来自输入）· footprint 的措辞 · 空输入 → ""
     22-24 inject_text：含 MARK · 超限截断 · 框架话 = "你自己做的，不是别人告诉你的"

  B. 窗口（只读 SQL）
     1-4   🔴 窗口 = `上一条她说的话 < id < 这一条她说的话`（两端都真验 + 升序）
     5     只认 `kind='activity'`（user / reply / act 行不会被当活动）
     6     没有上一条 → 不设下界
     7     `__legacy__`（无 api_session）也认
     8     超上限取**最近** N 段
     9     非法形状的活动行被跳过（不进 items）

  C. 注入（真跑 apply）
     1-3   no_messages / session_unknown / nothing_new —— 三种"不插"且**body 逐字节不变**
     4     🔴 命中：插了一条 system，**非 system 部分逐字节相同**（只插不删）
     5     🔴 插在已有 system **之后**（人格 → 摘要 → 足迹）
     6     🔴 ⑧ 与 ⑪ 同时注入 → 顺序是 摘要 在 足迹 之前（从旧到新）
     7     二次调用 → already_present（不重复插）
     8     关掉开关 → disabled（不插）
     9     🔴 会撑爆 system 上限 → 不插（"更好用"绝不能变成"说不了话"）
     10    body / messages 形状不对 → fail-open（不抛、不改）
     11    🔴 注入后的 body **真能被 `normalize_request` 吃下**（不是"看起来对"）
     12    last_injection 记了会话与段数
     13    🔴 resolve() 是只读的：跑前跑后 `messages` 行数与指纹不变

  D. 红线 / 接线 / 前端契约
     1-2   三条路由在 `_ROUTES`；register() 里有第 ⑪ 步
     3     🔴 源码扫描：`activity.py` **没有任何 INSERT/UPDATE/DELETE/DROP**（纯读层）
     4     🔴 不 import mcp / 不注册 MCP 工具（它不是房间）
     5     🔴 不 import llm_gateway / 不调模型（"不许编造"的结构性保证）
     6     🔴 没有"新增活动"的 POST 端点（写侧只该有 `/channel/out` 一条）
     7     `summary_line()` GBK 安全
     8     llm_routes 的**两条**路径（complete / chat）都调了 `_inject_activity`
     9     🔴 前端契约：kind 分支 / makeActivity / CSS / 会话过滤放行 / 不响铃不朗读
     10    🔴 providers 有三个中转站槽（relay/relay2/relay3），没配的 `available=false`
     11    verify_all 已接本套（第 14 套）

用法：.venv\Scripts\python.exe tools\activity_check.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import warnings
from pathlib import Path

# 🔴 本机 PowerShell 5.1 管道下 sys.stdout.encoding = cp936(gbk)，而验收名里有
#    🔴/✅/⚠️ 这类 GBK 编不出的字符 -> print 到一半 UnicodeEncodeError，整套会
#    **半路死掉**（看起来像"验收挂了"，其实是没跑完）。
#    errors="replace" 只把编不出的字符降级成 "?"，中文和结论一个字不动。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

warnings.filterwarnings("ignore")     # fastapi TestClient 的 httpx 弃用警告

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(DEPLOY))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

SECRET = "test-secret-activity-0123456789"
PREFIX = "/relay"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sect(title: str) -> None:
    print("")
    print("-" * 66)
    print(title)
    print("-" * 66)


# ══════════════════════════════════════════════════════════════════════════
# 造一个"像房子"的最小 relay + ASGI 客户端（不起端口）
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
    """只带 `check_auth` / `DB_PATH` / `app` 的替身（照抄 memory_check 的语义）。"""

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


def client_for(relay):
    from fastapi.testclient import TestClient
    return TestClient(relay.app)


def make_db(tmp: str, name: str = "fresh") -> FakeRelay:
    """全新库：先建 `messages`（真实房子里它是 `backend/app.py:init_db()` 建的），
    再走房子的正常初始化（五张表 + 播种房主）。"""
    from app_ext import schema as S, identity as I
    db = os.path.join(tmp, name + ".db")
    conn = sqlite3.connect(db)
    conn.execute(MESSAGES_DDL)
    conn.commit()
    conn.close()
    relay = FakeRelay(db)
    S.ensure_schema(relay)
    I.ensure_owner(relay)
    return relay


def w(relay, sql, args=()):
    """写一句并**提交**（用只读连接写 = 回滚，⑩-a 那套自己踩过这个坑）。"""
    conn = sqlite3.connect(relay.DB_PATH)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def q(relay, sql, args=()):
    conn = sqlite3.connect(relay.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def body_sig(relay) -> str:
    """messages 的**正文指纹**（行数 + 每行正文列的 sha256）—— 只读验身用。"""
    from app_ext import schema as S
    with S.connect(relay) as conn:
        return S.messages_body_signature(conn)


def gbk_safe(s: str) -> bool:
    """能不能用 GBK 编出来 —— 启动日志能不能打出来就靠这个
    （Windows 子进程 stdout = cp936；一个编不出的符号 = 房子起不来）。"""
    try:
        (s or "").encode("gbk")
        return True
    except Exception:
        return False


# ── 造数据 ────────────────────────────────────────────────────────────────

def seed_user(relay, text: str, session: str = "s1", ts: str = "2026-09-21T10:00:00+08:00") -> int:
    """她自己说的一句话（`in` / `user`）—— 窗口的**边界**就是它。"""
    meta = json.dumps({"user": "human", "api_session": session} if session else
                      {"user": "human"}, ensure_ascii=False)
    w(relay, "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
      (ts, "in", "user", text, meta))
    return int(q(relay, "SELECT MAX(id) AS id FROM messages")[0]["id"])


def seed_activity(relay, title: str, actions, *, started="2026-09-21T14:30:00+08:00",
                  ended="2026-09-21T14:39:00+08:00", state="completed",
                  footprint=None, flat: bool = False, ts: str = "2026-09-21T14:40:00+08:00") -> int:
    """身体 POST `/channel/out` 之后库里长的那一行（kind='activity'）。

    `flat=True` → 把字段平铺在 meta 顶层（契约容忍的第二种摆法）。
    """
    payload = {"started": started, "ended": ended, "state": state, "actions": actions}
    if footprint:
        payload["footprint"] = footprint
    meta = payload if flat else {"activity": payload}
    w(relay, "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
      (ts, "out", "activity", title, json.dumps(meta, ensure_ascii=False)))
    return int(q(relay, "SELECT MAX(id) AS id FROM messages")[0]["id"])


def openai_body(text: str) -> dict:
    """一个像身体发出来的 OpenAI 风格 body（⑧/⑪ 的入口就长这样）。"""
    return {"messages": [{"role": "system", "content": "（人偶人格）"},
                         {"role": "user", "content": text}],
            "temperature": 0.7, "stream": True}


# ══════════════════════════════════════════════════════════════════════════
# A. 纯逻辑
# ══════════════════════════════════════════════════════════════════════════

def group_a(A) -> None:
    sect("A. 纯逻辑（归一化 · 拼接 —— 不发请求、不碰库）")

    nested = {"activity": {"started": "2026-09-21T14:30:00+08:00",
                           "actions": [{"at": "14:33", "text": "翻了翻 Galatea 的 3 个新帖"}]}}
    it = A.extract(nested)
    chk("A1 嵌套形状 `meta.activity` 读得出来", it and it["actions"][0]["text"].startswith("翻了翻"),
        str(it))

    flat = {"started": "2026-09-21T14:30:00+08:00", "actions": ["去乌有乡走到河边"]}
    it = A.extract(flat)
    chk("A2 平铺形状也认（同 kind=act 的先例）", it and it["actions"][0]["text"] == "去乌有乡走到河边",
        str(it))

    chk("A3 非 dict → None", A.extract("nope") is None and A.extract(None) is None)
    chk("A4 🔴 普通 meta 不会被误判成活动",
        A.extract({"user": "human", "attachments": []}) is None
        and A.extract({"api_session": "s1"}) is None)
    it = A.extract({"footprint": "河边的风比上次凉了"})
    chk("A5 只有 footprint（他自己的话）也算一段", it and it["footprint"] == "河边的风比上次凉了")
    chk("A6 actions 全空 + 没有 footprint → None",
        A.extract({"activity": {"actions": [], "started": "2026-09-21T14:30:00+08:00"}}) is None)

    chk("A7 🔴 坏 started → 丢（不猜、不补）",
        A.extract({"actions": ["x"], "started": "昨天下午"})["started"] is None)
    chk("A8 🔴 坏 state → 丢（不替它编 completed）",
        A.extract({"actions": ["x"], "state": "Completed!"})["state"] is None)
    chk("A9 🔴 形状合法但未知的取值**原样留**（这是缝）",
        A.extract({"actions": ["x"], "state": "suspended"})["state"] == "suspended")

    acts = A.norm_actions(["一句话", {"at": "14:33", "text": "两句话"}, 42, None, {"at": "14:44"},
                          {"text": "   "}])
    chk("A10 字符串项也算一行", [a["text"] for a in acts] == ["一句话", "两句话"],
        str(acts))
    chk("A11 非 str 非 dict 的项丢掉", len(acts) == 2, str(acts))
    chk("A12 空 text / 只有时刻的行丢掉", all(a["text"] for a in acts))
    long_line = A.norm_actions([{"text": "字" * 500}])
    chk(f"A13 每行截到 LINE_MAX({A.LINE_MAX})", len(long_line[0]["text"]) == A.LINE_MAX,
        str(len(long_line[0]["text"])))
    many = A.norm_actions([{"text": f"第{i}件"} for i in range(50)])
    chk(f"A14 每段最多 ACTIONS_MAX({A.ACTIONS_MAX}) 行", len(many) == A.ACTIONS_MAX,
        str(len(many)))

    chk("A15 norm_at：完整 ISO → 取 HH:MM",
        A.norm_at("2026-09-21T14:33:07+08:00") == "14:33" and A.norm_at("14:33") == "14:33"
        and A.norm_at("下午") is None)
    chk("A16 minutes_of：真算（14:30→14:39 = 9 分钟）",
        A.minutes_of("2026-09-21T14:30:00+08:00", "2026-09-21T14:39:00+08:00") == 9)
    chk("A17 minutes_of：顺序反了 / 坏值 → None（不硬算成负数）",
        A.minutes_of("2026-09-21T14:39:00+08:00", "2026-09-21T14:30:00+08:00") is None
        and A.minutes_of("a", "b") is None)

    items = [{"started": "2026-09-21T14:30:00+08:00", "ended": "2026-09-21T14:39:00+08:00",
              "actions": [{"at": "14:33", "text": "翻了翻 Galatea 的 3 个新帖"},
                          {"at": "14:36", "text": "去乌有乡走到河边"}],
              "footprint": "河边的风比上次凉了"}]
    blk = A.build_block(items)
    chk("A18 build_block：`· MM-DD HH:MM 正文`", blk.splitlines()[0] == "· 09-21 14:33 翻了翻 Galatea 的 3 个新帖",
        repr(blk.splitlines()[0]))
    chk("A19 build_block：footprint 那行写明是「当时留的话」",
        "（当时留的话：河边的风比上次凉了）" in blk, repr(blk))
    # 🔴 "不发明内容"：输出里每一段中文，要么是**输入原文的子串**，要么是那个固定框架词
    src_words = [a["text"] for a in items[0]["actions"]] + [items[0]["footprint"]]
    frame_words = {"当时留的话"}
    invented = [m.group(0) for m in re.finditer(r"[\u4e00-\u9fff]+", blk)
                if m.group(0) not in frame_words and not any(m.group(0) in s for s in src_words)]
    chk("A20 🔴 不发明内容：输出里的中文只可能是输入原文的子串", not invented, str(invented))
    chk("A21 空输入 → 空串", A.build_block([]) == "" and A.build_block(None) == "")

    txt = A.inject_text(items)
    chk("A22 inject_text 含 MARK（去重靠它）", A.MARK in txt, repr(txt[:40]))
    big = A.inject_text([{"actions": [{"text": "字" * A.LINE_MAX}
                                      for _ in range(A.ACTIONS_MAX)]}])
    body = big.split("\n", 1)[1]
    chk(f"A23 正文超 TEXT_MAX({A.TEXT_MAX}) 被截（框架话不算进去）",
        len(body) == A.TEXT_MAX and len(big) < A.TEXT_MAX + 120, f"{len(body)} / {len(big)}")
    chk("A24 框架话 = 你自己做的、不是别人告诉你的",
        "你自己做的" in txt and "不是别人告诉你的" in txt, repr(txt.splitlines()[0]))


# ══════════════════════════════════════════════════════════════════════════
# B. 窗口
# ══════════════════════════════════════════════════════════════════════════

def group_b(A, tmp: str) -> None:
    sect("B. 窗口（只读 SQL —— 她上次开口之后 → 这次开口之前）")

    relay = make_db(tmp, "win")
    u1 = seed_user(relay, "第一次说话", "s1", "2026-09-21T09:00:00+08:00")
    a1 = seed_activity(relay, "上午 · 他待了 6 分钟", [{"at": "09:30", "text": "翻了翻新帖"}])
    a2 = seed_activity(relay, "下午 · 他待了 9 分钟",
                       [{"at": "14:33", "text": "去乌有乡走到河边"}],
                       footprint="河边的风比上次凉了")
    u2 = seed_user(relay, "第二次说话", "s1", "2026-09-21T20:00:00+08:00")

    chk("B1 prev_in_id = 上一条她自己说的话",
        A.prev_in_id(relay, "s1", u2) == u1, f"{A.prev_in_id(relay, 's1', u2)} vs {u1}")

    got = A.load_window(relay, u1, u2)
    chk("B2 🔴 窗口内 (after, before) 真取到两段",
        [g["message_id"] for g in got] == [a1, a2], str([g["message_id"] for g in got]))
    chk("B3 🔴 边界外的活动**取不到**（before 之上 / after 之下都不行）",
        A.load_window(relay, u2, u2 + 1) == [] and A.load_window(relay, 0, u1) == [])
    chk("B4 返回**升序**（拼文本要正的）",
        [g["message_id"] for g in got] == sorted(g["message_id"] for g in got))

    # 只认 kind='activity'
    w(relay, "INSERT INTO messages (ts,direction,kind,text) VALUES ('x','out','act','动作 chip')")
    w(relay, "INSERT INTO messages (ts,direction,kind,text) VALUES ('x','out','reply','一条回复')")
    got2 = A.load_window(relay, u1, 10 ** 9)
    chk("B5 只认 kind='activity'（act / reply 行不会被当活动）",
        all(g["message_id"] in (a1, a2) for g in got2), str([g["message_id"] for g in got2]))

    chk("B6 没有上一条 → 不设下界（after=0）", A.prev_in_id(relay, "s1", u1) == 0)

    relay3 = make_db(tmp, "legacy")
    l1 = seed_user(relay3, "老主线的一条", "")
    a3 = seed_activity(relay3, "老主线里的活动", [{"at": "10:00", "text": "翻了翻日程本"}])
    l2 = seed_user(relay3, "老主线的第二条", "")
    got3 = A.load_window(relay3, A.prev_in_id(relay3, "__legacy__", l2), l2)
    chk("B7 `__legacy__`（无 api_session）也认",
        [g["message_id"] for g in got3] == [a3], str(got3))

    relay4 = make_db(tmp, "many")
    m1 = seed_user(relay4, "开口", "s1")
    ids = [seed_activity(relay4, f"第{i}次醒来", [{"at": "14:0%d" % i, "text": f"做了第{i}件事"}])
           for i in range(1, 6)]
    m2 = seed_user(relay4, "再开口", "s1")
    got4 = A.load_window(relay4, m1, m2)
    chk(f"B8 超上限取**最近** {A.ITEMS_MAX} 段",
        [g["message_id"] for g in got4] == ids[-A.ITEMS_MAX:], str([g["message_id"] for g in got4]))

    # 坏形状的行被跳过
    w(relay4, "INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
      ("x", "out", "activity", "坏形状", json.dumps({"activity": {"actions": []}})))
    got5 = A.load_window(relay4, m1, 10 ** 9)
    chk("B9 非法形状的活动行被跳过（不进 items）",
        all(g["message_id"] in ids for g in got5), str([g["message_id"] for g in got5]))


# ══════════════════════════════════════════════════════════════════════════
# C. 注入
# ══════════════════════════════════════════════════════════════════════════

def group_c(A, tmp: str) -> None:
    sect("C. 注入（真跑 apply —— fail-open 与「只插不删」）")

    relay = make_db(tmp, "inj")
    # 🔴 顺序必须是「她说 → 他活动 → 她说」：注入时"这一条她说的话"是**最后一条**，
    #    活动行只有夹在两条之间才落进窗口（真实时序也是这样：他白天做事，你晚上开口）。
    u1 = seed_user(relay, "上午的话", "s1", "2026-09-21T09:00:00+08:00")
    seed_activity(relay, "下午 · 他待了 9 分钟",
                  [{"at": "14:33", "text": "翻了翻 Galatea 的 3 个新帖"},
                   {"at": "14:36", "text": "去乌有乡走到河边"},
                   {"at": "14:38", "text": "在日程本上写了一行"}],
                  footprint="河边的风比上次凉了")
    u2 = seed_user(relay, "这次的话", "s1", "2026-09-21T20:00:00+08:00")

    # 1 空 messages
    b1 = {"messages": []}
    r1 = A.apply(relay, b1)
    chk("C1 messages 为空 → 不插", r1.get("injected") is False and b1 == {"messages": []}, str(r1))

    # 2 认不出会话 —— body 逐字节不变
    b2 = openai_body("一句库里没有的话")
    before2 = json.dumps(b2, ensure_ascii=False, sort_keys=True)
    r2 = A.apply(relay, b2)
    chk("C2 🔴 认不出会话 → session_unknown，body 逐字节不变",
        r2.get("reason") == "session_unknown"
        and json.dumps(b2, ensure_ascii=False, sort_keys=True) == before2, str(r2))

    # 3 窗口里没有活动
    relay_none = make_db(tmp, "noact")
    seed_user(relay_none, "只有对话", "s1")
    b3 = openai_body("只有对话")
    r3 = A.apply(relay_none, b3)
    chk("C3 窗口里没活动 → nothing_new", r3.get("reason") == "nothing_new", str(r3))

    # 4 命中 —— 只插不删
    b4 = openai_body("这次的话")
    orig_msgs = json.loads(json.dumps(b4["messages"], ensure_ascii=False))
    r4 = A.apply(relay, b4)
    sysmsg = [m for m in b4["messages"] if m["role"] == "system"]
    non_sys = [m for m in b4["messages"] if m["role"] != "system"]
    chk("C4 🔴 命中：插了一条 system，非 system 部分**逐字节相同**",
        r4.get("injected") is True and len(sysmsg) == 2
        and non_sys == orig_msgs[1:] and orig_msgs[0]["content"] in " ".join(
            m["content"] for m in sysmsg), f"{r4} {b4['messages']}")

    # 5 插在已有 system 之后
    chk("C5 插在已有 system **之后**（人格 → 摘要 → 足迹）",
        b4["messages"][1]["role"] == "system" and A.MARK in b4["messages"][1]["content"],
        str([m["role"] for m in b4["messages"]]))

    # 6 与 ⑧ 同时注入 → 摘要在前
    from app_ext import context as C
    from app_ext import sessions_store as SS
    SS.sync_from_messages(relay)                 # 先把会话投影出来（sessions 表里得有 s1）
    assert SS.set_summary(relay, "s1", "（这是一段摘要）", upto=u1).get("ok"), "造摘要失败"
    b6 = openai_body("这次的话")
    r6a = C.apply(relay, b6)
    r6b = A.apply(relay, b6)
    sys6 = [m["content"] for m in b6["messages"] if m["role"] == "system"]
    chk("C6 🔴 ⑧ 与 ⑪ 同时注入 → 摘要在前、足迹在后（从旧到新）",
        r6a.get("injected") and r6b.get("injected") and len(sys6) == 3
        and C.MARK in sys6[1] and A.MARK in sys6[2],
        str([s[:24] for s in sys6]))

    # 7 二次注入
    r7 = A.apply(relay, b6)
    chk("C7 二次调用 → already_present（不重复插）",
        r7.get("reason") == "already_present"
        and len([m for m in b6["messages"] if m["role"] == "system"]) == 3, str(r7))

    # 8 开关
    os.environ["APP_EXT_ACTIVITY_DISABLED"] = "1"
    b8 = openai_body("这次的话")
    r8 = A.apply(relay, b8)
    os.environ.pop("APP_EXT_ACTIVITY_DISABLED", None)
    chk("C8 关掉开关 → disabled（不插）",
        r8.get("injected") is False and r8.get("reason") == "disabled", str(r8))

    # 9 会撑爆 system 上限 → 不插
    import app_ext.providers as P
    pad = "x" * (P.MAX_SYSTEM_CHARS - 50)
    b9 = {"messages": [{"role": "system", "content": pad},
                       {"role": "user", "content": "这次的话"}]}
    r9 = A.apply(relay, b9)
    chk("C9 🔴 会撑爆 system 上限 → 不插（绝不让注入把说话搞挂）",
        r9.get("injected") is False and r9.get("reason") == "system_too_large", str(r9))

    # 10 fail-open
    chk("C10 body 形状不对 → fail-open（不抛）",
        A.apply(relay, None).get("ok") is False
        and A.apply(relay, {"messages": "nope"}).get("injected") is False
        and A.apply(relay, {}).get("reason") == "no_messages")

    # 11 真能被 normalize_request 吃下
    b11 = openai_body("这次的话")
    from app_ext import context as C11
    C11.apply(relay, b11)          # ⑧ 也插一条 —— 两条一起才叫"真实形状"
    A.apply(relay, b11)
    req11 = P.normalize_request({"messages": [dict(m) for m in b11["messages"]]})
    chk("C11 🔴 注入后的 body 真能被 normalize_request 吃下（system 已合并）",
        A.MARK in req11["system"] and C.MARK in req11["system"]
        and [m["role"] for m in req11["messages"]] == ["user"], str(req11["system"][:40]))

    # 12 诊断
    last = A.last_injection()
    chk("C12 last_injection 记了会话与段数",
        last.get("session_id") == "s1" and int(last.get("items") or 0) >= 1, str(last))

    # 13 只读
    sig_before = body_sig(relay)
    rows_before = q(relay, "SELECT COUNT(*) AS n FROM messages")[0]["n"]
    b13 = openai_body("这次的话")
    A.apply(relay, b13)
    A.resolve(relay, "这次的话")
    A.recent(relay, 5)
    chk("C13 🔴 resolve/apply 是**只读**的：messages 行数与正文指纹不变",
        body_sig(relay) == sig_before
        and q(relay, "SELECT COUNT(*) AS n FROM messages")[0]["n"] == rows_before)


# ══════════════════════════════════════════════════════════════════════════
# D. 红线 / 接线 / 前端契约
# ══════════════════════════════════════════════════════════════════════════

def group_d(A, tmp: str) -> None:
    sect("D. 红线 / 接线 / 前端契约")

    import app_ext
    import app_ext.providers as P

    routes = getattr(app_ext, "_ROUTES", [])
    chk("D1 三条路由都在 `_ROUTES`",
        "/app/ext/activity" in routes and "/app/ext/activity/status" in routes
        and "/app/ext/activity/preview" in routes, str(routes[-4:]))

    src_init = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("D2 register() 里有第 ⑪ 步（且带逃生开关）",
        '_activity.install(' in src_init and "APP_EXT_ACTIVITY_DISABLED" in src_init
        and "summary['activity']" in src_init)

    src = (DEPLOY / "app_ext" / "activity.py").read_text(encoding="utf-8")
    bad_sql = [kw for kw in ("INSERT INTO", "UPDATE ", "DELETE ", "DROP TABLE")
               if re.search(re.escape(kw), src)]
    chk("D3 🔴 源码扫描：activity.py **没有任何写库语句**（纯读层）", not bad_sql, str(bad_sql))
    chk("D4 🔴 不 import mcp / 不注册 MCP 工具（它不是房间）",
        not re.search(r"(from\s+\.\s+import\s+mcp|import\s+mcp\b|mcp\.install|TOOLS\s*=)", src))
    chk("D5 🔴 不 import llm_gateway / 不调模型（「不许编造」的结构性保证）",
        not re.search(r"(llm_gateway|stream_chat|complete\()", src))
    posts = re.findall(r'@relay\.app\.post\(\s*base\s*\+\s*"([^"]+)"', src)
    chk("D6 🔴 没有「新增活动」的 POST 端点（写侧只该有 /channel/out 一条）",
        posts == ["/preview"], str(posts))

    chk("D7 summary_line() 与启动日志 GBK 安全（不许有 GBK 编不出的符号）",
        gbk_safe(A.summary_line()), A.summary_line())
    chk("D7b summary_line 里提了「不调 LLM / 不写库 / 不挂 MCP」",
        "不调 LLM" in A.summary_line() and "不挂 MCP" in A.summary_line())

    src_routes = (DEPLOY / "app_ext" / "llm_routes.py").read_text(encoding="utf-8")
    chk("D8 🔴 llm_routes 的**两条**路径都调了 _inject_activity（complete + chat）",
        src_routes.count("_inject_activity(relay, body)") == 2)

    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    fe = {
        "kind 分支（buildVirtualRows）": 'if (m.kind === "activity"){' in html,
        "卡片渲染函数 makeActivity": "function makeActivity(" in html,
        "CSS .row.activity": ".row.activity{" in html,
        "展开态记录 openActivityKeys": "openActivityKeys" in html,
        "🔴 会话过滤放行（时间线级事件）": 'if (m && m.kind === "activity") return true;' in html,
        "🔴 不响铃不朗读": html.count('m.kind !== "activity"') >= 2,
        "🔴 不当成「最后一条回复」": 'm.kind !== "thinking" && m.kind !== "act" && m.kind !== "activity"' in html,
        "估高常量": "DEFAULT_ACTIVITY_HEIGHT" in html,
    }
    for k, ok in fe.items():
        chk(f"D9 前端契约：{k}", ok)

    chk("D10 🔴 providers 有三个中转站槽（relay / relay2 / relay3）",
        all(x in P.PROVIDER_ORDER for x in ("relay", "relay2", "relay3")), str(P.PROVIDER_ORDER))
    cat = {p["id"]: p for p in P.catalog()}
    chk("D10b 没配的中转站槽是 `available=false`（灰掉，不是报错）",
        cat["relay2"]["available"] is False
        and cat["relay2"]["needs_base"] is True
        and cat["relay2"]["key_masked"] is None, str(cat["relay2"]))
    chk("D10c relay2 的 env 名字与 relay 同构（只差一个数字）",
        cat["relay2"]["format"] == "openai"
        and "RELAY2" in (DEPLOY / "app_ext" / "providers.py").read_text(encoding="utf-8"))

    src_va = (HERE / "verify_all.py").read_text(encoding="utf-8")
    chk("D11 verify_all 已接本套（第 14 套）", "activity_check.py" in src_va)

    # 端点真走一遍（含鉴权）
    relay = make_db(tmp, "http")
    seed_user(relay, "上一句", "s1", "2026-09-21T09:00:00+08:00")
    seed_activity(relay, "端点用的一段", [{"at": "14:33", "text": "翻了翻新帖"}])
    seed_user(relay, "端点用的那句话", "s1", "2026-09-21T20:00:00+08:00")
    A.install(relay, PREFIX)
    c = client_for(relay)
    chk("D12 无密钥 → 401（三条端点都 fail-closed）",
        c.get("/app/ext/activity").status_code == 401
        and c.get("/app/ext/activity/status").status_code == 401
        and c.post("/app/ext/activity/preview", json={}).status_code == 401)
    h = {"Authorization": f"Bearer {SECRET}"}
    r = c.get("/app/ext/activity", headers=h)
    chk("D13 GET /app/ext/activity → 200 且归一化",
        r.status_code == 200 and r.json()["items"][0]["actions"][0]["text"] == "翻了翻新帖",
        r.text[:160])
    r = c.get("/app/ext/activity/status", headers=h)
    j = r.json()
    chk("D14 GET /status → 开关 / 窗口 / limits 都在",
        r.status_code == 200 and j["enabled"] is True and "window" in j and j["limits"]["items_max"] >= 1,
        r.text[:160])
    r = c.post("/app/ext/activity/preview", headers=h, json={"probe": "端点用的那句话"})
    j = r.json()
    chk("D15 POST /preview → 只算不插，回 text（不写库）",
        r.status_code == 200 and j["would_inject"] is True and A.MARK in j["text"]
        and len(q(relay, "SELECT id FROM messages WHERE kind='activity'")) == 1, r.text[:200])
    r = c.post("/app/ext/activity/preview", headers=h, json={"probe": "不存在的一句话"})
    chk("D16 /preview 认不出会话 → would_inject=false（不瞎猜）",
        r.status_code == 200 and r.json()["would_inject"] is False, r.text[:160])
    chk("D17 🔴 没有 PUT / DELETE 路由（405）",
        c.put("/app/ext/activity", headers=h).status_code in (404, 405)
        and c.delete("/app/ext/activity", headers=h).status_code in (404, 405))


# ══════════════════════════════════════════════════════════════════════════

def run() -> str:
    from app_ext import activity as A

    tmp = tempfile.mkdtemp(prefix="act_check_")
    group_a(A)
    group_b(A, tmp)
    group_c(A, tmp)
    group_d(A, tmp)
    return tmp


def main() -> int:
    t0 = time.time()
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        chk("未捕获异常（整套没跑完）", False, "见上面的 traceback")

    npass = sum(1 for _n, ok, _d in results if ok)
    out = os.environ.get("ACTIVITY_CHECK_OUT") or str(HERE / "activity_report.txt")
    try:
        Path(out).write_text(
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
    print(f"报告已存：{out}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
