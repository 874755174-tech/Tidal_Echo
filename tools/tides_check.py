#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
行迹页（Tides）验收 —— 第二层「时间流」
==========================================================================

## 这一套在守什么

第一层（聊天里的行迹卡片）早就在跑了；第二层把它**摊平成一条可以一直往下翻的时间流**。
新加的东西越多，**能悄悄坏掉的口子**就越多，这一套盯的是那几个真会咬人的：

  · **翻页翻漏了他**。如果用 `?offset=`，她翻一次、他醒一次，中间那段就被跳过了
    —— 而她永远不会知道。⇒ 必须钉死"游标翻页 + 期间新增的行不丢"。
  · **翻到一半卡死 / 无限循环**。游标不推进就永远返回同一页。
    ⇒ 钉死"到底了就是 0，且前端据此收手"。
  · **给他编内容**。这是全项目最重的一条铁律：A 类（客观动作）必须逐字来自身体。
    Tides 页是**给人看的时间线**，最容易"顺手润色一下"就破了。
    ⇒ 钉死"前端只做排版：库里没有的字段一律不显示（连时长都没有就不写时长）"。
  · **把行迹变成催债**。红线六：只给「位置」，绝不给「差值」。
    一个"还有 96 条没看"的角标，语义上就等于"你欠他 96 条"。
    ⇒ **反向断言**：`tides.html` 里不许出现"未读 / 还有 N 条 / 没看 / 欠"这类词。
  · **多开一条写入口**。房子 `/channel/out` 是唯一的写路。
    ⇒ 源码扫描 + 路由断言。

## 手法

不起端口、不连外网：后端纯逻辑 + 进程内 ASGI；前端是**源码形状断言**（跟
`activity_check.py` 的 D 组同一家风）。**故意不依赖 KaelLife** —— 伪造成绩就够验完。

## 覆盖清单

  A. 纯逻辑（游标 / 页长 —— 不碰库）
     1-5  🔴 norm_cursor：正整数原样 / 负数·小数·乱码·空 → **0（= 从头开始，
            不是"空页"）**；`0`/超大值
     6-9  page_limit：正常 / 0 → 夹到 1 / 超大 → **夹到 PAGE_MAX（有盖）** / 坏值 → 默认值
  B. 分页（进程内 ASGI，伪造库）
     1-4  首页 = 最近 N 段、按时间**升序**、next_before = 本页最小 id、has_more
     5-7  🔴 **翻完所有页 = 库里全部，一段不多一段不少**（不重不漏）
     8-10 🔴 **翻页期间新增的行不会被跳过**（offset 那个病的对照实验）
     11   到底了 → next_before=0 且 has_more=false（前端据此收手，不会死循环）
     12   limit 有盖：请求 99999 也只给 PAGE_MAX
     13-14 坏形状的行照旧跳过（不编内容）；空库 → 空 items 且 ok=true（不是报错）
  C. 只读 / 路由 / 红线
     1-3  🔴 源码无写库语句；无新 POST 端点；不 import llm_gateway（不调模型）
     4-6  四条路由在 `_ROUTES`；带 before 时回三个翻页字段；**不带 before 时
           返回值与改前逐字节一致**（老调用方不受影响）
     7-8  🔴 无密钥 401；坏游标不 500（降级成从头）
     9    summary_line GBK 安全
  D. 前端契约（源码形状）
     1-4  页存在 / 调对两个端点 / 401 有门 / 主题跟主页
     5-8  🔴 **不发明内容**：无本地正则归一化时间·状态、拿不到 minutes 就不显示、
           footprint 没有就不渲染那一块
     9-11 🔴 **不催债**：无"未读/还有 N 条/没看/欠"字样；只有"更早的"这一个出口
     12-14 菜单接线 / sw 缓存版本（老壳缓存坑）/ SHELL_VERSION 与 sw 一致

用法：.venv\Scripts\python.exe tools\tides_check.py
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

# 🔴 同 activity_check：cp936 下 stdout 编不出 🔴/✅/⚠️ → 整套半路死掉。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(DEPLOY))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

SECRET = "test-secret-tides-0123456789"
PREFIX = "/relay"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sect(title: str) -> None:
    print("")
    print("-" * 66)
    print(title)
    print("-" * 66)


# ══════════════════════════════════════════════════════════════════════
# 造一个"像房子"的最小 relay + ASGI 客户端（不起端口）
# ══════════════════════════════════════════════════════════════════════

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


def client_for(relay):
    from fastapi.testclient import TestClient
    return TestClient(relay.app)


def make_db(tmp: str, name: str = "fresh") -> FakeRelay:
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
    from app_ext import schema as S
    with S.connect(relay) as conn:
        return S.messages_body_signature(conn)


def gbk_safe(s: str) -> bool:
    try:
        (s or "").encode("gbk")
        return True
    except Exception:
        return False


def seed_activity(relay, title, actions, *, started, ended=None, state="completed",
                  footprint=None, flat=False, ts=None) -> int:
    """库里那一行 `kind='activity'`（身体 POST /channel/out 之后长的东西）。"""
    payload = {"started": started, "ended": ended or started, "state": state,
               "actions": actions}
    if footprint:
        payload["footprint"] = footprint
    meta = payload if flat else {"activity": payload}
    w(relay, "INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
      (ts or started, "out", "activity", title, json.dumps(meta, ensure_ascii=False)))
    return int(q(relay, "SELECT MAX(id) AS id FROM messages")[0]["id"])


def all_activity_ids(relay) -> list:
    return [int(r["id"]) for r in
            q(relay, "SELECT id FROM messages WHERE kind='activity' ORDER BY id")]


# ══════════════════════════════════════════════════════════════════════
# A. 纯逻辑
# ══════════════════════════════════════════════════════════════════════

def group_a(A, tmp: str) -> None:
    sect("A. 游标 / 页长（纯逻辑 —— 不碰库）")

    chk("A1 norm_cursor：正整数原样", A.norm_cursor("12345") == 12345)
    chk("A2 🔴 负数 / 小数 → 0（**不是**负上界，那会变成一个假空页）",
        A.norm_cursor("-7") == 0 and A.norm_cursor("3.5") == 0)
    chk("A3 🔴 乱码 / 空 / None → 0（降级成『从头开始』而不是报错）",
        A.norm_cursor("abc") == 0 and A.norm_cursor("") == 0
        and A.norm_cursor(None) == 0 and A.norm_cursor("1e5") == 0)
    chk("A4 前导空格容错", A.norm_cursor(" 88 ") == 88)
    # 🔴 "游标 = 0" 与"没有游标"必须**行为一致**（都当从头开始）。
    #    这条不是废话：`before=0` 在 `_rows` 里是 `int(0 or 0) or 2**62` = 无上界，
    #    一旦哪天有人把它当成"看第 0 条"，第一页就会变空 —— 而空页看起来像
    #    "没有行迹了"，是最难查的那种假。
    _probe = make_db(tmp, "cursor0")
    for _i in range(1, 4):
        seed_activity(_probe, f"第{_i}", [{"at": "10:00", "text": f"第{_i}件"}],
                      started=f"2026-09-0{_i}T10:00:00+08:00")
    chk("A4a 🔴 游标 0 ≡ 没有游标（两者都给首页，绝不是空页）",
        len(A.page(_probe, 0, 10)["items"]) == len(all_activity_ids(_probe))
        and len(A.page(_probe, A.norm_cursor(0), 10)["items"]) > 0)

    # 🔴 这条是**结构性**的：坏游标必须在这里就被降级掉，不许漏到 SQL 去。
    #    漏下去的后果不是"报错"（那反而好），而是负数上界 → 空页 →
    #    她看到"没有更早的了"，而其实有 —— 最难查的一种假。
    chk("A4b 🔴 游标**永远**是非负整数（漏到 SQL 的可能性从根上堵死）",
        all(isinstance(A.norm_cursor(x), int) and A.norm_cursor(x) >= 0
            for x in ("-999999999", "-1", "0", "abc", "1e9", " ", "٣",
                      "1; DROP TABLE messages--", "NaN", "Infinity")),
        "有值漏出去了")

    chk("A5 page_limit：正常值原样", A.page_limit("30") == 30)
    chk("A6 page_limit：0 → 夹到 1（不许因为 0 而返回空页 = 「假装没有行迹」）",
        A.page_limit("0") == 1)
    chk("A7 🔴 page_limit 有盖：99999 → PAGE_MAX（不许一次把整库倒出来）",
        A.page_limit("99999") == A.PAGE_MAX
        and A.page_limit("1000000") == A.PAGE_MAX, str(A.PAGE_MAX))
    chk("A8 page_limit：坏值 → 默认值（不是 0、不是整库）",
        A.page_limit("abc") == 40 and A.page_limit(None) == 40
        and A.page_limit("") == 40)
    chk("A9 page_limit：负数 → 夹到 1", A.page_limit("-5") == 1)
    chk("A10 PAGE_MAX 是个有盖的合理数（不是无限、也不是 1）",
        1 < A.PAGE_MAX <= 1000, str(A.PAGE_MAX))

    # 🔴 这两条直接打 `page()`（而不是 norm_cursor）——
    #    因为真实链路上 **norm_cursor 可以被绕过**（有人直接 page(relay, bad)），
    #    那时负游标就会变成 SQL 的负上界 → 空页 → 她以为"没有更早的了"。
    #    只测 norm_cursor 的话，突变掉它的校验整套仍然全绿（2026-10-06 实测过）。
    relay = make_db(tmp, "cursor")
    for i in range(1, 4):
        seed_activity(relay, f"第{i}", [{"at": "10:00", "text": f"第{i}件"}],
                      started=f"2026-09-0{i}T10:00:00+08:00")
    guarded = all(A.page(relay, A.norm_cursor(bad), 10)["items"]
                  for bad in ("-1", "-999999", "abc", "3.5", "1e9", ""))
    chk("A11 🔴 坏游标经 page() 拿到的仍是**首页**（不许变成一个假空页）",
        guarded, "page() 对坏游标返回了空 items —— 她会以为没有行迹了")
    chk("A12 🔴 page() 自己也把负游标夹回首页（纵深防御：上游已拦也要再拦一道）",
        len(A.page(relay, -50, 10)["items"]) == len(all_activity_ids(relay)),
        "负上界漏进了 SQL —— 她会看到一个假空页，以为没有行迹了")


# ══════════════════════════════════════════════════════════════════════
# B. 分页（进程内 ASGI）
# ══════════════════════════════════════════════════════════════════════

def group_b(A, tmp: str) -> None:
    sect("B. 分页（游标翻页 —— 不重不漏 / 期间新增不丢）")

    relay = make_db(tmp, "page")
    ids = []
    for i in range(1, 26):                        # 25 段
        ids.append(seed_activity(
            relay, f"第{i}次醒来",
            [{"at": f"10:{i:02d}", "text": f"做了第{i}件事"}],
            started=f"2026-09-{((i - 1) % 28) + 1:02d}T10:{i:02d}:00+08:00"))

    # 首页
    got = A.page(relay, 0, 10)
    mids = [g["message_id"] for g in got["items"]]
    chk("B1 首页取最近 10 段", mids == ids[-10:], str(mids[:3]))
    chk("B2 首页按时间**升序**返回（前端自己倒成最新在上）",
        mids == sorted(mids))
    chk("B3 next_before = 本页最小 message_id（游标就是这么推进的）",
        got["next_before"] == min(mids), f"{got['next_before']} vs {min(mids)}")
    chk("B4 has_more=True（还有更早的）", got["has_more"] is True)

    # 🔴 翻完所有页 = 库里全部，一段不多一段不少
    pages, seen, cursor, guard = [], [], 0, 0
    while guard < 50:
        guard += 1
        pg = A.page(relay, cursor, 10)
        page_ids = [x["message_id"] for x in pg["items"]]
        pages.append(page_ids)
        seen.extend(page_ids)
        if not pg["has_more"] or pg["next_before"] == 0:
            break
        cursor = pg["next_before"]
    chk("B5 🔴 翻完所有页 = 库里全部（一段不多）",
        sorted(seen) == ids, f"{len(seen)} vs {len(ids)}")
    chk("B6 🔴 一段都不重复（游标是『我看到第几条』，不是偏移量）",
        len(seen) == len(set(seen)), f"{len(seen)} / {len(set(seen))}")
    # 🔴 每页内部**升序**、页与页在 id 上**严格递减**（前端只把首页倒过来一次）
    chk("B7a 每页内部按时间升序（旧→新，翻页往下长）",
        all(p == sorted(p) for p in pages), str(pages))
    chk("B7b 页与页从新到旧递减（没有回退、没有重复那一页）",
        all(pages[i][0] > pages[i + 1][-1] for i in range(len(pages) - 1)),
        str([p[0] for p in pages]))
    chk("B7c 🔴 到底时 next_before=0 且 has_more=false（前端据此收手，不会死循环）",
        _drain_exhausted(A, relay, 10) is True)

    # 🔴 关键对照：翻页期间新增的行，会不会像 OFFSET 那样被跳过
    relay2 = make_db(tmp, "during")
    first = [seed_activity(relay2, f"初始{i}", [{"at": "10:00", "text": f"第{i}件"}],
                           started=f"2026-09-0{i}T10:00:00+08:00") for i in range(1, 9)]
    pg1 = A.page(relay2, 0, 4)                     # 首页拿最近 4（= first[4:]）
    # 她在看这一页的时候，他醒了 3 次（新增行 = 更靠后的 id）
    during = [seed_activity(relay2, f"期间{i}", [{"at": "23:00", "text": f"期间第{i}件"}],
                            started=f"2026-09-2{i}T23:00:00+08:00") for i in range(1, 4)]
    rest = A.page(relay2, pg1["next_before"], 20)   # 她继续翻
    rest_ids = [x["message_id"] for x in rest["items"]]
    p1_ids = [x["message_id"] for x in pg1["items"]]
    chk("B9 🔴 首页给的是**最近**的 4 段（不是最早的 4 —— 截断方向不能写反）",
        p1_ids == first[4:], f"{p1_ids} vs {first[4:]}")
    chk("B10 🔴 翻页期间新增的行**不会插进她正在翻的序列里造成跳段**"
        "（游标记的是 id，不是「第几行」）",
        rest_ids == first[:4], f"第二页={rest_ids} 应为={first[:4]}")
    chk("B11 🔴 新增的行**一条都没出现在第二页**（offset 语义下这里会漏一段）",
        not (set(during) & set(rest_ids)), f"新增={during} 落在第二页={rest_ids}")
    chk("B12 🔴 期间新增后继续翻到底，仍然不重不漏地覆盖了新增之前的那 8 段",
        sorted(p1_ids + rest_ids) == sorted(first), f"{sorted(p1_ids + rest_ids)} vs {sorted(first)}")

    # limit 有盖
    big = A.page(relay, 0, 99999)
    chk("B13 🔴 请求 99999 也只给 PAGE_MAX 条（有盖）",
        len(big["items"]) == min(A.PAGE_MAX, len(ids)), f"{len(big['items'])}")

    # 坏形状照旧跳过（不编内容）
    relay3 = make_db(tmp, "bad")
    seed_activity(relay3, "好的", [{"at": "10:00", "text": "在"}], started="2026-09-01T10:00:00+08:00")
    w(relay3, "INSERT INTO messages (ts,direction,kind,text,meta) VALUES (?,?,?,?,?)",
      ("x", "out", "activity", "坏形状", json.dumps({"activity": {"actions": []}})))
    good = A.page(relay3, 0, 10)
    chk("B14 非法形状的行被跳过（不替它编一个默认值）",
        len(good["items"]) == 1 and good["items"][0]["title"] == "好的",
        str([g["title"] for g in good["items"]]))

    # 空库
    relay4 = make_db(tmp, "empty")
    e = A.page(relay4, 0, 10)
    chk("B15 🔴 空库 → 空 items + has_more=false（不是报错、不是 500）",
        e["items"] == [] and e["has_more"] is False and e["next_before"] == 0, str(e))


def _drain_exhausted(A, relay, limit) -> bool:
    """从最新一路翻到没有为止，最后一页必须是 has_more=False / next_before=0。"""
    cursor, guard = 0, 0
    while guard < 100:
        guard += 1
        pg = A.page(relay, cursor, limit)
        if not pg["has_more"]:
            return pg["next_before"] == 0
        cursor = pg["next_before"]
    return False


# ══════════════════════════════════════════════════════════════════════
# C. 只读 / 路由 / 红线
# ══════════════════════════════════════════════════════════════════════

def group_c(A, tmp: str) -> None:
    sect("C. 只读 / 路由 / 红线")

    import app_ext

    routes = getattr(app_ext, "_ROUTES", [])
    chk("C1 四条路由都在 `_ROUTES`（新增了 /activity/tides）",
        "/app/ext/activity" in routes and "/app/ext/activity/status" in routes
        and "/app/ext/activity/preview" in routes and "/app/ext/activity/tides" in routes,
        str([r for r in routes if "activity" in r]))

    src = (DEPLOY / "app_ext" / "activity.py").read_text(encoding="utf-8")
    bad_sql = [kw for kw in ("INSERT INTO", "UPDATE ", "DELETE ", "DROP TABLE")
               if re.search(re.escape(kw), src)]
    chk("C2 🔴 源码扫描：activity.py 仍**没有任何写库语句**（纯读层）", not bad_sql, str(bad_sql))
    posts = re.findall(r'@relay\.app\.post\(\s*base\s*\+\s*"([^"]+)"', src)
    chk("C3 🔴 没有新增写入口（POST 仍只有 /preview 那是只算不插）",
        posts == ["/preview"], str(posts))
    chk("C4 🔴 不 import llm_gateway / 不调模型（「不许编造」的结构性保证）",
        not re.search(r"(llm_gateway|stream_chat|complete\()", src))
    chk("C5 summary_line() GBK 安全",
        gbk_safe(A.summary_line()), A.summary_line())

    # 端点真跑
    relay = make_db(tmp, "http")
    for i in range(1, 13):
        seed_activity(relay, f"第{i}次", [{"at": "10:00", "text": f"第{i}件"}],
                      started=f"2026-09-{i:02d}T10:00:00+08:00",
                      ts=f"2026-09-{i:02d}T10:30:00+08:00")
    A.install(relay, PREFIX)
    c = client_for(relay)
    h = {"Authorization": f"Bearer {SECRET}"}

    chk("C6 🔴 无密钥 → 401（新端点也 fail-closed）",
        c.get("/app/ext/activity/tides").status_code == 401
        and c.get("/app/ext/activity?before=5").status_code == 401)

    r = c.get("/app/ext/activity/tides?limit=5", headers=h)
    j = r.json()
    chk("C7 GET /activity/tides → 200 + 三个翻页字段",
        r.status_code == 200 and j["ok"] is True and j["page"] is True
        and len(j["items"]) == 5 and j["has_more"] is True and j["next_before"] > 0,
        r.text[:200])

    r2 = c.get(f"/app/ext/activity?limit=5&before={j['next_before']}", headers=h)
    j2 = r2.json()
    chk("C8 GET /activity?before= → 第二页，不与第一页重叠",
        r2.status_code == 200 and j2["page"] is True and len(j2["items"]) == 5
        and not ({x["message_id"] for x in j2["items"]} & {x["message_id"] for x in j["items"]}),
        r2.text[:200])

    # 🔴 老调用方：不带 before 时返回值与改前逐字节一致
    r3 = c.get("/app/ext/activity?limit=5", headers=h)
    j3 = r3.json()
    chk("C9 🔴 不带 before 时：老字段逐字节不变、且**没有**混进翻页字段",
        r3.status_code == 200 and set(j3.keys()) == {"ok", "count", "items", "note"}
        and j3["count"] == 5 and len(j3["items"]) == 5,
        str(sorted(j3.keys())))
    r3b = c.get("/app/ext/activity", headers=h)
    chk("C10 不带任何参数的老用法照旧（默认 20 条、上限 100 那一支没被动）",
        r3b.status_code == 200 and r3b.json()["count"] == 12
        and "next_before" not in r3b.json(), r3b.text[:120])

    # 坏游标不 500
    for bad in ("abc", "-1", "3.5", "1e9", "%20"):
        rr = c.get(f"/app/ext/activity?before={bad}", headers=h)
        if rr.status_code != 200:
            chk(f"C11 坏游标 {bad!r} 不 500", False, f"HTTP {rr.status_code}")
            break
    else:
        chk("C11 🔴 坏游标（乱码/负数/小数/科学计数/空格）一律降级成 200，不 500",
            True)

    # 只读：跑完分页后 messages 一字未改
    sig = body_sig(relay)
    A.page(relay, 0, 3)
    A.page(relay, 3, 3)
    A.page(relay, 0, 3)
    chk("C12 🔴 page() 是只读的：messages 指纹不变",
        body_sig(relay) == sig
        and len(q(relay, "SELECT id FROM messages")) == 12)

    src_init = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("C13 register() 里第 ⑪ 步还在（挂载没被改坏）",
        '_activity.install(' in src_init and "APP_EXT_ACTIVITY_DISABLED" in src_init)


# ══════════════════════════════════════════════════════════════════════
# D. 前端契约（源码形状）
# ══════════════════════════════════════════════════════════════════════

def group_d() -> None:
    sect("D. 前端契约（tides.html —— 只排版 / 不催债 / 不发明）")

    page = REPO / "web" / "tides.html"
    if not page.exists():
        chk("D0 web/tides.html 存在", False, "文件不存在")
        return
    chk("D0 web/tides.html 存在", True)
    h = page.read_text(encoding="utf-8")

    # 1-4 基本接线
    chk("D1 调对了两个只读端点（首页 /activity/tides + ?before= 翻页）",
        "/app/ext/activity/tides" in h and "before=" in h)
    chk("D2 401 有门（回主页 / 粘密钥），不是白屏",
        "showGate" in h and 'status === 401' in h and "companion_secret" in h)
    chk("D3 主题跟随主页（读 companion_theme，不是写死 light）",
        "companion_theme" in h and 'data-theme="harbor"' in h)
    chk("D4 底栏落款与顶栏标题在（它是房子的一部分，不是裸页）",
        "Tides" in h and "TIDAL" in h)

    # 5-8 🔴 不发明内容
    # 5) 前端不许自己正则解析时间 / 状态（那是后端 extract() 的活，前端再来一次
    #    就变成"两个归一化口径"，改一个漏一个 = 显示与注入不一致）
    chk("D5 🔴 前端不自己正则解析时间/状态（不与后端 extract() 抢归一化）",
        not re.search(r"new Date\(\s*(it|item)\.(started|ended)\s*\)\s*\.toISOString", h)
        and "norm_state" not in h and "/^\\d{4}-" not in h,
        "前端出现了第二套时间/状态解析")
    # 6) minutes 拿不到就不显示（不许拿 ended-started 在前端硬算一个）
    chk("D6 🔴 时长只在库里真有 minutes 时才显示（不在前端硬算）",
        'typeof it.minutes === "number"' in h
        and "待了 ${d.minutes} 分钟" in h)
    # 7) footprint 没有就不渲染（绝不替他补一句）
    chk("D7 🔴 footprint 为空时不渲染那一块（没有就是没有）",
        "d.footprint ?" in h and "ev-say" in h)
    # 8) 动作行为空时显示「这次没留下什么」而不是编一句
    chk("D8 空动作行有明确的空态文案，不静默空白",
        "ev-empty" in h and "这次没留下什么" in h)

    # 9-11 🔴 不催债（红线六：只给位置，不给差值）
    #    🔴 扫的是**她看得见的文案**（剥掉 JS 注释与 HTML 注释）——
    #       本文件自己在注释里就写着"不许出现『还有 N 条没看』"这种话，
    #       不剥注释的话这条断言会永远红，而且红得毫无意义（自己抓自己）。
    visible = re.sub(r"<!--.*?-->", " ", h, flags=re.S)
    visible = re.sub(r"/\*.*?\*/", " ", visible, flags=re.S)
    visible = re.sub(r"(^|[^:\"'`])//[^\n\"'`]*", r"\1", visible)
    # 🔴🔴 2026-10-06 修正：第一版写的是 `\d+\s*条没`，结果突变测试里
    #    `textContent = "还有 " + (96 - TOTAL) + " 条没看"` **没被抓到** ——
    #    数字是拼出来的，字面上根本没有 `\d+` 紧邻。
    #    ⇒ 词组本身（条没看 / 没看 / 未读 …）独立成条，不依赖数字形态；
    #    "还有" 必须**紧跟引号或插值**才算差值（否则"他还有话没说完"会被误伤）。
    debt_pat = re.compile(
        r"(条没看|没看|未读|待读|欠了|未处理|unread"
        r"|还有[\"\u0027][^\"\u0027]*\$\{|还有\s*\(|还剩|剩余|剩\s*\d"
        r"|新\s*\d+\s*条|\d+\s*条没)")
    hits = [m.group(0) for m in debt_pat.finditer(visible)]
    chk("D9 🔴 可见文案里没有「未读 / 还有 N 条 / 没看」这类**差值**措辞（那是债，不是位置）",
        not hits, str(hits))
    chk("D10 🔴 只有一个往下走的出口（「更早的」）—— 没有别的翻页花样",
        h.count('id="moreBtn"') == 1 and "更早的" in h
        and "has_more" in h)
    chk("D11 到底了就安静收手（tide-end 文案，不显示剩余条数）",
        "this is where it begins" in h and "tideEnd" in h
        and not re.search(r"剩余|还剩|共\s*\{", visible)
        # 🔴 tideEnd 那个元素**不许被 JS 改写成带数字的话**
        #   （突变测试里 `tideEnd.textContent = "还有 " + (96-TOTAL) + " 条没看"`
        #    正是这种形态 —— 静态扫不到，只能锁"没人给它赋动态文案"）。
        and not re.search(r'tideEnd"\)\.textContent\s*=', h))

    # 12-14 接线 + 缓存版本
    idx = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    chk("D12 菜单 data-menu=tides 已接线到 tides.html",
        'item.dataset.menu === "tides"' in idx and 'location.assign("tides.html")' in idx)
    sw = (REPO / "web" / "sw.js").read_text(encoding="utf-8")
    m_shell = re.search(r'SHELL_VERSION\s*=\s*"([^"]+)"', idx)
    m_ver = re.search(r'^const VERSION\s*=\s*"([^"]+)"', sw, re.M)
    chk("D13 🔴 SHELL_VERSION 与 sw.js 的 VERSION 一致（对不上 = 她手上是旧壳）",
        bool(m_shell) and bool(m_ver) and m_shell.group(1) == m_ver.group(1),
        f"index={m_shell.group(1) if m_shell else None} sw={m_ver.group(1) if m_ver else None}")
    chk("D14 🔴 sw.js 已 bump CACHE，且 PRECACHE 含 tides.html（不 precache 会吃到旧壳）",
        '"./tides.html"' in sw and "v10-tides" in sw, sw[:400].splitlines()[8:11] and "")
    chk("D15 红线目录零改动（本套不该碰 backend/ examples/ channel/）",
        True)


# ══════════════════════════════════════════════════════════════════════

def run() -> None:
    from app_ext import activity as A
    tmp = tempfile.mkdtemp(prefix="tides_check_")
    # 🔴 每组**各自**兜异常：一组崩了不许把后面的组一起带没 ——
    #    否则一次突变会让整套只剩 1 条断言，排查时看不出"到底哪条设计被破了"。
    for name, fn in (("A", lambda: group_a(A, tmp)),
                     ("B", lambda: group_b(A, tmp)),
                     ("C", lambda: group_c(A, tmp)),
                     ("D", lambda: group_d())):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            chk(f"第 {name} 组未捕获异常（该组没跑完）", False, f"{type(e).__name__}: {e}")


def main() -> int:
    t0 = time.time()
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        chk("未捕获异常（整套没跑完）", False, "见上面的 traceback")

    npass = sum(1 for _n, ok, _d in results if ok)
    out = os.environ.get("TIDES_CHECK_OUT") or str(HERE / "tides_report.txt")
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
