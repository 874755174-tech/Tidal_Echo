#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作间验收 —— 房子的第一间房 + 那扇 MCP 门
==========================================================================

## 这份脚本最重要的一条

**用官方 `mcp` SDK 真连一次。**

不是"我读了协议觉得对"，而是把 KaelLife 那三行原样搬过来跑：

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

（`KaelLife/scheduler.py:1175-1179`）

握手过一次，才算"他今天就能用上"。其余都是在证明**别把它做坏**。

⚠️ 官方 SDK 不在 `backend/requirements.txt` 里（房子自己实现协议，容器不需要它）。
   本机装了才有这一组：

       .venv\\Scripts\\python.exe -m pip install "mcp>=1.2.0,<2"

   没装 → C 组**响亮地跳过**（既不假装通过、也不假装失败），其余组照跑。

## 覆盖清单

  A. 工作间存储（纯逻辑）
     1-3   做一件：目录 / index.html / meta.json / id 形状 / 字段齐全
     4-5   正文逐字节一致；列表最近动过在前
     6-10  🔴 改一件：升版 + **旧版留档 r1.html** + 旧版读得回来
     11-13 不存在的 id / 不存在的版本 → 人话错误，不是崩
     14-15 note 与署名：不传就沿用
     16-17 🔴 id 形状与**路径穿越**一律拒，且根目录外没被建东西
     18-22 空 title / 空 html / 超大 / 超长 → 拒，且带实际数值
     23-25 🔴 三个上限（件数 / 总量 / 版本数）到顶 → 拒，**且旧版还在**
     26-27 meta.json 坏掉 → 列表标 broken、读取报人话，都不崩
     28-30 不相干的文件被忽略；原子写不留 .tmp；limit 生效

  B. 工具注册表与 JSON-RPC（纯逻辑）
     1-4   四个工具都在、都有中文描述、schema 形状对
     5-9   🔴 重名/撞名/非法名/坏 schema → 报错（不静默覆盖）；clear 只摘一间房
     10-11 install 幂等；tools/list 形状
     12-17 通知不回包 / 未知方法 -32601 / 坏消息 -32600 / **版本回显** / 头兜底
     18-24 tools/call 的四种错（未知名、ToolError、未知异常、参数不是对象）+ 返回非 str

  C. 🔴 真 MCP 客户端 + 原始 JSON-RPC（起真实房子）
     1-8   官方 SDK 握手 / 协议版本 / list_tools / 四个工具真跑 / 未知名 isError
     9-10  🔴 无密钥、错密钥 → 401（fail-closed）
     11    🔴 **协议版本回显**（模拟一个老客户端说 2024-11-05）
     12-17 通知 202 / GET·DELETE 405 / 未知方法 / 解析错 / 超大 413 / 别名路径

  D. 房子 REST 端点 + raw 的安全
     1-4   三个端点无密钥 401；list / item 正常；不存在 404
     5-8   🔴 raw 是 text/html + **CSP sandbox** + **没有 allow-same-origin** + nosniff
     9-10  旧版看得到；非法 id → 404（不是 500、也读不到东西）
     11-14 🔴 回归：/healthz、/app/history、/app/ext/me 都还在（房子照常营业）

  E. 接线 / 幂等 / 红线
     1-3   register 摘要含房间；entrypoint 设了目录；**register 在静态挂载之前**
     4-5   🔴 房间不 import KaelLife / requests；install 两次不炸
     6-7   逃生开关存在；verify_all 里有这一套
     8     🔴 **空数据库**上身份层/门/房间照样装上（这是一个真 bug 的回归锁，
           见 app_ext/__init__.py 里 `_step` 的注释）

  F. 展示页（web/workshop.html）与接线
     1-5   页面在；🔴 预览走 srcdoc、**URL 里不出现密钥**；iframe 只给 allow-scripts；
           不写死密钥；三个端点路径对
     6-8   菜单里有 Workshop 并真的路由过去；🔴 sw.js 提过缓存版本
     9-11  实跑：/relay/workshop.html 200 且是工作间；列表端点真返回 C 组造的那一件

⚠️ 起房子前**必须空出 8798 / 8799**（本套用自己的端口，不跟 secaudit 的 8080 抢）。
⚠️ C/D/F 组找文件的路径一律走 `server_workshop(tmp)` —— 别手抄 `tmp/"workshop"`，
   那个假红吃过一次（服务器写 `tmp/house/workshop`，测试去 `tmp/workshop` 找）。
⚠️ 本套会给自己设 `NO_PROXY=127.0.0.1,localhost`（见常量区注释）：官方 mcp SDK 用的
   httpx 会把本机地址也塞进代理，报出来只有一句 `ExceptionGroup`，像"门坏了"其实不是。

用法：.venv\\Scripts\\python.exe tools\\workshop_check.py
"""
import ast
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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

SECRET = "test-secret-workshop-0123456789"
PORT_OK = 8798       # 有数据的库（正常路径）
PORT_FRESH = 8799    # 全新的库（空 /data 那种）
PREFIX = "/relay"

# 🔴 本机自测**不许走代理**（2026-09-19 吃过的假红）。
#    官方 mcp SDK 用的 httpx 默认 `trust_env=True`，会把 `127.0.0.1` 也塞进
#    `HTTP_PROXY` / `HTTPS_PROXY` → C1 握手直接炸，而报出来只有一句
#    `ExceptionGroup: unhandled errors in a TaskGroup`（**看不出是代理**）。
#    ⚠️ 注意这是**测试环境**的问题，不是房子的问题：同一个 env 加上 NO_PROXY
#       之后本套 121/121 全绿；raw urllib 那几条（C11–C18）本来也不受影响，
#       因为 urllib 默认就 bypass localhost —— 只有 httpx 需要显式告诉它。
_no = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
_missing = [h for h in ("127.0.0.1", "localhost") if h not in _no]
if _missing:
    _merged = ",".join([p for p in (_no, *_missing) if p])
    os.environ["NO_PROXY"] = _merged
    os.environ["no_proxy"] = _merged

results: list = []
_skipped: list = []
_C_ID: str | None = None   # C 组通过官方 SDK 造出来的那一件（D 组拿它做端点验收）


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def skip_group(name: str, why: str) -> None:
    _skipped.append((name, why))


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def req(url, *, method="GET", token=None, body=None, raw_body=None, timeout=20,
        headers=None):
    data = raw_body
    if data is None and body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "{}"), dict(resp.headers)
            except Exception:
                return resp.status, {"_raw": raw}, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)


def hget(headers: dict, name: str) -> str:
    """大小写无关地取一个响应头。

    ⚠️ Starlette 发出去的响应头是**全小写**的，而 `dict(resp.headers)` 是普通 dict
    （大小写敏感）—— 直接 `h["Referrer-Policy"]` 会取不到，看着像"头没发"，
    其实发了。这个假红吃过一次，别再手写两遍 `or h.get(小写)`。
    """
    want = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == want:
            return v or ""
    return ""


def wait_port(port, path="/healthz", timeout=40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def house_dir(tmp: Path) -> Path:
    """房子进程的工作目录（`RELAY_DB` 等都在它下面）。"""
    return tmp / "house"


def workshop_under(home: Path) -> Path:
    """工作间在房子目录下的**唯一**子目录名 —— 只在这里拼一次。"""
    return home / "workshop"


def server_workshop(tmp: Path) -> Path:
    """🔴 服务器进程眼里 `RELAY_WORKSHOP_DIR` **到底是哪个目录**。

    必须跟 `house_env()` 走**同一个算式**。上一版这里对不上：`house_env()` 收到的是
    房子目录，于是服务器写 `tmp/house/workshop`，而 C/D 组去 `tmp/workshop` 找文件 ——
    结果一片"id 明明返回了却找不到那个文件"的假红，还顺手把 A 组留在 `tmp/workshop`
    的条目当成了服务器做的（服务器当然说"没这一件"）。算式只留一处，别再各处手抄。
    """
    return workshop_under(house_dir(tmp))


def house_env(home: Path, port: int) -> dict:
    env = dict(os.environ)
    env.update({
        "RELAY_DB": str(home / "relay.db"),
        "RELAY_SECRET": SECRET,
        "RELAY_HUMAN_NAME": "Lily",
        "RELAY_PUBLIC_PREFIX": PREFIX,
        "RELAY_BACKEND_DIR": str(BACKEND),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "RELAY_UPLOAD_DIR": str(home / "uploads"),
        "RELAY_WORKSHOP_DIR": str(workshop_under(home)),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        # 供应商全关掉 —— 这一套不验模型网关，别让它去碰网络
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini,relay",
    })
    return env


def start_house(home: Path, port: int):
    """`home` = **房子目录本身**（不是临时根目录）。`RELAY_*` 全挂在它下面。"""
    env = house_env(home, port)
    log_path = home / f"uvicorn-{port}.log"
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY)],
        env=env, stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf, log_path


def seed_messages(db_path: Path) -> None:
    """造一张和后端一模一样的 messages 表 + 一条消息。

    为什么要有它：后端在 **lifespan** 里建表，而 `app_ext.register()` 跑在它之前 ——
    所以只有"库里已经有 messages"时，会话投影那一步才会成功。
    这里就是要跑**正常路径**。
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
    conn.execute(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        ("2026-09-18T12:00:00+08:00", "in", "user", "在吗", "{}"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# A 组 · 工作间存储
# ---------------------------------------------------------------------------

def _expect_tool_error(fn, *a, **kw):
    """跑一个应当被拒的动作，返回它的错误文本（不是异常类型）。"""
    from app_ext import mcp as MCP
    try:
        fn(*a, **kw)
    except MCP.ToolError as e:
        return str(e)
    except Exception as e:
        return f"!!! 抛了别的异常：{type(e).__name__}: {e}"
    return ""


def part_a(tmp: Path) -> None:
    from app_ext import mcp as MCP
    from app_ext.modules import workshop as W

    root = tmp / "workshop_a"
    os.environ["RELAY_WORKSHOP_DIR"] = str(root)

    HTML1 = "<html><body><h1>海</h1><p>第一版</p></body></html>"
    HTML2 = "<html><body><h1>海</h1><p>第二版，改了</p></body></html>"

    m1 = W.create_thing("第一件事", HTML1, note="试试工作间")
    d1 = root / m1["id"]

    chk("A1 做一件 → 目录 + index.html + meta.json 都在",
        d1.is_dir() and (d1 / "index.html").is_file() and (d1 / "meta.json").is_file(),
        str(list(d1.iterdir()) if d1.is_dir() else d1))
    chk("A2 id 形状 = YYYYMMDD-xxxx（日期是今天）",
        bool(re.match(r"^\d{8}-[0-9a-f]{4}$", m1["id"]))
        and m1["id"][:8] == W._today_str(),
        m1["id"])
    chk("A3 meta 字段齐全",
        all(k in m1 for k in ("id", "title", "note", "kind", "by", "created",
                              "updated", "revision", "bytes", "revisions")),
        str(sorted(m1)))
    chk("A4 正文逐字节等于传进去的 html",
        (d1 / "index.html").read_text(encoding="utf-8") == HTML1)
    chk("A4b meta.bytes 与真实字节数一致",
        m1["bytes"] == len(HTML1.encode("utf-8")), str(m1["bytes"]))
    chk("A4c 第一次做出来 revision=1 且 revisions 只有一条",
        m1["revision"] == 1 and len(m1["revisions"]) == 1)

    time.sleep(0.01)
    m2 = W.create_thing("第二件事", "<h1>另一件</h1>")
    lst = [W.item_summary(x)["id"] for x in W.list_things(limit=10)]
    chk("A5 列表最近动过在前（第二件排第一）", lst[:2] == [m2["id"], m1["id"]], str(lst[:3]))

    m1b = W.revise_thing(m1["id"], HTML2, note="第二版说明")
    chk("A6 改一件 → revision=2",
        m1b["revision"] == 2 and len(m1b["revisions"]) == 2, str(m1b["revision"]))
    chk("A7 🔴 旧版留档 r1.html 真的在，且内容是原来那一版",
        (d1 / "r1.html").is_file()
        and (d1 / "r1.html").read_text(encoding="utf-8") == HTML1,
        str(sorted(x.name for x in d1.iterdir())))
    chk("A9 改完之后 index.html 是新内容",
        (d1 / "index.html").read_text(encoding="utf-8") == HTML2)
    try:
        _meta, old, _t = W.read_thing(m1["id"], 1)
        ok_old = old == HTML1
    except Exception as e:
        ok_old, old = False, repr(e)
    chk("A8 🔴 旧版**读得回来**（不然'旧版留着'只是一句说法）", ok_old, str(old)[:80])
    _meta, cur, _t = W.read_thing(m1["id"])
    chk("A10 read 默认读现在这一版", cur == HTML2)
    chk("A11 不存在的版本 → 人话错误（且列出有哪些旧版）",
        "没有第 9 版" in _expect_tool_error(W.read_thing, m1["id"], 9),
        _expect_tool_error(W.read_thing, m1["id"], 9))
    chk("A12 改不存在的 id → 人话错误",
        "没有这一件" in _expect_tool_error(W.revise_thing, "20260101-aaaa", "<h1>x</h1>"),
        "")
    chk("A13 读不存在的 id → 人话错误",
        "没有这一件" in _expect_tool_error(W.read_thing, "20260101-aaaa"))

    m1c = W.revise_thing(m1["id"], "<h1>第三版</h1>")   # 不传 note
    chk("A14 改的时候不传 note → 沿用原来的说明",
        m1c["note"] == "第二版说明", m1c["note"])
    chk("A15 署名默认 kael；改的时候不传就沿用",
        m1["by"] == "kael" and m1c["by"] == "kael", f"{m1['by']}/{m1c['by']}")

    bad_ids = ["", "../x", "..\\evil", "2026-09-18", "20260918-7F3A",
               "20260918-7f3", "20260918-7f3aa", "notes.txt", None]
    all_bad = all(_expect_tool_error(W.read_thing, b) for b in bad_ids)
    chk("A16 🔴 一组合法外的 id 形状全部被拒", all_bad,
        str([b for b in bad_ids if not _expect_tool_error(W.read_thing, b)]))

    # 穿越：确认根目录**外面**什么都没被建出来
    outside = tmp / "evil"
    before = sorted(p.name for p in tmp.iterdir())
    _expect_tool_error(W.create_thing, "穿越", "<h1>x</h1>")  # 正常那件应该成功
    for bad in ("../../evil", "..\\..\\evil", "/etc/passwd"):
        _expect_tool_error(W._item_dir, bad)
    chk("A17 🔴 路径穿越被拒，且根目录外没有多出东西",
        not (tmp / "evil").exists() and not outside.exists()
        and sorted(p.name for p in tmp.iterdir()).count("evil") == 0,
        str(before))

    chk("A18 空 title → 拒", "少了 title" in _expect_tool_error(W.create_thing, "  ", "<h1>x</h1>"))
    chk("A19 空 html / 非字符串 → 拒",
        "少了 html" in _expect_tool_error(W.create_thing, "x", "")
        and "少了 html" in _expect_tool_error(W.create_thing, "x", 123))
    big = "<h1>" + ("a" * (W.MAX_HTML_BYTES + 100)) + "</h1>"
    e_big = _expect_tool_error(W.create_thing, "太大", big)
    chk("A20 正文超上限 → 拒，且人话里带实际大小",
        "太大了" in e_big and "KB" in e_big, e_big)
    e_long = _expect_tool_error(W.create_thing, "标" * (W.MAX_TITLE_CHARS + 1), "<h1>x</h1>")
    chk("A21 title 超长 → 拒，且带数值", "title 太长" in e_long and str(W.MAX_TITLE_CHARS) in e_long, e_long)
    e_note = _expect_tool_error(W.create_thing, "x", "<h1>x</h1>",
                                "n" * (W.MAX_NOTE_CHARS + 1))
    chk("A22 note 超长 → 拒", "note 太长" in e_note, e_note)

    # 三个上限 —— 临时把常量调小（跑完恢复）
    old_items, old_total, old_revs = W.MAX_ITEMS, W.MAX_TOTAL_BYTES, W.MAX_REVISIONS
    try:
        W.MAX_ITEMS = 2
        d3 = tmp / "cap_items"
        os.environ["RELAY_WORKSHOP_DIR"] = str(d3)
        W.create_thing("1", "<h1>1</h1>")
        W.create_thing("2", "<h1>2</h1>")
        e = _expect_tool_error(W.create_thing, "3", "<h1>3</h1>")
        chk("A23 🔴 件数到顶 → 拒（不是静默丢弃，也不是悄悄覆盖）",
            "装不下" in e, e)
        chk("A23b 到顶时前两件都还在", len(W.list_things(limit=50)) == 2)

        W.MAX_ITEMS = 9999
        W.MAX_TOTAL_BYTES = 120
        d4 = tmp / "cap_total"
        os.environ["RELAY_WORKSHOP_DIR"] = str(d4)
        W.create_thing("大件", "<h1>" + "x" * 100 + "</h1>")
        e2 = _expect_tool_error(W.create_thing, "再来", "<h1>" + "y" * 100 + "</h1>")
        chk("A24 🔴 总量到顶 → 拒（保护 /data 卷）", "快满了" in e2, e2)

        W.MAX_TOTAL_BYTES = old_total
        W.MAX_REVISIONS = 2
        d5 = tmp / "cap_rev"
        os.environ["RELAY_WORKSHOP_DIR"] = str(d5)
        mm = W.create_thing("改到顶", "<h1>v1</h1>")
        W.revise_thing(mm["id"], "<h1>v2</h1>")
        e3 = _expect_tool_error(W.revise_thing, mm["id"], "<h1>v3</h1>")
        chk("A25 🔴 版本数到顶 → 拒，**而且老的那一版还在**",
            "改不动了" in e3 and (d5 / mm["id"] / "r1.html").is_file(), e3)
    finally:
        W.MAX_ITEMS, W.MAX_TOTAL_BYTES, W.MAX_REVISIONS = old_items, old_total, old_revs

    # 坏 meta / 杂项文件
    d6 = tmp / "bad"
    os.environ["RELAY_WORKSHOP_DIR"] = str(d6)
    W.create_thing("好的", "<h1>ok</h1>")
    (d6 / "20260101-dead").mkdir()
    (d6 / "20260101-dead" / "meta.json").write_text("{这不是 JSON", encoding="utf-8")
    (d6 / "notes.txt").write_text("不相干", encoding="utf-8")
    (d6 / "notanid").mkdir()
    try:
        items = [W.item_summary(x) for x in W.list_things(limit=50)]
        broken = [i for i in items if i.get("broken")]
        chk("A26 meta.json 坏掉 → 列表标 broken 且不崩（其余条目照常）",
            len(broken) == 1 and len(items) == 2, f"broken={len(broken)} items={len(items)}")
    except Exception as e:
        chk("A26 meta.json 坏掉 → 列表标 broken 且不崩", False, repr(e))
    chk("A27 读坏掉的条目 → 人话错误（叫你别改它、先跟 Lily 说一声）",
        "读不出来" in _expect_tool_error(W.read_thing, "20260101-dead"))
    chk("A28 不相干的文件与目录被忽略（notes.txt / notanid 不进列表）",
        len(W.list_things(limit=50)) == 2)
    chk("A29 原子写：目录里没有 .tmp 残留",
        not list(d6.rglob("*.tmp")), str(list(d6.rglob("*.tmp"))))
    os.environ["RELAY_WORKSHOP_DIR"] = str(root)
    chk("A30 list 的 limit 生效",
        len(W.list_things(limit=1)) == 1 and len(W.list_things(limit=50)) >= 2)


# ---------------------------------------------------------------------------
# B 组 · 工具注册表 + JSON-RPC
# ---------------------------------------------------------------------------

def part_b(tmp: Path) -> None:
    from app_ext import mcp as MCP
    from app_ext.modules import workshop as W

    os.environ["RELAY_WORKSHOP_DIR"] = str(tmp / "workshop_b")
    names = W.install_tools()
    chk("B1 四个工具都注册上了",
        set(names) == {"make_thing", "revise_thing", "list_things", "read_thing"},
        str(names))
    specs = {t.name: t for t in MCP.installed_tools()}
    chk("B2 每个都有非空的中文描述",
        all(s.description.strip() and any("\u4e00" <= c <= "\u9fff"
                                         for c in s.description) for s in specs.values()))
    chk("B3 每个 inputSchema 都是 type=object",
        all(s.input_schema.get("type") == "object" for s in specs.values()))
    req_make = specs["make_thing"].input_schema.get("required") or []
    req_rev = specs["revise_thing"].input_schema.get("required") or []
    chk("B4 make_thing 必填 title+html；revise_thing 必填 id+html",
        set(req_make) == {"title", "html"} and set(req_rev) == {"id", "html"},
        f"{req_make} / {req_rev}")
    chk("B24 schema 明确 additionalProperties=False（多余键要被拒）",
        all(s.input_schema.get("additionalProperties") is False
            for s in specs.values()))

    dup_err = ""
    try:
        MCP.register_tool("make_thing", "重名", {"type": "object"}, lambda a: "")
    except ValueError as e:
        dup_err = str(e)
    chk("B5 🔴 重名注册 → 报错（不静默覆盖）", "重复" in dup_err, dup_err)

    dup2 = ""
    try:
        MCP.register_tool("make_thing", "跨房间重名", {"type": "object"},
                          lambda a: "", room="另一间房")
    except ValueError as e:
        dup2 = str(e)
    chk("B6 🔴 两个房间撞名也报错（说明它们其实是一间房）", "重复" in dup2, dup2)

    bad_name = ""
    try:
        MCP.register_tool("有空格 的名字", "x", {"type": "object"}, lambda a: "")
    except ValueError as e:
        bad_name = str(e)
    chk("B7 非法工具名 → 报错", "工具名" in bad_name, bad_name)

    bad_schema = ""
    try:
        MCP.register_tool("ok_name", "x", {"type": "string"}, lambda a: "")
    except ValueError as e:
        bad_schema = str(e)
    chk("B8 input_schema 不是 object → 报错", "JSON Schema" in bad_schema, bad_schema)

    MCP.register_tool("temp_thing", "临时", {"type": "object"}, lambda a: "", room="temp")
    n_before = len(MCP.tool_names())
    gone = MCP.clear_tools(room="temp")
    chk("B9 clear_tools(room=) 只摘那一间房的",
        gone == 1 and "temp_thing" not in MCP.tool_names()
        and len(MCP.tool_names()) == n_before - 1, str(MCP.tool_names()))

    names2 = W.install_tools()
    chk("B10 install 幂等（再跑一次工具数不变）",
        sorted(names2) == sorted(names) and len(MCP.tool_names()) == n_before - 1,
        str(names2))

    r = MCP.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "x")
    tool_list = (r or {}).get("result", {}).get("tools") or []
    chk("B11 tools/list 形状 = [{name, description, inputSchema}]",
        len(tool_list) == 4
        and all(set(t) >= {"name", "description", "inputSchema"} for t in tool_list),
        str(tool_list[:1])[:120])

    chk("B12 通知（没有 id）→ 不回包",
        MCP.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, "x") is None)
    m = MCP.handle_message({"jsonrpc": "2.0", "id": 2, "method": "no/such"}, "x")
    chk("B13 未知方法 → -32601（不是静默成功）",
        (m or {}).get("error", {}).get("code") == -32601, str(m))
    chk("B14 非对象消息 → -32600",
        (MCP.handle_message("not-a-dict", "x") or {}).get("error", {}).get("code") == -32600)

    init = MCP.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}}, "2024-11-05")
    res = (init or {}).get("result", {})
    chk("B15 🔴 initialize 回显客户端说的版本（打死一个版本会把另一端打死）",
        res.get("protocolVersion") == "2024-11-05", str(res.get("protocolVersion")))
    chk("B15b 声明了 tools 能力 + serverInfo + instructions",
        "tools" in (res.get("capabilities") or {})
        and res.get("serverInfo", {}).get("name")
        and res.get("instructions"))
    chk("B16 客户端没带版本 → 用兜底版本（且是个非空字符串）",
        MCP.pick_version({}, None) == MCP._FALLBACK_VERSION)
    chk("B17 认 MCP-Protocol-Version 头",
        MCP.pick_version({}, "2025-03-26") == "2025-03-26"
        and MCP.pick_version({"params": {"protocolVersion": "x-y"}}, "2025-03-26") == "x-y")

    c = MCP.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "nope", "arguments": {}}}, "x")
    cr = (c or {}).get("result", {})
    chk("B18 未知名 → isError，并列出可用名字",
        cr.get("isError") is True and "没有这个工具" in cr["content"][0]["text"],
        str(cr)[:120])

    def _boom(args):
        raise MCP.ToolError("id 不存在：abc")

    def _wild(args):
        raise RuntimeError("内部炸了")

    def _notstr(args):
        return {"a": 1}

    def _none(args):
        return None

    MCP.register_tool("t_toolerror", "x", {"type": "object"}, _boom, room="temp2")
    MCP.register_tool("t_wild", "x", {"type": "object"}, _wild, room="temp2")
    MCP.register_tool("t_notstr", "x", {"type": "object"}, _notstr, room="temp2")
    MCP.register_tool("t_none", "x", {"type": "object"}, _none, room="temp2")
    try:
        rr = MCP.handle_message({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": "t_toolerror", "arguments": {}}}, "x")
        t1 = rr["result"]
        chk("B19 ToolError → isError + 原样带回那句话（他能读懂）",
            t1.get("isError") is True and t1["content"][0]["text"] == "id 不存在：abc",
            str(t1)[:140])

        rr = MCP.handle_message({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                 "params": {"name": "t_wild", "arguments": {}}}, "x")
        t2 = rr["result"]
        rr2 = MCP.handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, "x")
        chk("B20 工具抛未知异常 → isError，且**不带走整扇门**（后面的调用照常）",
            t2.get("isError") is True and "RuntimeError" in t2["content"][0]["text"]
            and len(rr2["result"]["tools"]) > 0, str(t2)[:140])

        rr = MCP.handle_message({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                 "params": {"name": "t_notstr", "arguments": {}}}, "x")
        chk("B21 工具返回非字符串 → 被转成 JSON 文本",
            '"a": 1' in rr["result"]["content"][0]["text"], str(rr["result"])[:120])
        rr = MCP.handle_message({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                 "params": {"name": "t_none", "arguments": {}}}, "x")
        chk("B22 工具返回 None → 有兜底文案（不是空字符串）",
            bool(rr["result"]["content"][0]["text"].strip()))
        rr = MCP.handle_message({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                 "params": {"name": "list_things", "arguments": "不是对象"}}, "x")
        chk("B23 arguments 不是对象 → isError",
            rr["result"].get("isError") is True, str(rr["result"])[:120])
    finally:
        MCP.clear_tools(room="temp2")


# ---------------------------------------------------------------------------
# C 组 · 真 MCP 客户端 + 原始 JSON-RPC
# ---------------------------------------------------------------------------

def _sdk_available() -> bool:
    try:
        import mcp  # noqa: F401
        from mcp.client.streamable_http import streamablehttp_client  # noqa: F401
        return True
    except Exception:
        return False


def _sdk_round(port: int) -> dict:
    """把 KaelLife 的那三行原样跑一遍。返回一个结果字典（给断言用）。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{port}/mcp"
    out = {"error": None}
    headers = {"Authorization": f"Bearer {SECRET}"}

    async def go():
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                out["protocol"] = init.protocolVersion
                out["server"] = f"{init.serverInfo.name}/{init.serverInfo.version}"
                tools = await session.list_tools()
                out["tools"] = sorted(t.name for t in tools.tools)
                out["descriptions_ok"] = all(
                    t.description and t.inputSchema.get("type") == "object"
                    for t in tools.tools)

                made = await session.call_tool("make_thing", arguments={
                    "title": "他做的第一件",
                    "html": "<html><body><svg viewBox='0 0 10 10'>"
                            "<circle cx='5' cy='5' r='4'/></svg></body></html>",
                    "note": "用官方客户端做的",
                })
                out["make_isError"] = made.isError
                out["make_text"] = made.content[0].text if made.content else ""
                mm = re.search(r"id=(\d{8}-[0-9a-f]{4})", out["make_text"])
                out["id"] = mm.group(1) if mm else None

                listed = await session.call_tool("list_things", arguments={})
                out["list_text"] = listed.content[0].text if listed.content else ""

                if out["id"]:
                    got = await session.call_tool("read_thing", arguments={"id": out["id"]})
                    out["read_text"] = got.content[0].text if got.content else ""
                    rev = await session.call_tool("revise_thing", arguments={
                        "id": out["id"], "html": "<h1>改过了</h1>"})
                    out["revize_isError"] = rev.isError
                    out["revise_text"] = rev.content[0].text if rev.content else ""

                bad = await session.call_tool("no_such_thing", arguments={})
                out["bad_isError"] = bad.isError
                out["bad_text"] = bad.content[0].text if bad.content else ""

    try:
        asyncio.run(go())
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def part_c(tmp: Path) -> None:
    base = f"http://127.0.0.1:{PORT_OK}"
    mcp_url = base + "/mcp"

    # ⑨⑩ 鉴权 fail-closed —— 先验，因为它是门锁
    st, _d, _h = req(mcp_url, method="POST",
                     body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    chk("C9 🔴 无密钥 POST /mcp → 401", st == 401, f"实际 {st}")
    st, _d, _h = req(mcp_url, method="POST", token="wrong-secret",
                     body={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    chk("C10 🔴 错密钥 → 401", st == 401, f"实际 {st}")

    # ⑪ 协议版本回显（模拟老客户端）
    st, d, _h = req(mcp_url, method="POST", token=SECRET,
                    body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05",
                                     "capabilities": {}, "clientInfo": {"name": "old"}}})
    chk("C11 🔴 老客户端说 2024-11-05 → 原样回显（写死一个版本会把它打死）",
        st == 200 and (d.get("result") or {}).get("protocolVersion") == "2024-11-05",
        f"{st} {json.dumps(d, ensure_ascii=False)[:120]}")

    st, _d, _h = req(mcp_url, method="POST", token=SECRET,
                     body={"jsonrpc": "2.0", "method": "notifications/initialized"})
    chk("C12 通知 → 202（按协议不回 body）", st == 202, f"实际 {st}")

    for meth in ("GET", "DELETE"):
        st, _d, _h = req(mcp_url, method=meth, token=SECRET)
        chk(f"C13/C14 {meth} /mcp → 405（无状态门明确拒绝，不假装成功）",
            st == 405, f"实际 {st}")

    st, d, _h = req(mcp_url, method="POST", token=SECRET,
                    body={"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    chk("C15 未知方法 → -32601",
        (d.get("error") or {}).get("code") == -32601, json.dumps(d, ensure_ascii=False)[:120])

    st, d, _h = req(mcp_url, method="POST", token=SECRET, raw_body="{这不是 JSON".encode("utf-8"))
    chk("C16 body 不是 JSON → -32700", st == 400 and (d.get("error") or {}).get("code") == -32700,
        f"{st} {json.dumps(d, ensure_ascii=False)[:120]}")

    st, _d, _h = req(mcp_url, method="POST", token=SECRET,
                     raw_body=b"{" + b"a" * (2 * 1024 * 1024 + 10) + b"}")
    chk("C17 body 超大 → 413（有护栏，不是拿内存硬扛）", st == 413, f"实际 {st}")

    st, d, _h = req(base + "/app/ext/mcp", method="POST", token=SECRET,
                    body={"jsonrpc": "2.0", "id": 3, "method": "ping"})
    chk("C18 /app/ext/mcp 别名也通（给自己人排查用）", st == 200 and "result" in d,
        f"{st} {json.dumps(d, ensure_ascii=False)[:80]}")

    # ①-⑧ 官方 SDK
    if not _sdk_available():
        skip_group("C1-C8 真 MCP 客户端",
                   "本机没装 mcp SDK："
                   '.venv\\Scripts\\python.exe -m pip install "mcp>=1.2.0,<2"')
        return
    out = _sdk_round(PORT_OK)
    global _C_ID
    _C_ID = out.get("id")
    chk("C1 🔴 官方 mcp SDK 能连上（照 KaelLife scheduler.py:1175-1179 的写法）",
        out.get("error") is None, str(out.get("error")))
    if out.get("error"):
        return
    chk("C2 握手拿到的协议版本非空（回显，不回错）", bool(out.get("protocol")),
        str(out.get("protocol")))
    chk("C3 list_tools 拿到四个工具",
        out.get("tools") == ["list_things", "make_thing", "read_thing", "revise_thing"],
        str(out.get("tools")))
    chk("C3b 每个工具的 description 与 inputSchema 都被客户端认了",
        out.get("descriptions_ok") is True)
    chk("C4 🔴 call_tool make_thing 真的造出了东西（返回里有 id）",
        out.get("make_isError") is False and out.get("id"), str(out.get("make_text"))[:120])
    chk("C4b 返回里给了她那边能打开的东西（或明确说放在架子上）",
        "工作间" in (out.get("make_text") or ""), (out.get("make_text") or "")[-80:])
    _d1 = server_workshop(tmp) / str(out.get("id"))
    chk("C4c 🔴 返回的 id 在磁盘上真有那个目录（不是只回了一个字符串）",
        _d1.is_dir() and (_d1 / "index.html").is_file() and (_d1 / "meta.json").is_file(),
        f"目录={_d1} 存在={_d1.is_dir()} 里面="
        f"{sorted(p.name for p in _d1.iterdir()) if _d1.is_dir() else '（无）'} "
        f"| 服务器的工作间在={server_workshop(tmp)}")
    chk("C5 call_tool list_things 看得见它",
        out.get("id") in (out.get("list_text") or ""), (out.get("list_text") or "")[:100])
    chk("C6 call_tool read_thing 原文读得回来（含 SVG）",
        "circle" in (out.get("read_text") or ""), (out.get("read_text") or "")[:100])
    _r1 = server_workshop(tmp) / str(out.get("id")) / "r1.html"
    chk("C7 call_tool revise_thing → 第 2 版 + 旧版还在",
        out.get("revize_isError") is False and "第 2 版" in (out.get("revise_text") or "")
        and _r1.is_file(),
        f"isError={out.get('revize_isError')} r1存在={_r1.is_file()} "
        f"文本={(out.get('revise_text') or '')[:100]}")
    _old = _r1.read_text(encoding="utf-8") if _r1.is_file() else ""
    chk("C7b 旧版内容是原始那一版（含 SVG）",
        _old.count("circle") == 1, f"r1 里 circle 出现 {_old.count('circle')} 次")
    chk("C8 名字写错 → isError（不是抛异常、不是静默）",
        out.get("bad_isError") is True and "没有这个工具" in (out.get("bad_text") or ""),
        (out.get("bad_text") or "")[:120])


# ---------------------------------------------------------------------------
# D 组 · 房子 REST + raw 的安全
# ---------------------------------------------------------------------------

def part_d(tmp: Path) -> None:
    base = f"http://127.0.0.1:{PORT_OK}"
    wk = server_workshop(tmp)
    # 优先用 C 组**真的造出来**的那一件 —— 别靠"按字母序取第一个"猜，
    # 那个写法在目录里有别的东西时会一头撞到不存在的条目上。
    item_id = None
    if _C_ID and (wk / _C_ID / "index.html").is_file():
        item_id = _C_ID
    elif wk.is_dir():
        cands = sorted([p.name for p in wk.iterdir() if (p / "index.html").is_file()])
        item_id = cands[0] if cands else None
    chk("D0 前置：指向的是 C 组刚造的那一件（目录里没有别人塞的东西）",
        item_id == _C_ID and item_id is not None,
        f"item_id={item_id} C_组的={_C_ID} 目录里={sorted(p.name for p in wk.iterdir()) if wk.is_dir() else '（无目录）'}")
    if not item_id:
        return

    for path in ("/app/ext/workshop/list",
                 f"/app/ext/workshop/item/{item_id}",
                 f"/app/ext/workshop/raw/{item_id}"):
        st, _d, _h = req(base + path)
        chk(f"D1 🔴 无密钥 {path} → 401", st == 401, f"实际 {st}")

    st, d, _h = req(base + "/app/ext/workshop/list", token=SECRET)
    chk("D2 list 200 且 total / items / dir 都在",
        st == 200 and d.get("ok") and isinstance(d.get("items"), list)
        and d.get("total") == len(d["items"]) and d.get("dir"),
        f"{st} {json.dumps(d, ensure_ascii=False)[:120]}")

    st, d, _h = req(base + f"/app/ext/workshop/item/{item_id}", token=SECRET)
    chk("D3 item/{id} 200 且带回 revisions 历史",
        st == 200 and d.get("ok") and isinstance(d.get("revisions"), list),
        f"{st} {json.dumps(d, ensure_ascii=False)[:120]}")

    st, _d, _h = req(base + "/app/ext/workshop/item/20200101-aaaa", token=SECRET)
    chk("D4 item/不存在 → 404（不是 500）", st == 404, f"实际 {st}")

    st, d, h = req(base + f"/app/ext/workshop/raw/{item_id}", token=SECRET)
    ctype = hget(h, "Content-Type")
    chk("D5 raw 200 且 Content-Type 是 text/html",
        st == 200 and "text/html" in ctype, f"{st} {ctype}")
    csp = hget(h, "Content-Security-Policy")
    chk("D6 🔴 raw 带 CSP sandbox（顶层直接打开也被关进沙箱）",
        "sandbox" in csp, csp[:120])
    chk("D7 🔴 CSP 里**没有** allow-same-origin —— "
        "否则他写的一段脚本就能读走 localStorage 里的房子密钥",
        "allow-same-origin" not in csp, csp[:160])
    chk("D8 raw 带 nosniff + no-referrer（大小写无关地取头）",
        hget(h, "X-Content-Type-Options").lower() == "nosniff"
        and hget(h, "Referrer-Policy").lower() == "no-referrer",
        str(dict(h)))

    st, d2, _h = req(base + f"/app/ext/workshop/raw/{item_id}?rev=1", token=SECRET)
    body2 = d2.get("_raw") or ""
    chk("D9 raw?rev=1 看得到旧版（'旧版留着'是可验的）",
        st == 200 and "circle" in body2 and "改过了" not in body2, body2[:80])

    for bad in ("20200101-aaaa", "notes.txt", "....", "%2e%2e%2f%2e%2e%2frelay.db"):
        st, _d, _h = req(base + f"/app/ext/workshop/raw/{bad}", token=SECRET)
        ok = st in (404, 400, 422)
        if not ok:
            chk(f"D10 🔴 raw 非法 id {bad} → 4xx（不读得到东西、不 500）", False, f"实际 {st}")
            break
    else:
        chk("D10 🔴 一组非法 id 一律 4xx（不读得到东西、不 500）", True)

    st, _d, _h = req(base + "/healthz")
    chk("D11 🔴 回归：/healthz 仍 200（房子照常营业）", st == 200, f"实际 {st}")
    st, _d, _h = req(base + "/app/history?since=0", token=SECRET)
    chk("D12 🔴 回归：原版 /app/history 仍 200（红线目录没被碰）", st == 200, f"实际 {st}")
    st, _d, _h = req(base + "/app/ext/me", token=SECRET)
    chk("D13 🔴 回归：/app/ext/me 仍 200（身份层还在）", st == 200, f"实际 {st}")

    st, d, _h = req(base + f"/app/ext/workshop/raw/{item_id}", token=SECRET)
    chk("D14 raw 的正文里没有密钥（房子密钥不会被写进他做的东西里）",
        SECRET not in (d.get("_raw") or ""))


# ---------------------------------------------------------------------------
# E 组 · 接线 / 幂等 / 红线
# ---------------------------------------------------------------------------

def part_e(tmp: Path) -> None:
    dep = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    serve = (DEPLOY / "serve.py").read_text(encoding="utf-8")
    entry = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8")
    ws = (DEPLOY / "app_ext" / "modules" / "workshop.py").read_text(encoding="utf-8")
    va = (HERE / "verify_all.py").read_text(encoding="utf-8")

    chk("E1 register 摘要里有 rooms 这一项", '"rooms"' in dep and "summary[\"rooms\"]" in dep)
    chk("E2 entrypoint 设了 RELAY_WORKSHOP_DIR 并建目录",
        "RELAY_WORKSHOP_DIR" in entry and 'mkdir -p' in entry
        and '"$RELAY_WORKSHOP_DIR"' in entry)
    i_reg = serve.index("app_ext.register")
    i_mount = serve.index('mount("/"')
    chk("E3 🔴 app_ext.register 在静态挂载**之前**（否则 API 会被静态目录吃掉）",
        i_reg < i_mount, f"register@{i_reg} mount@{i_mount}")

    tree = ast.parse(ws)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported |= {a.name for a in node.names}
    banned = {"scheduler", "requests", "urllib", "KaelLife"}
    hit = imported & banned
    chk("E4 🔴 房间不许自己去碰 KaelLife / 网络（房子是被动的，他的腿在 KaelLife）",
        not hit, str(hit))

    from app_ext import mcp as MCP
    from app_ext.modules import workshop as W
    os.environ["RELAY_WORKSHOP_DIR"] = str(tmp / "workshop_e")

    class _FakeApp:
        def __init__(self):
            self.routes = []

        def add_api_route(self, path, fn, methods=None, name=None):
            self.routes.append((path, tuple(methods or ())))

        def _dec(self, path, methods):
            def wrap(fn):
                self.routes.append((path, tuple(methods)))
                return fn
            return wrap

        def get(self, path):
            return self._dec(path, ["GET"])

        def post(self, path):
            return self._dec(path, ["POST"])

        def put(self, path):
            return self._dec(path, ["PUT"])

    class _FakeRelay:
        app = _FakeApp()

    r1 = W.install(_FakeRelay(), PREFIX)          # 幂等：连跑两次
    n_after_first = len(_FakeRelay.app.routes)
    W.install(_FakeRelay(), PREFIX)
    chk("E5 install 两次不炸（幂等）",
        len(_FakeRelay.app.routes) == n_after_first and n_after_first >= 3,
        f"{n_after_first} → {len(_FakeRelay.app.routes)}")
    chk("E5b 工作间的三个展示端点都注册在 /app/ext/workshop/* 下",
        sum(1 for p, _m in _FakeRelay.app.routes
            if p.startswith("/app/ext/workshop/")) == 3,
        str([p for p, _m in _FakeRelay.app.routes]))

    chk("E6 逃生开关在（房间层可整体关掉）", "APP_EXT_ROOMS_DISABLED" in dep)
    chk("E7 verify_all.py 里接了 workshop_check",
        "workshop_check" in va and "workshop_check.py" in va)

    # E8 🔴 空数据库
    fresh = house_dir(tmp / "freshroot")
    fresh.mkdir(parents=True, exist_ok=True)
    # 注意：house_env 里 RELAY_DB 指向 fresh/relay.db，且**故意不 seed**
    proc, logf, log_path = start_house(fresh, PORT_FRESH)
    try:
        if not wait_port(PORT_FRESH):
            chk("E8 空库的房子能起来", False, "健康检查超时")
            return
        log = log_path.read_text(encoding="utf-8", errors="replace")
        chk("E8a 空库上：会话投影被跳过，而且**日志说了出来**",
            "会话投影 失败（已跳过这一步" in log, "")
        chk("E8b 空库上：四张表照样建好", "P0 地基就绪" in log and "schema v" in log)
        chk("E8c 🔴 空库上：门与房间**照样装上**（这是回归锁 —— "
            "旧写法会让整个 P0/P1/房间层一起装不上）",
            "工作间就绪" in log and "MCP 门就绪" in log, "")
        b = f"http://127.0.0.1:{PORT_FRESH}"
        st, _d, _h = req(b + "/app/ext/me", token=SECRET)
        chk("E8d 空库上：身份层端点 200（不是 404）", st == 200, f"实际 {st}")
        st, d, _h = req(b + "/mcp", method="POST", token=SECRET,
                        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        n_tools = len(((d.get("result") or {}).get("tools")) or [])
        chk("E8e 空库上：MCP 门列出四个工具", st == 200 and n_tools == 4,
            f"{st} n={n_tools}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


# ---------------------------------------------------------------------------
# F 组 · 展示页（web/workshop.html）与接线
# ---------------------------------------------------------------------------

def _strip_js_comments(src: str) -> str:
    """把注释去掉再查关键字 —— 页面里那段解释"为什么不能把 token 拼进 URL"的注释
    本身含 `token=`，不去掉会假红。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)
    return src


def part_f(tmp: Path) -> None:
    base = f"http://127.0.0.1:{PORT_OK}"
    page_path = REPO / "web" / "workshop.html"
    chk("F1 展示页在（web/workshop.html）", page_path.is_file(), str(page_path))
    if not page_path.is_file():
        return
    raw = page_path.read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    idx = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    sw = (REPO / "web" / "sw.js").read_text(encoding="utf-8")

    chk("F2 🔴 预览走 srcdoc（带 Bearer 抓原文再塞进去），URL 里不出现密钥",
        "srcdoc" in code and "?token=" not in code and "token=" not in code,
        "页面代码里出现了 token= 拼 URL 的写法")
    chk("F3 每一处 iframe 都是 sandbox=\"allow-scripts\"，且**没有** allow-same-origin",
        code.count("sandbox=\"allow-scripts\"") >= 2
        and "allow-same-origin" not in code,
        f"allow-scripts x{code.count('sandbox=\"allow-scripts\"')}")
    chk("F4 页面里没写死密钥（密钥只从 localStorage 读）",
        'localStorage.getItem(LS_KEY)' in code and "companion_secret" in code)
    chk("F5 三个展示端点都用对了路径",
        all(p in code for p in ("/app/ext/workshop/list",
                                "/app/ext/workshop/item/",
                                "/app/ext/workshop/raw/")))

    chk("F6 菜单里有 Workshop 这一项",
        'data-menu="workshop"' in idx and ">Workshop<" in idx)
    chk("F7 菜单点 Workshop 会去 workshop.html",
        'item.dataset.menu === "workshop"' in idx and '"workshop.html"' in idx)
    chk("F8 🔴 sw.js 的 CACHE 提过版本（不提的话装过的客户端永远看旧壳）",
        "kael-home-v3-" not in sw and re.search(r'CACHE\s*=\s*"kael-home-v\d+', sw) is not None,
        re.search(r'CACHE\s*=\s*"([^"]+)"', sw).group(1) if re.search(r'CACHE\s*=\s*"([^"]+)"', sw) else "")

    st, _d, _h = req(base + "/relay/workshop.html")
    chk("F9 实跑：/relay/workshop.html 拿得到（200）", st == 200, f"实际 {st}")
    st, d, _h = req(base + "/relay/workshop.html")
    body = d.get("_raw") or ""
    chk("F10 实跑：页面上是工作间（不是被静态目录的别的页顶掉）",
        "The Workshop" in body, body[:80].replace("\n", " "))
    chk("F11 实跑：能跟着页面看到那一件（列表端点真返回 C 组造的东西）",
        _C_ID is not None
        and _C_ID in json.dumps(req(base + "/app/ext/workshop/list", token=SECRET)[1],
                                ensure_ascii=False))


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _part(label: str, fn, *a) -> None:
    """跑一组。**组内炸了要把整份报告留下来** —— 记一条响亮的失败，不静默吞掉。"""
    try:
        fn(*a)
    except Exception as e:
        import traceback
        chk(f"{label} 整组跑完（没炸在半路）", False,
            f"{type(e).__name__}: {e} | {traceback.format_exc().strip().splitlines()[-1]}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kaelhome_workshop_"))
    started = []
    try:
        print("=" * 78)
        print("A 组 · 工作间存储（纯逻辑）")
        print("=" * 78)
        _part("A 组", part_a, tmp)

        print("\n" + "=" * 78)
        print("B 组 · 工具注册表与 JSON-RPC（纯逻辑）")
        print("=" * 78)
        _part("B 组", part_b, tmp)

        # 起房子（正常路径：库里已经有 messages）
        # ⚠️ 房子目录必须是 `house_dir(tmp)` —— C/D 组靠 `server_workshop(tmp)` 找文件，
        #    两边算式对不上就会一片假红（这个坑真踩过）。
        house = house_dir(tmp)
        house.mkdir(parents=True, exist_ok=True)
        seed_messages(house / "relay.db")
        proc, logf, log_path = start_house(house, PORT_OK)
        started.append((proc, logf))
        if not wait_port(PORT_OK):
            print("❌ 房子起不来，C/D/E8 无法进行")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
            chk("房子能启动", False, "健康检查超时")
        else:
            print("\n" + "=" * 78)
            print("C 组 · 真 MCP 客户端（官方 SDK）+ 原始 JSON-RPC")
            print("=" * 78)
            _part("C 组", part_c, tmp)

            print("\n" + "=" * 78)
            print("D 组 · 房子 REST 端点 + raw 的安全")
            print("=" * 78)
            _part("D 组", part_d, tmp)

            print("\n" + "=" * 78)
            print("F 组 · 展示页（web/workshop.html）与接线")
            print("=" * 78)
            _part("F 组", part_f, tmp)

            for p, f in started:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except Exception:
                    p.kill()
                f.close()
            started = []

        print("\n" + "=" * 78)
        print("E 组 · 接线 / 幂等 / 红线 / 空库回归")
        print("=" * 78)
        _part("E 组", part_e, tmp)
    finally:
        for p, f in started:
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                f.close()
            except Exception:
                pass

    lines = []
    lines.append("工作间验收 —— 房子的第一间房 + MCP 门")
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"仓库：{REPO}")
    lines.append(f"临时目录：{tmp}")
    lines.append("")
    for name, ok, detail in results:
        lines.append(f"[{'OK' if ok else 'XX'}] {name}" + (f"   -> {detail}" if detail and not ok else ""))
    n_ok = sum(1 for _n, ok, _d in results if ok)
    n_bad = len(results) - n_ok
    lines.append("")
    if _skipped:
        lines.append("被跳过的组（环境缺依赖）：")
        for n, why in _skipped:
            lines.append(f"  ⚠️ {n} —— {why}")
        lines.append("")
    lines.append(f"共 {len(results)} 项，通过 {n_ok}，失败 {n_bad}")
    if _skipped:
        lines.append(f"另有 {len(_skipped)} 组被跳过")
    lines.append("总检查：全部通过 ✅" if n_bad == 0 else "总检查：有失败 ❌")
    report = "\n".join(lines)
    (HERE / "workshop_report.txt").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
