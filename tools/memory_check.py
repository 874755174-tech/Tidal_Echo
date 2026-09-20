#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
记忆层验收 —— P2 ⑩-a（`memories` 表 + `source` 缝 + 写入路径）
==========================================================================

## 这一套到底在守什么

⑩-a 是**留缝**，不是盖房。而"缝"这种东西的价值**全在将来**：
今天它看起来只是"表上多了一列"，所以它最容易出的病不是"功能不对"，
而是**将来才发现缝没焊上**——

  · **缝焊在了错的形状上**：给列写 `DEFAULT 'chat'`，于是`ALTER` 之后
    **老行也报 `'chat'`** → 把「不知道从哪来」静默洗成「来自对话」。
    （2026-09-20 冒烟真踩到，见 `schema.py` 那段注释。）
  · **"不锁值"写成了"锁值"**：照抄 `kind` 那套"不在枚举里就归类"，
    把一条阅读记忆**静默标成对话记忆** —— 那不是校验，是数据损坏。
  · **迁移把别的表碰了**：`memories` 加列的同一段代码顺手动了 `messages`。
  · **内部权重漏出去**：`salience` 是内部权重（跟"想念度数值不做"同源），
    一旦出现在任何响应里，红线就破了。
  · **顺手给了 delete**：`memories_store.py` 的头号设计决定就是
    **让"删记忆"在代码层面不存在** —— 加一个 `delete()` 等于把这条作废。

## 手法：真跑迁移 + 真走端点（不起 HTTP 服务，用 ASGI TestClient 进进程）

不起真端口，所以**不跟别的套抢端口**，也不用等启动。要验的是
"老库能不能升上来 / 端点的形状对不对"，这两件事在进程内都能验实。

## 覆盖清单

  A. store 层（纯逻辑，不碰 HTTP）
     1-3    add 成功 / id 形状 / 落库能读回
     4-6    🔴 不传 source → `chat`（默认值住在**代码**里）；传 reading 就是 reading
     7-10   🔴 形状**不合法**才降级（大小写/空白只是归一化、不算降级）；
            降级时**如实报** `source_coerced` + `source_raw`
     11-13  🔴 形状合法但**未知**的值**原样存**（`xinchao-nian`）——
            这是"缝"的全部意义，照抄 `kind` 的封闭写法就把缝堵死了
     14     kind 不在枚举 → fact
     15-16  空 text → ok False 且**没写进去**；salience 夹进 0..1
     17-18  🔴 store 里**没有 delete**（源码扫描 + hasattr 双查）
     19-21  list/top/source 过滤；🔴 `source="chat"` **不把 NULL 老行捞进来**
     22-23  touch 真改 last_used；不存在的 id → not_found（不抛）
     24-26  stats 两个维度；🔴 NULL 归 `(unknown)`，**不并进 chat**
     27-28  🔴 `public()` 白名单投影**不含 salience**；`as_extra_for_prompt` 只留 kind/text
     29-30  老行（迁移前的）读回 `source = None`（不是 `'chat'`）

  B. 迁移 v3 → v4（**真造一个 v3 老库来升**）
     1-2   老库确实是 v3、memories 确实没有 source 列（先证明起点是对的）
     3-4   ensure_schema 后 version = 4，`migrated` 里点名 `memories.source`
     5-8   🔴 老行还在 / 正文一个字没改 / 新列是 **NULL** / `source_msg` 也没被碰
     9-11  🔴 `messages` DDL 逐字未变 + 行数未变（红线断言真跑了）
     12-13 幂等：再跑一次 migrated 为空、version 仍 4
     14-19 全新库就有 source 列；🔴 新老两库的列**集合**一致；🔴 列**顺序**必然不同
           （`ALTER ADD COLUMN` 只追加在末尾）→ 另证**代码不依赖列顺序**；
           🔴 列上**没有 DEFAULT**（带 DEFAULT = 老行报出默认值 = 伪造来源）

  C. 端点（真走 ASGI，含鉴权）
     1-4   无密钥 → 401；带密钥 GET/POST/stats → 200/201
     5-8   🔴 POST 与 GET 的响应里**一个 salience 都不许有**（字符串级比对）
     9-10  写进去的 source 读回来一致；`?source=` 过滤真生效
     11-12 `?limit=` 夹紧（0 → 1；99999 → 500）
     13-16 空 text / 超长 text / 坏 JSON / 坏 salience / 坏 source_msg → 400 + 明确 reason
     17-18 🔴 形状非法经端点 → 201 + `source_coerced`；未知合法 → 原样进库
     19-20 🔴 **没有 DELETE / PUT 路由**（405；让"删记忆"在 HTTP 面上也不存在）

  D. 红线 / 接线 / 开关
     1-2  两条路由在 `_ROUTES`；register() 里有第 ⑩ 步
     3     🔴 源码扫描：扩展层对 `memories` **没有任何 DELETE / DROP / ALTER**
     4     🔴 `memory.py` **不 import mcp**、不注册 MCP 工具（它不是房间）
     5-6   🔴 关掉开关的房子：端点 404，且**表与缝还在**（能力没了 ≠ 数据没了）
     7     schema 版本 = 4
     8     `summary_line()` 与启动日志那行 **GBK 安全**
     9     verify_all 已接本套

用法：.venv\Scripts\python.exe tools\memory_check.py
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

SECRET = "test-secret-memory-0123456789"
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
    """只带 `check_auth` / `DB_PATH` / `SECRET` / `app` 的替身。

    `check_auth` 照抄 `backend/app.py` 的语义（Bearer 或 `?token=`），
    这样"鉴权真被调到了"这件事才有意义。
    """

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


def make_db(tmp: str, name: str = "fresh"):
    """全新库：走房子的正常初始化（建表 + 播种房主）。"""
    from app_ext import schema as S, identity as I
    relay = FakeRelay(os.path.join(tmp, name + ".db"))
    S.ensure_schema(relay)
    I.ensure_owner(relay)
    return relay


def make_old_v3_db(tmp: str):
    """造一个**真的 v3 老库**：memories 没有 source 列 + 一条老记忆 + 两条消息。

    v3 的定义：settings 有 provider_id、sessions 有 summary_upto、
    memories **没有** source。前两张表直接用现在的 DDL 就是 v3 形状，
    只有 memories 要把 source 那一行摘掉。
    """
    from app_ext import schema as S
    db = os.path.join(tmp, "old_v3.db")
    conn = sqlite3.connect(db)
    tables = dict(S.DDL_TABLES)
    for name in ("users", "settings", "sessions"):
        conn.execute(tables[name])

    mem_ddl = "\n".join(
        ln for ln in tables["memories"].splitlines()
        if not ln.strip().startswith("source ")      # 只摘 `source     TEXT,`，不动 source_msg
    )
    conn.execute(mem_ddl)
    conn.execute(MESSAGES_DDL)

    conn.execute("INSERT INTO users (id, handle, display_name, secret_hash, created) "
                 "VALUES ('u_owner','owner','Lily','x','2026-09-01T00:00:00+00:00')")
    conn.execute("INSERT INTO memories "
                 "(id,user_id,kind,text,source_msg,salience,created,last_used) "
                 "VALUES ('m_legacy','u_owner','fact','迁移之前就存在的一条记忆',4242,0.88,"
                 "'2026-09-10T00:00:00+00:00',NULL)")
    conn.execute("INSERT INTO messages (ts,direction,kind,text) VALUES "
                 "('2026-09-10T00:00:00+08:00','in','user','老消息一')")
    conn.execute("INSERT INTO messages (ts,direction,kind,text) VALUES "
                 "('2026-09-10T00:00:01+08:00','out','reply','老消息二')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    return FakeRelay(db)


def snapshot(relay) -> dict:
    """库快照：表清单 / messages 的 DDL 与行数 / memories 的列 / user_version。"""
    conn = sqlite3.connect(relay.DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {
        "tables": sorted(r["name"] for r in
                         conn.execute("SELECT name FROM sqlite_master WHERE type='table'")),
        "version": int(conn.execute("PRAGMA user_version").fetchone()[0] or 0),
        "mem_cols": [(r["name"], r["type"], r["dflt_value"])
                     for r in conn.execute("PRAGMA table_info(memories)")],
        "mem_ddl": dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()).get("memories", ""),
    }
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()
    out["msg_ddl"] = (row["sql"] if row else "")
    try:
        out["msg_rows"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except Exception:
        out["msg_rows"] = None
    conn.close()
    return out


def q(relay, sql, args=()):
    """只读取数。"""
    conn = sqlite3.connect(relay.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def w(relay, sql, args=()):
    """写一句并**提交**。

    🔴 用 `q()` 写是错的：那条连接在 finally 里就关了，**没 commit = 回滚**
        —— "插进去了又查不到"（2026-09-20 本脚本自己踩过，B17 假红）。
    """
    conn = sqlite3.connect(relay.DB_PATH)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════

def run():
    from app_ext import schema as S, memories_store as M, memory as MEM
    # ⚠️ 不能写 `from app_ext import __init__ as AE` —— 包上的 `__init__`
    #    是模块那个特方法（method-wrapper），不是 `__init__.py` 这个模块。
    #    `app_ext/__init__.py` 就是模块 `app_ext` 本身，直接 import 它。
    import app_ext as AE

    tmp = tempfile.mkdtemp(prefix="kael-memcheck-")

    # ══════════════════════════════════════════════════════════════════
    sect("A. store 层（纯逻辑）")
    # ══════════════════════════════════════════════════════════════════
    fresh = make_db(tmp, "store")

    r1 = M.add(fresh, "fact", "她喜欢盐系手札风", source="reading")
    chk("A1  add 返回 ok", r1.get("ok") is True, str(r1))
    chk("A2  id 形状 m_xxxx", bool(re.match(r"^m_[0-9a-f]{12}$", r1.get("id") or "")), str(r1.get("id")))
    chk("A3  落库能读回（正文一致）",
        (M.list_recent(fresh, 5) or [{}])[0].get("text") == "她喜欢盐系手札风")

    r2 = M.add(fresh, "preference", "不传 source 的默认")
    chk("A4  🔴 不传 source → chat（默认值住在代码里）", r2.get("source") == "chat", str(r2))
    r3 = M.add(fresh, "event", "显式 reading", source="reading")
    chk("A5  显式 source 落库一致", r3.get("source") == "reading", str(r3))
    chk("A6  库里那一行的 source 真是 reading",
        [x for x in q(fresh, "SELECT source FROM memories WHERE id=?", (r3["id"],))][0]["source"] == "reading")

    r4 = M.add(fresh, "fact", "大小写与空白", source="  Reading  ")
    chk("A7  大小写/空白 → 归一化，且**不算降级**（不给噪音）",
        r4.get("source") == "reading" and not r4.get("source_coerced"), str(r4))
    r5 = M.add(fresh, "fact", "带空格的非法值", source="Reading Book!!")
    chk("A8  🔴 形状非法 → chat", r5.get("source") == "chat", str(r5))
    chk("A9  🔴 降级**如实报**（source_coerced + source_raw 是原值）",
        r5.get("source_coerced") is True and r5.get("source_raw") == "Reading Book!!", str(r5))
    r6 = M.add(fresh, "fact", "超长形状", source="a" * 30)
    chk("A10 超长（>24）→ chat + 报降级",
        r6.get("source") == "chat" and r6.get("source_coerced") is True, str(r6))
    r7 = M.add(fresh, "fact", "空字符串跟 None 等价", source="")
    chk("A11 空串 → chat（报降级）",
        r7.get("source") == "chat" and r7.get("source_coerced") is True, str(r7))

    r8 = M.add(fresh, "reading", "未知但合法的来源", source="xinchao-nian")
    chk("A12 🔴 形状合法但未知 → **原样存**（缝的意义）", r8.get("source") == "xinchao-nian", str(r8))
    chk("A13 且**没有**被报成降级", not r8.get("source_coerced"), str(r8))

    r9 = M.add(fresh, "不存在的kind", "kind 兜底")
    chk("A14 kind 不在枚举 → fact", r9.get("kind") == "fact", str(r9))

    n_before = len(q(fresh, "SELECT 1 FROM memories"))
    r10 = M.add(fresh, "fact", "   ")
    n_after = len(q(fresh, "SELECT 1 FROM memories"))
    chk("A15 空 text → ok False 且 reason=empty_text",
        r10.get("ok") is False and r10.get("reason") == "empty_text", str(r10))
    chk("A16 且**一行都没写进去**", n_before == n_after, f"{n_before} → {n_after}")

    r11 = M.add(fresh, "fact", "salience 夹紧", salience=5.0)
    r12 = M.add(fresh, "fact", "salience 夹紧下界", salience=-3)
    sal_up = q(fresh, "SELECT salience FROM memories WHERE id=?", (r11["id"],))[0]["salience"]
    sal_dn = q(fresh, "SELECT salience FROM memories WHERE id=?", (r12["id"],))[0]["salience"]
    chk("A17 salience 夹进 0..1（上）", sal_up == 1.0, str(sal_up))
    chk("A18 salience 夹进 0..1（下）", sal_dn == 0.0, str(sal_dn))

    store_src = (DEPLOY / "app_ext" / "memories_store.py").read_text(encoding="utf-8")
    chk("A19 🔴 store 里**没有 delete 函数**（让'删记忆'在代码层面不存在）",
        not re.search(r"^def\s+(delete|remove|drop)\w*\s*\(", store_src, re.M))
    chk("A20 🔴 且模块上也没有这些属性",
        not [x for x in ("delete", "delete_memory", "remove") if hasattr(M, x)])

    lr = M.list_recent(fresh, 50)
    chk("A21 list_recent 默认全取（回得比 0 多）", len(lr) >= 8, str(len(lr)))
    only_reading = M.list_recent(fresh, 50, source="reading")
    chk("A22 ?source=reading 只回 reading",
        bool(only_reading) and all(x["source"] == "reading" for x in only_reading),
        str(sorted({x["source"] for x in only_reading})))

    # 老行（NULL）+ source="chat" 过滤：不许把 NULL 捞进来
    conn = sqlite3.connect(fresh.DB_PATH)
    conn.execute("INSERT INTO memories (id,user_id,kind,text,salience,created) "
                 "VALUES ('m_null','u_owner','fact','没标来源的老行',0.5,'2026-09-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    chat_only = M.list_recent(fresh, 50, source="chat")
    chk("A23 🔴 source='chat' **不把 NULL 老行捞进来**（兜底=洗白）",
        all(x["source"] == "chat" for x in chat_only) and
        "m_null" not in {x["id"] for x in chat_only},
        str(sorted({x["source"] for x in chat_only})))
    null_row = [x for x in M.list_recent(fresh, 50) if x["id"] == "m_null"]
    chk("A24 老行读回 source = **None**（不是 'chat'）",
        bool(null_row) and null_row[0]["source"] is None, str(null_row[:1]))

    t = M.touch(fresh, r1["id"])
    chk("A25 touch 真改了 last_used",
        t.get("ok") is True and q(fresh, "SELECT last_used FROM memories WHERE id=?",
                                  (r1["id"],))[0]["last_used"] is not None, str(t))
    chk("A26 touch 不存在的 id → not_found（不抛）",
        M.touch(fresh, "m_不存在").get("reason") == "not_found")

    st = M.stats(fresh)
    chk("A27 stats 有 by_kind 与 by_source 两维",
        isinstance(st.get("by_kind"), dict) and isinstance(st.get("by_source"), dict), str(st))
    chk("A28 🔴 NULL 归 '(unknown)'，**不并进 chat**",
        st["by_source"].get("(unknown)") == 1 and st["by_source"].get("chat", 0) >= 1, str(st["by_source"]))

    pub = M.public(M.list_recent(fresh, 1)[0])
    chk("A29 🔴 public() 投影**不含 salience**",
        "salience" not in pub and "user_id" not in pub, str(sorted(pub)))
    extra = M.as_extra_for_prompt(fresh, 3)
    keys = set((extra.get("items") or [{}])[0].keys())
    chk("A30 🔴 as_extra_for_prompt 只留 kind/text（不带 salience、不带 source）",
        bool(keys) and keys == {"kind", "text"},
        f"keys={sorted(keys)}（注意：**别拿整段 JSON 做子串比对** —— "
        f"测试数据自己的正文里就可能有这个词，那是假红）")

    # ══════════════════════════════════════════════════════════════════
    sect("B. 迁移 v3 → v4（真造一个 v3 老库来升）")
    # ══════════════════════════════════════════════════════════════════
    old = make_old_v3_db(tmp)
    before = snapshot(old)
    old_cols = [c[0] for c in before["mem_cols"]]
    chk("B1  起点确实是个 v3 库", before["version"] == 3, str(before["version"]))
    chk("B2  起点 memories 确实**没有** source 列（但**有** source_msg）",
        "source" not in old_cols and "source_msg" in old_cols, str(old_cols))
    legacy_text_before = q(old, "SELECT text FROM memories WHERE id='m_legacy'")[0]["text"]

    rep = S.ensure_schema(old)
    after = snapshot(old)
    new_cols = [c[0] for c in after["mem_cols"]]
    chk("B3  升完 version = 4", after["version"] == 4, str(after["version"]))
    chk("B4  migrated 里点名 memories.source",
        "memories.source" in (rep.get("migrated") or []), str(rep.get("migrated")))

    legacy = q(old, "SELECT * FROM memories WHERE id='m_legacy'")[0]
    chk("B5  🔴 老行还在（行数没变、id 没变）", len(q(old, "SELECT 1 FROM memories")) == 1)
    chk("B6  🔴 老行正文一个字没改", legacy["text"] == legacy_text_before,
        f"{legacy_text_before!r} → {legacy['text']!r}")
    chk("B7  🔴 老行的新列是 **NULL**（不是 'chat' —— 不伪造来源）",
        legacy["source"] is None, repr(legacy["source"]))
    chk("B8  🔴 老行的 source_msg 也没被碰", legacy["source_msg"] == 4242, str(legacy["source_msg"]))

    chk("B9  🔴 messages 的 DDL 逐字未变",
        after["msg_ddl"] == before["msg_ddl"],
        "DDL 被改了" if after["msg_ddl"] != before["msg_ddl"] else "")
    chk("B10 🔴 messages 行数未变（2 → 2）",
        before["msg_rows"] == after["msg_rows"] == 2,
        f"{before['msg_rows']} → {after['msg_rows']}")
    chk("B11 红线报告的 messages_untouched = True",
        rep.get("messages_untouched") is True, str(rep.get("messages_untouched")))

    rep2 = S.ensure_schema(old)
    after2 = snapshot(old)
    chk("B12 幂等：第二次 migrated 为空", not (rep2.get("migrated") or []), str(rep2.get("migrated")))
    chk("B13 幂等：version 仍 4、列清单不变",
        after2["version"] == 4 and after2["mem_cols"] == after["mem_cols"])

    fresh2 = make_db(tmp, "brandnew")
    fc = snapshot(fresh2)
    chk("B14 全新库建出来就有 source 列",
        "source" in [c[0] for c in fc["mem_cols"]], str([c[0] for c in fc["mem_cols"]]))
    chk("B15 🔴 新库列清单 == 老库迁移后的列清单（**按集合**比，见 B16）",
        set(map(tuple, fc["mem_cols"])) == set(map(tuple, after["mem_cols"])),
        f"\n    新库: {fc['mem_cols']}\n    老库: {after['mem_cols']}")
    # ⚠️ 列**顺序**必然不同：`ALTER TABLE ADD COLUMN` 只会把列**追加在末尾**，
    #    而新库是照 DDL 的位置建的。这不是 bug，是 SQLite 的既定行为 ——
    #    所以要比**集合**，并且**额外证明代码不依赖顺序**（B16 / B17）。
    order_differs = [c[0] for c in fc["mem_cols"]] != [c[0] for c in after["mem_cols"]]
    chk("B16 列顺序不同这件事**被记录下来了**（ALTER 必然追加在末尾）",
        order_differs, "两个库列顺序竟然一样 —— 那反而说明这条注释该删了")
    mem_src_now = (DEPLOY / "app_ext" / "memories_store.py").read_text(encoding="utf-8")
    # ⚠️ 正则要容忍源码里的字符串折行：`"INSERT INTO memories "` 换行再
    #    `"(id, user_id, …)"` —— 中间夹着**引号**，所以 `\s*\(` 匹配不到。
    chk("B17 🔴 代码**不依赖列顺序**：INSERT 一律带列名、`SELECT *` 的结果按 dict 取",
        bool(re.search(r"INSERT INTO memories[\s\"']*\(", mem_src_now))
        and not re.search(r"INSERT INTO memories[\s\"']*VALUES", mem_src_now, re.I)
        and "dict(r)" in mem_src_now,
        "存在不带列名的 INSERT 或按位置解包的读法")
    chk("B18 🔴 那一列上**没有 DEFAULT**（带 DEFAULT 会让老行报出默认值 = 伪造来源）",
        all(c[2] is None for c in after["mem_cols"] if c[0] == "source"),
        str([c for c in after["mem_cols"] if c[0] == "source"]))
    w(fresh2, "INSERT INTO memories (id,user_id,kind,text,salience,created) "
              "VALUES ('m_manual','u_owner','fact','手工插的',0.5,'x')")
    chk("B19 手工 INSERT 省略 source → NULL（跟老行同一个语义）",
        q(fresh2, "SELECT source FROM memories WHERE id='m_manual'")[0]["source"] is None)

    # ══════════════════════════════════════════════════════════════════
    sect("C. 端点（真走 ASGI）")
    # ══════════════════════════════════════════════════════════════════
    web = make_db(tmp, "web")
    MEM._INSTALLED = False          # 同进程里重新挂一次（正常只在 register() 里挂）
    MEM.install(web, PREFIX)
    c = client_for(web)
    H = {"Authorization": f"Bearer {SECRET}"}
    B = "/app/ext/memories"

    chk("C1  无密钥 GET → 401", c.get(B).status_code == 401, str(c.get(B).status_code))
    chk("C2  无密钥 POST → 401", c.post(B, json={"text": "x"}).status_code == 401)
    chk("C3  带密钥 GET → 200", c.get(B, headers=H).status_code == 200)
    chk("C4  带密钥 stats → 200", c.get(B + "/stats", headers=H).status_code == 200)

    p = c.post(B, headers=H, json={"kind": "fact", "text": "从端点写一条",
                                   "source": "reading", "salience": 0.99})
    chk("C5  POST → 201 且 ok",
        p.status_code == 201 and p.json().get("ok") is True, str(p.status_code) + " " + p.text[:120])
    chk("C6  POST 回带出的 item.source 正确",
        p.json().get("item", {}).get("source") == "reading", p.text[:160])
    chk("C7  🔴 POST 响应里**没有 salience 这个词**（字符串级）",
        "salience" not in p.text, p.text[:200])

    g = c.get(B, headers=H)
    chk("C8  🔴 GET 响应里**没有 salience**（字符串级）",
        "salience" not in g.text, g.text[:200])
    chk("C9  🔴 stats 响应里**没有 salience**",
        "salience" not in c.get(B + "/stats", headers=H).text)
    chk("C10 🔴 连 'user_id' 也不回（白名单投影的旁证）",
        "user_id" not in g.text, g.text[:160])

    n_web = g.json()["count"]
    chk("C11 写进去的读得回来（条数对上）", n_web >= 1, str(n_web))
    only_rd = c.get(B + "?source=reading", headers=H).json()
    chk("C12 ?source=reading 过滤生效",
        only_rd["count"] == 1 and only_rd["items"][0]["text"] == "从端点写一条", str(only_rd))

    lim0 = c.get(B + "?limit=0", headers=H).json()
    lim_big = c.get(B + "?limit=99999", headers=H).json()
    chk("C13 ?limit=0 夹到 1", lim0["filter"]["limit"] == 1, str(lim0["filter"]))
    chk("C14 ?limit=99999 夹到 500", lim_big["filter"]["limit"] == 500, str(lim_big["filter"]))

    chk("C15 空 text → 400 empty_text",
        (lambda r: r.status_code == 400 and r.json().get("reason") == "empty_text")(
            c.post(B, headers=H, json={"text": "   "})),
        c.post(B, headers=H, json={"text": "   "}).text[:120])
    long_txt = "长" * 2001
    rl = c.post(B, headers=H, json={"text": long_txt})
    chk("C16 超长 text → 400 text_too_long",
        rl.status_code == 400 and rl.json().get("reason") == "text_too_long", rl.text[:140])
    chk("C17 坏 JSON → 400 bad_json",
        c.post(B, headers={**H, "Content-Type": "application/json"},
               content=b"{not json").json().get("reason") == "bad_json")
    chk("C18 坏 salience → 400 bad_salience",
        c.post(B, headers=H, json={"text": "x", "salience": "很多"}).json().get("reason") == "bad_salience")
    chk("C19 坏 source_msg → 400 bad_source_msg",
        c.post(B, headers=H, json={"text": "x", "source_msg": "abc"}).json().get("reason") == "bad_source_msg")

    pc = c.post(B, headers=H, json={"text": "端点上的非法形状", "source": "Not Valid!"})
    chk("C20 🔴 形状非法经端点 → 201 + source_coerced 说明",
        pc.status_code == 201 and pc.json().get("source_coerced") is True
        and pc.json().get("item", {}).get("source") == "chat",
        pc.text[:200])
    c.post(B, headers=H, json={"text": "端点上的新来源", "source": "reading_v2"})
    chk("C21 🔴 未知但合法经端点 → 原样进库（缝从端点也通）",
        q(web, "SELECT source FROM memories WHERE text='端点上的新来源'")[0]["source"] == "reading_v2")

    chk("C22 🔴 没有 DELETE 路由（405）", c.delete(B, headers=H).status_code == 405,
        str(c.delete(B, headers=H).status_code))
    chk("C23 🔴 没有 PUT 路由（405）", c.put(B, headers=H, json={}).status_code == 405,
        str(c.put(B, headers=H, json={}).status_code))

    # ══════════════════════════════════════════════════════════════════
    sect("D. 红线 / 接线 / 开关")
    # ══════════════════════════════════════════════════════════════════
    chk("D1  _ROUTES 里有 /app/ext/memories",
        "/app/ext/memories" in AE._ROUTES, str([r for r in AE._ROUTES if "memor" in r]))
    chk("D2  _ROUTES 里有 /app/ext/memories/stats",
        "/app/ext/memories/stats" in AE._ROUTES)
    ae_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("D3  register() 里有第 ⑩ 步（memory.install + 开关）",
        "memory.install" in ae_src and "APP_EXT_MEMORY_DISABLED" in ae_src)

    mem_src = (DEPLOY / "app_ext" / "memory.py").read_text(encoding="utf-8")
    chk("D4  🔴 memory.py 对 memories **没有任何 DELETE / DROP / ALTER**",
        not re.search(r"\b(DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE)\b", mem_src, re.I))
    chk("D5  🔴 memory.py **不 import mcp**、不注册 MCP 工具（它不是房间）",
        "import mcp" not in mem_src and "_mcp" not in mem_src
        and "register_tool" not in mem_src and "modules" not in mem_src)
    chk("D6  🔴 整个扩展层没有 delete_memory 这类函数（红线是结构性的）",
        not re.search(r"def\s+\w*delete\w*\(", mem_src + store_src, re.I))

    # 正常开关下：真跑一遍 register()，确认端点**真的被挂上了**
    # 🔴 这一条跟 C 组不同 —— C 组是"直接调 MEM.install()"，
    #    它验不出"register() 里那一步漏了/写错了"。端到端就得走 register()。
    on_db = os.path.join(tmp, "on.db")
    on = FakeRelay(on_db)
    MEM._INSTALLED = False
    on_sum = AE.register(on, PREFIX)
    con = client_for(on)
    _no_auth = con.get(B).status_code
    _auth = con.get(B, headers=H).status_code
    # ⚠️ 断言要**同时**看两个数：只看"有密钥那次"的话，404 与 200 都能被读成"通过"。
    #    401（=挂上了但要密钥）才跟 404（=这一步没挂）区分得开。
    chk("D7  🔴 正常开关：register() 之后端点真挂上了（无密钥 401 / 有密钥 200）",
        _no_auth == 401 and _auth == 200,
        f"无密钥={_no_auth} 有密钥={_auth}（404 = 这一步没挂上）"
        f" warnings={on_sum.get('warnings')}")
    chk("D8  register() 摘要把记忆层报出来了",
        bool(on_sum.get("memory")) and "记忆层" in str(on_sum.get("memory")),
        str(on_sum.get("memory")))

    # 关掉开关的房子：端点 404，但**表与缝还在**
    off_db = os.path.join(tmp, "off.db")
    off = FakeRelay(off_db)
    os.environ["APP_EXT_MEMORY_DISABLED"] = "1"
    try:
        MEM._INSTALLED = False
        AE.register(off, PREFIX)
    finally:
        os.environ.pop("APP_EXT_MEMORY_DISABLED", None)
    coff = client_for(off)
    chk("D9  🔴 关掉开关：/app/ext/memories → 404（能力没了）",
        coff.get(B, headers=H).status_code == 404,
        str(coff.get(B, headers=H).status_code))
    # ⚠️ 裸 sqlite3.connect() 没有 row_factory → 取列要用**下标**，不是 `r["name"]`
    off_cols = [r[1] for r in sqlite3.connect(off_db).execute("PRAGMA table_info(memories)")]
    off_ver = sqlite3.connect(off_db).execute("PRAGMA user_version").fetchone()[0]
    chk("D10 🔴 但**表与缝还在**（能力没了 ≠ 数据没了）：source 列 + version 4",
        "source" in off_cols and off_ver == 4, f"cols={off_cols} ver={off_ver}")

    chk("D11 schema 版本 = 4", S.SCHEMA_VERSION == 4, str(S.SCHEMA_VERSION))
    chk("D12 messages 红线断言在（ensure_schema 里那段比对没被删）",
        "红线被破坏" in (DEPLOY / "app_ext" / "schema.py").read_text(encoding="utf-8"))

    line = MEM.summary_line()
    try:
        line.encode("gbk")
        gbk_ok = True
    except Exception as e:
        gbk_ok = False
        gbk_err = f"{type(e).__name__}: {e}"
    chk("D13 🔴 summary_line() GBK 安全（Windows 启动日志会打它）",
        gbk_ok, "" if gbk_ok else gbk_err)
    chk("D14 🔴 memory.py 的 print 语句里没有 GBK 编不出的符号（扫源码）",
        not [l for l in mem_src.splitlines()
             if "print(" in l and re.search(r"[\U0001F300-\U0001FAFF\u2705\u26A0\u274C]", l)],
        "有 print 含符号")

    va = (HERE / "verify_all.py").read_text(encoding="utf-8")
    chk("D15 verify_all 已接本套",
        "memory_check.py" in va and "13. tools/memory_check.py" in va, "")

    return tmp


def main():
    t0 = time.time()
    tmp = None
    try:
        tmp = run()
    except Exception:
        import traceback
        traceback.print_exc()
        chk("未捕获异常（整套没跑完）", False, "见上面的 traceback")

    npass = sum(1 for _n, ok, _d in results if ok)
    out = os.environ.get("MEMORY_CHECK_OUT") or str(HERE / "memory_report.txt")
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
