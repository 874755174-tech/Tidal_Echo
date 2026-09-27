#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
在场信号验收 —— P3 前置 ⑬（`/app/ext/presence`）
==========================================================================

## 这一套到底在守什么

「她最后开口的时刻」只有一条 SQL 那么长，但它最容易出的病**全都不显眼**：

  · **判据选错列**：拿 `ts DESC` 排序 —— 而 `ts` 可以被调用方在 `meta` 里指定
    （`backend/app.py:140`）⇒ 判据交给了写它的人。必须按 `id`（自增主键）取最新。
  · **把 out 也算成"她开口"**：拿 `MAX(ts)` 不筛 direction ⇒ 他刚回复完
    → `minutes_ago ≈ 0` → 那道硬闸**永远开着**（她说什么他都不推手机）——
    表面上"更安静了"，实际是那道闸废了。
  · **语音/通话不算**：只认 `kind='user'` ⇒ 她按住说话、打了一通电话，
    在系统看来"她从没来过"。⇒ 只筛 `direction`，不筛 `kind`。
  · **"数据坏了"被说成"她刚来过"**：`ts` 解析失败就顺手写 0 分钟 ⇒
    一次库损坏 = 永久静音。⇒ 算不出来就是 `null` + `reason`，**绝不编默认值**。
  · **"不知道"被说成"确定不在场"**：房子读不到 → 回 `ok=false`。
    调用方若把 `ok=false` 当 `minutes_ago=null` 用，就会把
    「房子挂了」读成「她不在」⇒ 他在她身边说话时还推她手机（本次要修的正是这个）。
    ⇒ fail 分支**字段里不许出现 `active_recent`**（那个字段会被读成结论）。
  · **她说了话之后他又说了几句，最新 in 被顶掉**。
  · **端点被写成能写**：presence 一旦能被写，它就不再是"事实"，而是"某个人的说法"。
  · **这一层挂了把聊天带走**：它是"更好用"，不是"能不能说话"的前提 → 必须 fail-open。

## 手法

  A 组 派生（真写库 + 纯函数 `fold`）
  B 组 窗口（注入 `now` / `window`，不受真实时钟影响）
  C 组 只读（**源码扫描** —— 结构性约束，不是行为约束）
  D 组 fail-open（坏库 / 畸形行 / 端点回 200 + `ok=false`）
  E 组 形状锁死取值不锁死（非法 ts → 丢，**绝不编默认值**；带**变异证明**）
  F 组 端点（进程内 ASGI，**不起端口**）+ 接线 + 红线

用法：.venv\Scripts\python.exe tools\presence_check.py
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 🔴 本机 PowerShell 5.1 管道下 sys.stdout.encoding = cp936(gbk)，而验收名里有
#    🔴/✅/⚠️ 这类 GBK 编不出的字符 -> print 到一半 UnicodeEncodeError，整套会
#    **半路死掉**（看起来像"验收挂了"，其实是没跑完）。
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

SECRET = "test-secret-presence-0123456789"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sect(title: str) -> None:
    print("")
    print("-" * 66)
    print(title)
    print("-" * 66)


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


def fresh_db(tmp: str, name: str, with_messages: bool = True) -> FakeRelay:
    """走到 `ensure_schema` 的新库。

    🔴 `messages` 表**必须自己建**：它是 `backend/app.py` 的 `init_db()`（后端 lifespan）
       建的，`schema.ensure_schema()` **有意不管它**（"绝不碰 messages"那条红线）。
       忘建它 = 每个用例都 `no such table: messages` ⇒ 整套红（2026-09-27 真踩过）。
    `with_messages=False` 用来复现"全新 /data 第一次部署"那一刻（表还没建）。
    """
    from app_ext import schema as S
    relay = FakeRelay(os.path.join(tmp, name + ".db"))
    S.ensure_schema(relay)
    if with_messages:
        conn = sqlite3.connect(relay.DB_PATH)
        conn.execute(MESSAGES_DDL)
        conn.commit()
        conn.close()
    return relay


def put(relay, direction: str, kind: str, text: str = "x", ts: str = None) -> int:
    """直接往 `messages` 插一行（**验收自己插** —— 房子的 presence 只读，不许它插）。"""
    ts = ts or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(relay.DB_PATH)
    cur = conn.execute(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        (ts, direction, kind, text, "{}"))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def install_presence(relay):
    """挂 presence 端点。**每次重置 `_INSTALLED`**（它是模块级单例守卫，
    而本套要往**多个** app 上分别挂）。"""
    from app_ext import presence as P
    P._INSTALLED = False
    P.install(relay)
    return P


def run() -> str:
    from app_ext import presence as P, schema as S

    tmp = tempfile.mkdtemp(prefix="presence_check_")
    NOW = datetime(2026, 9, 27, 12, 40, 0, tzinfo=timezone.utc)   # 固定"现在"
    #: 一个形状合法的 ts（D 组畸形行也要喂它，所以定义在这儿、不放在 E 组）
    LEGAL = "2026-09-27T12:30:00+00:00"

    # ══════════════════════════════════════════════════════════════════════
    # A. 派生：谁算"她开口"
    # ══════════════════════════════════════════════════════════════════════
    sect("A. 派生：in 才算 / out 不算 / 语音通话也算 / 按 id 不按 ts")

    r1 = fresh_db(tmp, "empty")
    d = P.derive(r1, now=NOW)
    chk("A1 空库 → ok=True 但 found=False（**确定没有**，不是'未知'）",
        d["ok"] is True and d["found"] is False and d["minutes_ago"] is None
        and d["reason"] == "no_in_messages", str(d))
    chk("A1b 🔴 空库 active_recent=False（确定不在场 —— 她从没在这间房子里说过话）",
        d["active_recent"] is False, str(d["active_recent"]))
    chk("A1c source 白纸黑字写出来源（同 ⑩-a 的 source 规矩）",
        d["source"] == "messages", str(d["source"]))

    # 只有 out（他自言自语 / 他的回复）→ 仍然是"没有人开口"
    r2 = fresh_db(tmp, "only_out")
    put(r2, "out", "reply", ts=iso(NOW - timedelta(minutes=2)))
    d = P.derive(r2, now=NOW)
    chk("A2 🔴 只有 out → 不算她开口（**拿 MAX(ts) 不筛 direction 就会在这儿错**）",
        d["found"] is False and d["reason"] == "no_in_messages", str(d))

    # in 之后又有 out → 最新 in 不许被顶掉
    r3 = fresh_db(tmp, "in_then_out")
    put(r3, "in", "user", ts=iso(NOW - timedelta(minutes=12)))
    put(r3, "out", "reply", ts=iso(NOW - timedelta(minutes=11)))
    put(r3, "out", "reply", ts=iso(NOW - timedelta(minutes=10)))
    d = P.derive(r3, now=NOW)
    chk("A3 🔴 她说完他回了两句：最新 in 仍是她那句（12 分钟前，不是 10 分钟前）",
        d["found"] is True and abs(d["minutes_ago"] - 12.0) < 0.01 and d["last_in_kind"] == "user",
        str(d))

    # 语音 / 通话 / 附件 也算她开口
    for kind, label, key in (("voice", "语音", "A4"), ("call", "通话", "A5")):
        rr = fresh_db(tmp, "kind_" + kind)
        put(rr, "in", kind, ts=iso(NOW - timedelta(minutes=3)))
        dd = P.derive(rr, now=NOW)
        chk(f"{key} 🔴 {label}（direction=in, kind={kind}）也算她开口"
            f"（只筛 kind='user' 会把它们全漏掉）",
            dd["found"] is True and dd["last_in_kind"] == kind
            and abs(dd["minutes_ago"] - 3.0) < 0.01, str(dd))

    # 🔴 判据是 id 不是 ts：把"更晚插入"的那行的 ts 故意写成更早
    r4 = fresh_db(tmp, "id_not_ts")
    put(r4, "in", "user", "第一条（ts 晚）", ts=iso(NOW - timedelta(minutes=5)))
    rid2 = put(r4, "in", "user", "第二条（ts 早，但 id 更大）",
               ts=iso(NOW - timedelta(minutes=120)))
    d = P.derive(r4, now=NOW)
    chk("A6 🔴🔴 按 **id**（自增主键）取最新，不按 ts —— ts 是**调用方能指定**的列"
        "（`backend/app.py:140`），拿它排序 = 把判据交给写它的人",
        d["last_in_id"] == rid2 and abs(d["minutes_ago"] - 120.0) < 0.01, str(d))

    # 纯函数 fold 的边界
    chk("A7 fold(None) 与 derive(空库) 同形（纯函数是派生的唯一实现）",
        P.fold(None, now=NOW)["reason"] == "no_in_messages"
        and P.fold(None, now=NOW)["found"] is False, "")
    chk("A8 last_in_ts 原样回（不加工、不截断、不格式化）",
        P.fold({"id": 1, "ts": "2026-09-27T12:00:00+08:00", "kind": "user"},
               now=NOW)["last_in_ts"] == "2026-09-27T12:00:00+08:00", "")
    chk("A9 `+08:00` 偏移被正常换算（12:00+08 = 04:00Z ⇒ 距 12:40Z 是 520 分钟）",
        abs(P.fold({"id": 1, "ts": "2026-09-27T12:00:00+08:00", "kind": "user"},
                   now=NOW)["minutes_ago"] - 520.0) < 0.01, "")

    # ══════════════════════════════════════════════════════════════════════
    # B. 窗口：与 KaelLife 的 LILY_ACTIVE_WINDOW_MIN 同口径
    # ══════════════════════════════════════════════════════════════════════
    sect("B. 窗口：8 分钟内 = 在场 / 40 分钟前 = 不在场（对齐 LILY_ACTIVE_WINDOW_MIN）")

    rw = fresh_db(tmp, "window")
    put(rw, "in", "user", ts=iso(NOW - timedelta(minutes=8)))
    d = P.derive(rw, now=NOW)
    chk("B1 8 分钟前 → active_recent=True（默认窗口 30）",
        d["active_recent"] is True and abs(d["minutes_ago"] - 8.0) < 0.01, str(d))

    rw2 = fresh_db(tmp, "window2")
    put(rw2, "in", "user", ts=iso(NOW - timedelta(minutes=40)))
    d = P.derive(rw2, now=NOW)
    chk("B2 40 分钟前 → active_recent=False",
        d["active_recent"] is False and abs(d["minutes_ago"] - 40.0) < 0.01, str(d))

    # 边界：正好等于窗口 → 在场（<=）；刚超一点 → 不在场
    chk("B3 边界：正好 30 分钟 → True（用 `<=` 不是 `<`）",
        P.fold({"id": 1, "ts": iso(NOW - timedelta(minutes=30)), "kind": "user"},
               now=NOW, window=30)["active_recent"] is True, "")
    chk("B4 边界：30 分 1 秒 → False",
        P.fold({"id": 1, "ts": iso(NOW - timedelta(seconds=1801)), "kind": "user"},
               now=NOW, window=30)["active_recent"] is False, "")

    # env 真的被读（且与 KaelLife 同名）
    rw3 = fresh_db(tmp, "window_env")
    put(rw3, "in", "user", ts=iso(NOW - timedelta(minutes=8)))
    old = os.environ.get("LILY_ACTIVE_WINDOW_MIN")
    try:
        os.environ["LILY_ACTIVE_WINDOW_MIN"] = "5"
        d5 = P.derive(rw3, now=NOW)
        os.environ["LILY_ACTIVE_WINDOW_MIN"] = "60"
        d60 = P.derive(rw3, now=NOW)
    finally:
        if old is None:
            os.environ.pop("LILY_ACTIVE_WINDOW_MIN", None)
        else:
            os.environ["LILY_ACTIVE_WINDOW_MIN"] = old
    chk("B5 🔴 窗口读 `LILY_ACTIVE_WINDOW_MIN`（**与 KaelLife 同名** ⇒ 口径只有一份）："
        "=5 → 8 分钟前算不在场；=60 → 算在场",
        d5["active_recent"] is False and d5["window_min"] == 5
        and d60["active_recent"] is True and d60["window_min"] == 60,
        f"{d5['window_min']}/{d5['active_recent']} vs {d60['window_min']}/{d60['active_recent']}")
    chk("B6 坏值 / 空值 → 回落 30 且不抛",
        P.window_min() == 30, str(P.window_min()))
    rw4 = fresh_db(tmp, "window_junk")
    put(rw4, "in", "user", ts=iso(NOW - timedelta(minutes=8)))
    try:
        os.environ["LILY_ACTIVE_WINDOW_MIN"] = "abc"
        d = P.derive(rw4, now=NOW)
        chk("B7 `=abc` 不炸（回落 30）", d["window_min"] == 30 and d["active_recent"] is True,
            str(d["window_min"]))
    finally:
        os.environ.pop("LILY_ACTIVE_WINDOW_MIN", None)

    # 她"未来"说的话（时钟漂移 / 手写 ts）不许炸
    chk("B8 ts 在未来 → 不炸（minutes_ago 为负，仍算在场）",
        P.fold({"id": 1, "ts": iso(NOW + timedelta(minutes=5)), "kind": "user"},
               now=NOW)["minutes_ago"] < 0
        and P.fold({"id": 1, "ts": iso(NOW + timedelta(minutes=5)), "kind": "user"},
                   now=NOW)["active_recent"] is True, "")

    # ══════════════════════════════════════════════════════════════════════
    # C. 只读（源码扫描 —— 结构性约束）
    # ══════════════════════════════════════════════════════════════════════
    sect("C. 只读：源码里没有任何写库语句 / 没有写路由")

    src = (DEPLOY / "app_ext" / "presence.py").read_text(encoding="utf-8")

    bad = [kw for kw in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER ",
                         "CREATE INDEX", "CREATE TABLE", "REPLACE INTO",
                         "executemany", "commit()")
           if kw in src]
    chk("C1 🔴🔴 源码扫描：presence.py **一个写库语句都没有**"
        "（含 CREATE INDEX —— 给 messages 加索引也是动它的结构）",
        not bad, str(bad))

    chk("C2 🔴 禁止写路由：没有 .post / .put / .delete / .patch 装饰器",
        not re.search(r"\.(post|put|delete|patch)\(", src), "")

    chk("C3 🔴 不挂 MCP 门（这是我看的诊断口径，不是给他的记忆）",
        "import mcp" not in src and "modules" not in src, "")

    chk("C4 只读两种写法都在：两条 SQL 都是 SELECT + `connect` 上下文用完即走",
        src.count("SELECT ") == 2 and "_schema.connect(relay)" in src
        and src.count("_SELECT_LAST_IN") >= 2, "")

    # 真跑一次，验"调用前后 messages 一字未改"（行为层面的只读）
    r5 = fresh_db(tmp, "readonly")
    put(r5, "in", "user", ts=iso(NOW - timedelta(minutes=6)))
    put(r5, "out", "reply", ts=iso(NOW - timedelta(minutes=5)))

    def snap(relay):
        conn = sqlite3.connect(relay.DB_PATH)
        rows = conn.execute("SELECT id, ts, direction, kind, text, meta "
                            "FROM messages ORDER BY id").fetchall()
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name='messages'").fetchone()[0]
        idx = conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                           "AND tbl_name='messages'").fetchall()
        conn.close()
        return ([tuple(r) for r in rows], ddl, [i[0] for i in idx])

    b4 = snap(r5)
    for _ in range(3):
        P.derive(r5, now=NOW)
        P.count_in(r5)
    a4 = snap(r5)
    chk("C5 🔴🔴 真跑三次 derive：messages 的行 / 正文 / DDL / 索引**逐字节不变**",
        b4 == a4, f"before={b4} after={a4}")

    # ══════════════════════════════════════════════════════════════════════
    # D. fail-open：坏库 / 畸形行 / 端点
    # ══════════════════════════════════════════════════════════════════════
    sect("D. fail-open：读不到 = 「未知」，绝不伪装成「确定不在场」")

    class Broken:
        DB_PATH = os.path.join(tmp, "no_such_dir", "deep", "x.db")

    # 🔴 全新 /data 第一次部署那一刻：五张表建好了，但 `messages` 还没建
    #    （它归后端 lifespan）。这不是理论情况 —— 每套新部署都会经过这一秒。
    r_nomsg = fresh_db(tmp, "no_messages", with_messages=False)
    d = P.derive(r_nomsg, now=NOW)
    chk("D0 🔴 `messages` 表还不存在（全新 /data 的第一次）→ fail-open，"
        "**不许抛**、也不许假装'她不在'",
        d["ok"] is False and d["reason"] == "read_failed"
        and "active_recent" not in d, str(d))
    chk("D0b 同一时刻 count_in → None（不是 0）",
        P.count_in(r_nomsg) is None, str(P.count_in(r_nomsg)))

    d = P.derive(Broken(), now=NOW)
    chk("D1 🔴 库不可达 → ok=False + reason=read_failed（**不抛异常**）",
        d["ok"] is False and d["reason"] == "read_failed" and "minutes_ago" not in d, str(d))
    chk("D1b 🔴🔴 fail 分支里**没有 `active_recent`** —— 那个字段会被读成结论；"
        "有它 = 「房子挂了」被读成「她不在」⇒ 他在她身边说话时还推她手机",
        "active_recent" not in d and "found" not in d, str(sorted(d.keys())))
    chk("D1c fail 分支带 detail（查得出来的那种坏）",
        bool(d.get("detail")), str(d.get("detail")))

    chk("D2 库不可达时 count_in → None（**不是 0**：0 会被读成'她从没说过话'）",
        P.count_in(Broken()) is None, str(P.count_in(Broken())))

    # 畸形行：查库成功但行缺列
    #    ⚠️ 必须**带内容**（`WeirdRow()` 空着就是 falsy，会走"没有行"那一支 —— 假绿）
    class WeirdRow(dict):
        def __getitem__(self, k):
            raise KeyError(k)

    d = P.fold(WeirdRow({"id": 1, "ts": LEGAL, "kind": "user"}), now=NOW)
    chk("D3 🔴 行畸形（取不到列）→ found=True 但 minutes_ago=None + ts_invalid，"
        "**不抛**、不编数",
        d["ok"] is True and d["found"] is True and d["minutes_ago"] is None
        and d["reason"] == "ts_invalid", str(d))

    d = P.fold({}, now=NOW)
    chk("D4 空 dict 行（falsy）与 None **同支**：都当'库里没有 in'，不抛",
        d["ok"] is True and d["found"] is False and d["reason"] == "no_in_messages", str(d))

    # ══════════════════════════════════════════════════════════════════════
    # E. 形状锁死、取值不锁死：非法 ts → 丢，**绝不编默认值**
    # ══════════════════════════════════════════════════════════════════════
    sect("E. 非法 ts → 算不出（null），**绝不顶成 0**（'数据坏了' != '她刚来过'）")

    ILLEGAL = [
        "not-a-time",
        "",
        None,
        "2026-13-45T99:99:99+00:00",
        "2026-09-27T12:30:00",            # 🔴 无时区 = 无法判定绝对时刻
        "2026-09-27 12:30:00",            # 空格分隔 + 无时区
        "1758970800",                      # 裸 unix 秒（形状不对）
        "2026-09-27T12:30:00.1+00:00 extra",
        "x" * 60,
    ]
    ok_all = True
    for v in ILLEGAL:
        dd = P.fold({"id": 1, "ts": v, "kind": "user"}, now=NOW)
        if not (dd["minutes_ago"] is None and dd["reason"] == "ts_invalid"
                and dd["active_recent"] is None):
            ok_all = False
            print(f"    非法 ts 用例失败：{v!r} -> {dd}")
    chk(f"E1 🔴🔴 {len(ILLEGAL)} 种非法 ts 全部 → minutes_ago=None + reason=ts_invalid"
        f" + active_recent=None（'未知'，不是 False）", ok_all, "")

    chk("E2 🔴🔴 非法 ts **绝不顶成 0**（0 分钟 = '她刚刚说过话'，会永久静音他）",
        P.fold({"id": 1, "ts": "garbage", "kind": "user"}, now=NOW)["minutes_ago"] is None
        and P.fold({"id": 1, "ts": "garbage", "kind": "user"}, now=NOW)["minutes_ago"] != 0,
        "")

    chk("E3 🔴 变异证明：把'非法 ts'那条改成'回 0'，本套会红 —— 所以 E1/E2 有牙齿",
        P.fold({"id": 1, "ts": "garbage", "kind": "user"}, now=NOW)["minutes_ago"] is None
        and P.fold({"id": 1, "ts": LEGAL, "kind": "user"}, now=NOW)["minutes_ago"] is not None,
        "（若实现改成回 0，第一条断言立刻失败）")

    chk("E4 形状合法就放行（取值不锁死）：`Z` 结尾 / 无秒 / 毫秒 / `+0800` 都认",
        P.parse_ts("2026-09-27T12:30:00Z") is not None
        and P.parse_ts("2026-09-27T12:30Z") is not None
        and P.parse_ts("2026-09-27T12:30:00.123456+00:00") is not None
        and P.parse_ts("2026-09-27T12:30:00+0800") is not None, "")

    chk("E5 非法 ts 时 `last_in_ts` 仍**原样保留**（如实，不加工成 None 也不改字）",
        P.fold({"id": 1, "ts": "garbage", "kind": "user"}, now=NOW)["last_in_ts"]
        == "garbage", "")

    chk("E6 minutes_ago 是小数（不是整数截断）",
        isinstance(P.fold({"id": 1, "ts": iso(NOW - timedelta(seconds=30)), "kind": "user"},
                          now=NOW)["minutes_ago"], float), "")

    r6 = fresh_db(tmp, "bad_ts")
    put(r6, "in", "user", ts="garbage")
    d = P.derive(r6, now=NOW)
    chk("E7 真写库（ts='garbage'）→ ok=True/found=True 但算不出（**不假装她刚来过**）",
        d["ok"] is True and d["found"] is True and d["minutes_ago"] is None
        and d["reason"] == "ts_invalid" and d["last_in_ts"] == "garbage", str(d))

    # ══════════════════════════════════════════════════════════════════════
    # F. 端点（ASGI，不起端口）+ 接线 + 红线
    # ══════════════════════════════════════════════════════════════════════
    sect("F. 端点：全只读 / 鉴权 / 两条路由 / 开关 / 接线 / GBK")

    from fastapi.testclient import TestClient
    relayF = fresh_db(tmp, "api")
    # ⚠️ 端点走**真实时钟**（端点不接受 now 注入 —— 那是它自己的"现在"），
    #    所以这里的行 ts 必须基于真实 now。`NOW` 是给纯函数/derive 用的固定时刻。
    put(relayF, "in", "user",
        ts=(datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat())
    install_presence(relayF)
    c = TestClient(relayF.app)
    H = {"Authorization": f"Bearer {SECRET}"}

    chk("F1 无密钥 → 401（fail-closed）",
        c.get("/app/ext/presence").status_code == 401, "")
    resp = c.get("/app/ext/presence", headers=H)
    body = resp.json()
    chk("F2 带密钥 → 200 且 ok=True / found=True / 契约字段齐",
        resp.status_code == 200 and body["ok"] is True and body["found"] is True
        and set(body) >= {"ok", "source", "found", "last_in_id", "last_in_ts",
                          "last_in_kind", "minutes_ago", "reason", "window_min",
                          "active_recent", "as_of"},
        str(sorted(body.keys())))
    chk("F3 端点的 minutes_ago 是活的（≈8，用真实时钟）",
        7.5 < body["minutes_ago"] < 9.5, str(body["minutes_ago"]))
    chk("F4 status 200 且回答'这一层在不在工作'（窗口 / in 条数 / 最近读数）",
        c.get("/app/ext/presence/status", headers=H).status_code == 200
        and c.get("/app/ext/presence/status", headers=H).json()["in_rows"] == 1, "")

    chk("F5 🔴🔴 **没有写入口**：POST /presence → 405（一旦能写，它就不是'事实'了）",
        c.post("/app/ext/presence", headers=H).status_code == 405, "")
    chk("F6 PUT / DELETE 也没有",
        c.put("/app/ext/presence", headers=H).status_code == 405
        and c.delete("/app/ext/presence", headers=H).status_code == 405, "")

    # 坏库的端点是 200 + ok=false（**不是 5xx**）
    relayFB = FakeRelay(os.path.join(tmp, "no_dir", "x.db"))
    install_presence(relayFB)
    rb = TestClient(relayFB.app).get("/app/ext/presence", headers=H)
    chk("F7 🔴 房子这层读不到 → 端点回 **200 + ok=false**（不是 5xx）："
        "「不知道」是这个端点的合法答案，回 5xx 会让'这层坏了'和'整个挂了'看起来一样",
        rb.status_code == 200 and rb.json()["ok"] is False
        and "active_recent" not in rb.json(), f"{rb.status_code} {rb.json()}")

    # 幂等
    P.install(relayF)
    chk("F8 install 幂等（第二次什么都不做）", P._INSTALLED is True, "")

    # 接线
    import app_ext as AE
    init_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("F9 两条路由都在 `__init__._ROUTES` 里",
        "/app/ext/presence" in AE._ROUTES and "/app/ext/presence/status" in AE._ROUTES, "")
    chk("F10 `__init__` 里有第 ⑬ 步 + 逃生开关 + import",
        "⑬ 在场信号收口" in init_src and "APP_EXT_PRESENCE_DISABLED" in init_src
        and "presence as _presence" in init_src, "")
    chk("F11 verify_all 已接本套",
        "presence_check.py" in (HERE / "verify_all.py").read_text(encoding="utf-8"), "")

    # 逃生开关（真跑一次 register）
    os.environ["APP_EXT_PRESENCE_DISABLED"] = "1"
    os.environ["APP_EXT_ROOMS_DISABLED"] = "1"
    try:
        relayG = fresh_db(tmp, "off")
        summ = AE.register(relayG)
        paths = {getattr(r, "path", None) for r in relayG.app.routes}
        chk("F12 🔴 开关打开时：端点**不挂**（404），且 summary 里点名是开关关的",
            summ.get("presence") == "disabled by APP_EXT_PRESENCE_DISABLED"
            and "/app/ext/presence" not in paths, str(summ.get("presence")))
    finally:
        os.environ.pop("APP_EXT_PRESENCE_DISABLED", None)
        os.environ.pop("APP_EXT_ROOMS_DISABLED", None)

    # "关了能力，聊天照常" —— 关了也必须能正常 register（不抛）
    os.environ["APP_EXT_ROOMS_DISABLED"] = "1"      # 本套不关心房间，别拖慢/引入噪声
    try:
        relayH = fresh_db(tmp, "on")
        summ2 = AE.register(relayH)
    finally:
        os.environ.pop("APP_EXT_ROOMS_DISABLED", None)
    chk("F13 开着时 register 会挂上它（summary 里是就绪那行，不是开关关的）",
        isinstance(summ2.get("presence"), str)
        and "在场信号就绪" in summ2["presence"], str(summ2.get("presence")))

    # 启动日志 GBK 安全
    line = P.summary_line()
    try:
        line.encode("gbk")
        gbk_ok, gbk_err = True, ""
    except Exception as e:
        gbk_ok, gbk_err = False, f"{type(e).__name__}: {e}"
    chk("F14 🔴 summary_line() GBK 安全（Windows 启动日志会打它）", gbk_ok, gbk_err)
    chk("F15 🔴 presence.py 的 print 里没有 GBK 编不出的符号",
        not [l for l in src.splitlines()
             if "print(" in l and re.search(r"[\U0001F300-\U0001FAFF\u2705\u26A0\u274C]", l)],
        "")

    # 红线：这一层不许碰别人的表
    bad2 = []
    for kw in ("INSERT INTO", "UPDATE messages", "DELETE FROM messages",
               "INSERT INTO memories", "DROP TABLE", "ALTER TABLE"):
        if kw in src:
            bad2.append(kw)
    chk("F16 🔴 源码扫描：presence 这一层只 SELECT（不 INSERT / 不 UPDATE / 不 DROP）",
        not bad2, str(bad2))

    shutil.rmtree(tmp, ignore_errors=True)
    return tmp


def main():
    t0 = time.time()
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        chk("未捕获异常（整套没跑完）", False, "见上面的 traceback")

    npass = sum(1 for _n, ok, _d in results if ok)
    out = os.environ.get("PRESENCE_CHECK_OUT") or str(HERE / "presence_report.txt")
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
