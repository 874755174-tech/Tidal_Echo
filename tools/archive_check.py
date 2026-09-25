#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出 / 快照验收 —— P2-0（房子第一次真"动库"之前的那条安全网）
==========================================================================

## 这一套到底在守什么

导出端点写错，**当天不会有人发现** —— 它平时不用，只在"她真要拿走东西"那天用。
所以这里不是"跑通就完事"，而是把三类**只在那一天才暴露**的错误钉死：

  ① **备份了却打不开 / 少了一截** —— 裸拷 `.db` 在 WAL 下会拿到半截。
     → A 组**故意把库开成 WAL**，并要求快照能被**另一个连接独立打开**、
        表名行数与源一致、`PRAGMA integrity_check` = ok。
  ② **导出把库弄坏了 / 导出期间写不进去** ——
     → A 组断言导出后源库**一字未动**；B 组断言"导出一次 → 写一条 → 再导出"能多出那一行。
  ③ **密钥从 URL 漏出去** —— 下载最顺手的写法就是 `<a href="…?token=…">`。
     → B 组要求 `?token=` 被**明确拒绝**（400）；D 组要求页面里没有那种写法。

另外两条"这不是房间"的证据也在 C 组：**只注册 GET**、**不 import mcp**。

## 覆盖清单

  A. 纯逻辑（不起 HTTP）
     1-2   快照能被**独立** sqlite 打开；表名与源一致
     3-4   行数一致；`integrity_check` = ok
     5     🔴 一行正文逐字一致（不是"行数对了就行"）
     6     🔴 导出后**源库一字未动**
     7     🔴 WAL 模式下（有未 checkpoint 的写入）仍然一致
     8     库不存在 → 明确报错（不是静默给个空文件）
     9-10  jsonl 首行 `_meta`；表清单里 `memories` 在（哪怕 0 行）
     9b    🔴 每条**以换行结尾**（粘成一行 = grep / wc -l 全废 —— 09-19 真踩过）
     11    🔴 能 grep 到一条已知消息
     12    `meta` 列摊开成对象（人能读）
     13    🔴 **正文恰好长得像 JSON 时，不许被摊开**（内容被改是看不出来的）
     14    `users.secret_hash` 不进人读记录
     15    `push_subscriptions` 不进人读记录（.db 里在）
     16    文件抽屉里真有他的东西（arcname 形状）
     17-18 目录不存在 / 空目录 → 仍是**合法** tar.gz（不崩）

  B. HTTP 层（起真房子）
     1-2   info 无密钥 401；有密钥 200 且三个面都在
     3-4   /db 无密钥 401；有密钥 200 + `attachment` + 是 SQLite 文件头
     5     🔴 下载下来的那一份能被独立打开，且与源一致
     6-7   /jsonl 能 grep 到那条已知消息；/files 是合法 gzip+tar
     6b    🔴 **下载件**一行一条：换行数 == 记录数、每行可 parse、末行有换行
     8-9   🔴 `?token=` → **400**（本端点有意与房子别处不同）；错密钥 401
     10    🔴 只读：POST /db → 405
     11    🔴 导出期间写不受影响（导出→写→再导出，行数 +1）
     12-13 响应头 `no-store` / `nosniff`；临时目录不残留

  C. 接线 / 幂等 / 红线 / 空库
     1-3   register 摘要含 archive；`_ROUTES` 四条；install 幂等
     4     🔴 **只注册 GET**（这个文件里没有写方法）
     5     🔴 **不 import mcp / 不碰 KaelLife / 不走网络**（它不是房间）
     6-8   逃生开关在；verify_all 里接了本套
     9-11  🔴 **空库上照样装得上、导得出**（"出事那天"最常见的样子）

  D. 页面（web/archive.html）与接线
     1-4   页面在；🔴 URL 里不出现密钥；下载走 fetch + Blob；
            端点路径由后端 `faces` 给 —— 页面里不另抄一份
     5-6   页面明说「不做导入」；菜单里有 Archive 并真的路由过去
     7      sw.js 提过缓存版本
     8-9   实跑：`/relay/archive.html` 200 且是存档页

⚠️ 本套用自己的端口 8796 / 8797（不跟 secaudit 的 8080、工作间的 8798/8799 抢）。
⚠️ B/D 组找文件的路径一律走 `server_workshop_dir()` —— 算式只留一处，
   别再手抄 `tmp/"workshop"`（那个假红在 09-18 吃过一次）。

用法：.venv\\Scripts\\python.exe tools\\archive_check.py
"""
import io
import json
import os
import re
import shutil
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

import tarfile
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

SECRET = "test-secret-archive-0123456789"
PREFIX = "/relay"
PORT_OK = 8796        # 有数据的库（正常路径）
PORT_FRESH = 8797     # 全新的库（空 /data 那种）

CANARY = "盐系手帐风，米白底细线分隔"
CANARY_JSON = '{"这条正文长得像 JSON": true}'

results: list = []
_skipped: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


# ---------------------------------------------------------------------------
# 临时快照目录：三个小工具
# ---------------------------------------------------------------------------
#
# 🔴 为什么值得单开一段（2026-09-25 真踩）：
#    导出走 `FileResponse(..., background=BackgroundTask(_cleanup, td))` —— 清理是
#    **响应体发完之后**才跑的。于是有两种失败方式，长得却一模一样：
#      ① 我们自己漏了（清理根本没跑 / 跑失败了）→ 该红
#      ② **上一轮**跑挂了留下的孤儿还在 → 这一轮**假红**
#    旧写法（扫全局临时目录里还有没有 `kael-archive-*`）两种都报红 ⇒ 全量验收里
#    第 10 套莫名其妙红了一条，说的却是"上一轮没扫干净"。
#    ⇒ 所以：**每次断言只认"这次新建的"**，并且**开跑前先扫掉上一轮的孤儿**。

_SNAP_RE = re.compile(r"^kael-archive-[a-z0-9_]{8}$")   # tempfile.mkdtemp 的形状


def _snap_dirs() -> set:
    """当前系统临时目录里**我们这套建的**快照目录。"""
    return {p for p in Path(tempfile.gettempdir()).iterdir()
            if _SNAP_RE.match(p.name)}


def sweep_orphan_snaps() -> int:
    """开跑前扫掉上一轮留下的孤儿快照目录，返回扫掉几个。

    🔴 只删**我们自己**建的（前缀 + `mkdtemp` 那 8 位形状都对得上），
       且都在系统临时目录里 —— 删它们零风险。不清的话孤儿会一轮一轮堆下去，
       而且每一轮都把下一轮的 B13 染红。
    """
    gone = 0
    for p in _snap_dirs():
        try:
            shutil.rmtree(p, ignore_errors=True)
            gone += 1
        except Exception:
            pass
    return gone


def new_snaps_since(before: set) -> list:
    """这次调用**新建**的快照目录（已在 `before` 里的不算）。"""
    return sorted(p for p in _snap_dirs() if p not in before)


def _wait_snap_gone(dirs, timeout: float = 6.0) -> list:
    """等这批快照目录被清掉（清理是异步的）。返回超时后**还在的**那些。

    🔴 返回的必须是**还在的**，不能返回"等过就算过了" —— 那样这条断言就成了摆设。
    """
    dirs = list(dirs)
    t0 = time.time()
    while time.time() - t0 < timeout:
        left = [p for p in dirs if p.exists()]
        if not left:
            return []
        time.sleep(0.2)
    return [p for p in dirs if p.exists()]


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def req(url, *, method="GET", token=None, body=None, timeout=30):
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
                return resp.status, json.loads(raw or "{}"), dict(resp.headers)
            except Exception:
                return resp.status, {"_raw": raw}, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}"), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)
    except Exception as e:
        return -1, {"_raw": f"{type(e).__name__}: {e}"}, {}


def req_bytes(url, *, method="GET", token=None, timeout=60):
    """拿**原始字节** —— 导出物是二进制（.db / .tar.gz），先 decode 再看就晚了。"""
    r = urllib.request.Request(url, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}".encode(), {}


def hget(headers: dict, name: str) -> str:
    """大小写无关地取一个响应头。

    ⚠️ Starlette 发出来的头是**全小写**的，而 `dict(resp.headers)` 大小写敏感 ——
    直接 `h["Content-Disposition"]` 会取不到，看着像"头没发"，其实发了。
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


# ---------------------------------------------------------------------------
# 房子（路径算式只留一处）
# ---------------------------------------------------------------------------

def house_dir(tmp: Path) -> Path:
    return tmp / "house"


def workshop_under(home: Path) -> Path:
    return home / "workshop"


def uploads_under(home: Path) -> Path:
    return home / "uploads"


def server_workshop_dir(tmp: Path) -> Path:
    """服务器进程眼里 `RELAY_WORKSHOP_DIR` 到底是哪个目录。"""
    return workshop_under(house_dir(tmp))


def server_uploads_dir(tmp: Path) -> Path:
    return uploads_under(house_dir(tmp))


def house_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "RELAY_DB": str(home / "relay.db"),
        "RELAY_SECRET": SECRET,
        "RELAY_HUMAN_NAME": "Lily",
        "RELAY_PUBLIC_PREFIX": PREFIX,
        "RELAY_BACKEND_DIR": str(BACKEND),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "RELAY_UPLOAD_DIR": str(uploads_under(home)),
        "RELAY_WORKSHOP_DIR": str(workshop_under(home)),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        # 供应商全关掉 —— 这一套不验模型网关，别让它去碰网络
        "PROVIDERS_DISABLED": "deepseek,siliconflow,openai,anthropic,gemini,relay",
    })
    return env


def start_house(home: Path, port: int):
    env = house_env(home)
    log_path = home / f"uvicorn-{port}.log"
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(port),
         "--app-dir", str(DEPLOY)],
        env=env, stdout=logf, stderr=subprocess.STDOUT)
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


def seed_messages(db_path: Path) -> None:
    """造一张和后端一模一样的 messages 表 + 几条消息。

    `app_ext.register()` 跑在 lifespan 之前 —— 只有"库里已经有 messages"时，
    会话投影那一步才会成功。这里要跑**正常路径**。
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
    conn.executemany(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        [("2026-09-19T12:00:00+08:00", "in", "user", CANARY, "{}"),
         ("2026-09-19T12:00:05+08:00", "in", "user", CANARY_JSON, "{}"),
         ("2026-09-19T12:00:09+08:00", "out", "reply", "好，记下了",
          '{"model": "opus", "truncated": false}')])
    conn.commit()
    conn.close()


def _msg_count_of_blob(blob: bytes) -> int:
    """把下载下来的 `.db` 字节写进临时文件、独立打开、数 messages 行数。

    🔴 必须落盘再打开 —— `sqlite3.connect` 要一个文件路径。
       （用 `:memory:` + `deserialize` 也行，但落盘更接近她真实拿到的那个东西。）
    """
    p = Path(tempfile.mkdtemp(prefix="kael-arch-n-")) / "n.db"
    p.write_bytes(blob)
    c = sqlite3.connect(str(p))
    try:
        return int(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        c.close()


def seed_workshop_and_uploads(home: Path) -> None:
    ws = workshop_under(home) / "w1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "index.html").write_text("<h1>他的第一件东西</h1>", encoding="utf-8")
    up = uploads_under(home)
    up.mkdir(parents=True, exist_ok=True)
    (up / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 30)


# ---------------------------------------------------------------------------
# A 组 · 纯逻辑
# ---------------------------------------------------------------------------

def part_a(tmp: Path) -> None:
    from app_ext import archive as A

    root = tmp / "a"
    root.mkdir(parents=True, exist_ok=True)
    db = root / "relay.db"

    conn = sqlite3.connect(str(db))
    # 🔴 故意开 WAL —— 裸拷 `.db` 在这种模式下会拿到半截（这正是"必须走
    #    Connection.backup()"的原因）。这一组要在 WAL 下验。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT NOT NULL,
            direction TEXT NOT NULL,
            kind      TEXT NOT NULL,
            text      TEXT NOT NULL,
            meta      TEXT NOT NULL DEFAULT '{}'
        )""")
    conn.executemany(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        [("2026-09-19T12:00:00+08:00", "in", "user", CANARY, "{}"),
         ("2026-09-19T12:00:05+08:00", "in", "user", CANARY_JSON, "{}"),
         ("2026-09-19T12:00:09+08:00", "out", "reply", "好，记下了",
          '{"model": "opus"}')])
    conn.execute("""
        CREATE TABLE users (
            id          TEXT PRIMARY KEY,
            handle      TEXT UNIQUE,
            display_name TEXT,
            secret_hash TEXT NOT NULL,
            role        TEXT DEFAULT 'owner',
            created     TEXT NOT NULL
        )""")
    conn.execute("INSERT INTO users VALUES ('u_owner','lily','Lily','PBKDF2-HASH-CANARY','owner','2026-09-01')")
    conn.execute("""
        CREATE TABLE push_subscriptions (
            endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
            ua TEXT, created TEXT NOT NULL, last_ok TEXT
        )""")
    conn.execute("INSERT INTO push_subscriptions VALUES ('https://push/x','k','a','UA','2026-09-01',NULL)")
    conn.commit()
    # 🔴 留一个**未 checkpoint** 的写入：WAL 里还有没落进主库的数据。
    #    裸拷 .db 在这一刻拿到的就是"少了这几行"的半截。
    conn.execute("INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
                 ("2026-09-19T12:01:00+08:00", "in", "user", "这一条还在 WAL 里", "{}"))
    conn.commit()
    src_rows_before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    class R:
        DB_PATH = str(db)

    rep = A.snapshot_to(R(), root / "snap.db")
    snap = root / "snap.db"

    chk("A1 快照产出了文件，且自报一致",
        snap.is_file() and rep.get("consistent") is True and rep.get("ok") is True,
        json.dumps(rep, ensure_ascii=False)[:200])

    # 独立连接打开（不是复用源连接）—— "备份了却打不开"就死在这一步
    ok_open, names_src, names_snap, rows_snap = False, set(), set(), {}
    try:
        s = sqlite3.connect(str(snap))
        names_snap = {r[0] for r in s.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        for n in names_snap:
            rows_snap[n] = s.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
        s.close()
        ok_open = True
    except Exception as e:
        chk("A1b 快照能被**独立** sqlite 打开", False, f"{type(e).__name__}: {e}")
    if ok_open:
        src = sqlite3.connect(str(db))
        names_src = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        src.close()
        chk("A1b 🔴 快照能被**独立** sqlite 打开", True, str(sorted(names_snap)))
        chk("A2 表名与源一致（不是只拷了主表）",
            names_src == names_snap, f"源{len(names_src)} 快照{len(names_snap)}")
        chk("A3 🔴 行数与源一致（WAL 里那条也在）",
            rows_snap.get("messages") == src_rows_before,
            f"快照 {rows_snap.get('messages')} / 源 {src_rows_before}")
    chk("A4 `integrity_check` = ok", rep.get("integrity") == "ok", str(rep.get("integrity")))

    # 一行正文逐字一致
    txt = None
    try:
        s = sqlite3.connect(str(snap))
        txt = s.execute("SELECT text FROM messages WHERE text = ?", (CANARY,)).fetchone()
        s.close()
    except Exception:
        pass
    chk("A5 🔴 正文逐字一致（不是「行数对了就行」）",
        bool(txt) and txt[0] == CANARY, str(txt))

    # 源库一字未动
    src = sqlite3.connect(str(db))
    n_now = src.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    canary_now = src.execute("SELECT text FROM messages WHERE id = 1").fetchone()
    src.close()
    chk("A6 🔴 导出之后源库一字未动（只读操作在代码层面也是只读）",
        n_now == src_rows_before and bool(canary_now) and canary_now[0] == CANARY,
        f"行数 {src_rows_before} → {n_now}")
    chk("A7 🔴 WAL + 未 checkpoint 的写入下仍然一致（裸拷在这里就死了）",
        rep.get("consistent") is True and rows_snap.get("messages") == src_rows_before)

    # 库不存在
    class R2:
        DB_PATH = str(root / "nope.db")

    try:
        A.snapshot_to(R2(), root / "x.db")
        chk("A8 库不存在 → 明确报错", False, "居然没报错")
    except FileNotFoundError:
        chk("A8 库不存在 → 明确报错（不是静默给个空文件）", True)
    except Exception as e:
        chk("A8 库不存在 → 明确报错", False, f"抛了别的：{type(e).__name__}: {e}")

    # jsonl
    lines = list(A.iter_jsonl(R()))
    meta = json.loads(lines[0]).get("_meta") if lines else None
    chk("A9 jsonl 首行是 `_meta`，带生成时间与表清单",
        bool(meta) and "generated" in meta and isinstance(meta.get("tables"), list),
        (lines[0][:120] if lines else "(空)"))
    no_lf = [i for i, ln in enumerate(lines) if not ln.endswith("\n")]
    chk("A9b 🔴 jsonl 每条**以换行结尾**（少了它 N 条粘成一整行，grep / wc -l 全废）",
        bool(lines) and not no_lf,
        f"共 {len(lines)} 条，缺换行结尾的 {len(no_lf)} 条 {no_lf[:5]}")
    tab_names = {t["name"] for t in (meta or {}).get("tables", [])}
    chk("A10 `_meta` 的表清单 = EXPORT_TABLES ∩ 库里真有的表（本例 = messages/users）",
        tab_names == {"messages", "users"}, str(sorted(tab_names)))
    chk("A11 🔴 能 grep 到一条已知消息",
        any(CANARY in ln for ln in lines), f"共 {len(lines)} 行")
    body_lines = [json.loads(ln) for ln in lines[1:]]
    msg_rows = [r for r in body_lines if r.get("_table") == "messages"]
    reply = [r for r in msg_rows if r.get("kind") == "reply"]
    chk("A12 `meta` 列摊开成对象（人能读）",
        bool(reply) and isinstance(reply[0].get("meta"), dict),
        str(reply[0].get("meta") if reply else None))
    json_text_row = [r for r in msg_rows if r.get("text") == CANARY_JSON]
    chk("A13 🔴 正文恰好长得像 JSON 时**不被摊开**（摊开 = 悄悄改了内容）",
        bool(json_text_row) and isinstance(json_text_row[0].get("text"), str),
        str(json_text_row[0].get("text") if json_text_row else None)[:60])
    user_rows = [r for r in body_lines if r.get("_table") == "users"]
    chk("A14 `users.secret_hash` 不进人读记录",
        bool(user_rows) and all("secret_hash" not in r for r in user_rows))
    chk("A15 `push_subscriptions` 不进人读记录（.db 快照里完整保留）",
        not any(r.get("_table") == "push_subscriptions" for r in body_lines)
        and "push_subscriptions" in rows_snap)

    # 文件抽屉
    ws = root / "ws" / "w1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")
    old_ws = os.environ.get("RELAY_WORKSHOP_DIR")
    old_up = os.environ.get("RELAY_UPLOAD_DIR")

    def _restore_env():
        if old_ws is None:
            os.environ.pop("RELAY_WORKSHOP_DIR", None)
        else:
            os.environ["RELAY_WORKSHOP_DIR"] = old_ws
        if old_up is None:
            os.environ.pop("RELAY_UPLOAD_DIR", None)
        else:
            os.environ["RELAY_UPLOAD_DIR"] = old_up

    try:
        os.environ["RELAY_WORKSHOP_DIR"] = str(root / "ws")
        os.environ["RELAY_UPLOAD_DIR"] = str(root / "nope_uploads")   # 故意不存在
        tg = root / "files.tar.gz"
        st = A.make_files_tar(tg)
        with tarfile.open(str(tg)) as tf:
            names = tf.getnames()
        chk("A16 文件抽屉里真有他的东西（arcname 形状 = workshop/…）",
            "workshop/w1/index.html" in names, str(names))
        chk("A17 其中一半目录不存在时也产出**合法** tar.gz（不崩）", st.get("ok") is True)
    except Exception as e:
        chk("A16 文件抽屉里真有他的东西", False, f"{type(e).__name__}: {e}")
        chk("A17 一半目录不存在时也产出合法 tar.gz", False, str(e))

    # 空目录 → 仍是合法 tar
    try:
        os.environ["RELAY_WORKSHOP_DIR"] = str(root / "empty_ws")
        os.environ["RELAY_UPLOAD_DIR"] = str(root / "empty_up")
        tg2 = root / "empty.tar.gz"
        A.make_files_tar(tg2)
        with tarfile.open(str(tg2)) as tf:
            n = len(tf.getnames())
        chk("A18 空目录 → 仍然是合法 tar（0 个条目），不是坏文件", n == 0, f"条目 {n}")
    except Exception as e:
        chk("A18 空目录 → 仍然是合法 tar", False, f"{type(e).__name__}: {e}")
    finally:
        _restore_env()


# ---------------------------------------------------------------------------
# B 组 · HTTP（起真房子）
# ---------------------------------------------------------------------------

def part_b(tmp: Path, base: str) -> None:
    A_INFO = base + "/app/ext/archive/info"
    A_DB = base + "/app/ext/archive/db"
    A_JSONL = base + "/app/ext/archive/jsonl"
    A_FILES = base + "/app/ext/archive/files"
    # 🔴 记下"进 B 组之前就有哪些快照目录"。B13 只认**之后新建的** ——
    #    否则任何一处遗留的孤儿都会把它染红（而那说的不是我们这套坏了）。
    snaps_before = _snap_dirs()

    st, d, _h = req(A_INFO)
    chk("B1 info 无密钥 → 401", st == 401, f"实际 {st}")

    st, d, _h = req(A_INFO, token=SECRET)
    faces = (d or {}).get("faces") or []
    chk("B2 info 有密钥 → 200，三个面都在（db / jsonl / files）",
        st == 200 and {f.get("id") for f in faces} == {"db", "jsonl", "files"},
        f"{st} {[f.get('id') for f in faces]}")

    st, _b, _h = req_bytes(A_DB)
    chk("B3 /db 无密钥 → 401", st == 401, f"实际 {st}")

    st, body, h = req_bytes(A_DB, token=SECRET)
    cd = hget(h, "Content-Disposition")
    chk("B4 /db → 200 + attachment + 真正的 SQLite 文件头",
        st == 200 and "attachment" in cd.lower() and body[:16].startswith(b"SQLite format 3"),
        f"{st} cd={cd[:60]} head={body[:16]!r}")

    saved = Path(tempfile.mkdtemp(prefix="kael-arch-b-")) / "down.db"
    saved.write_bytes(body)
    ok_open, mismatch = False, ""
    try:
        s = sqlite3.connect(str(saved))
        names = {r[0] for r in s.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        n_msg = s.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        canary = s.execute("SELECT text FROM messages WHERE text = ?", (CANARY,)).fetchone()
        integ = s.execute("PRAGMA integrity_check").fetchone()[0]
        s.close()
        ok_open = True
        src = sqlite3.connect(str(house_dir(tmp) / "relay.db"))
        n_src = src.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        src.close()
        if n_msg != n_src:
            mismatch = f"行数 {n_msg} != 源 {n_src}"
        elif not canary:
            mismatch = "那条已知消息不在里面"
        elif integ != "ok":
            mismatch = f"integrity={integ}"
        chk("B5 🔴 **下载下来的那一份**能被独立打开、与源一致、正文在",
            ok_open and not mismatch, mismatch or str(sorted(names)))
    except Exception as e:
        chk("B5 🔴 下载下来的那一份能被独立打开", False, f"{type(e).__name__}: {e}")

    st, _b, h = req_bytes(A_JSONL, token=SECRET)
    text = _b.decode("utf-8", "replace")
    chk("B6 /jsonl 能 grep 到那条已知消息（人可读的那一份真能读）",
        st == 200 and CANARY in text and '"_meta"' in text, f"{st} {len(_b)} 字节")
    # 🔴 上面那条只做子串搜索 —— 记录粘成一行它照样绿。真正的 JSONL 契约在这里：
    http_lines = [x for x in text.split("\n") if x.strip()]
    parse_ok = True
    for x in http_lines:
        try:
            json.loads(x)
        except Exception:
            parse_ok = False
            break
    chk("B6b 🔴 **下载下来的那一份**一行一条：换行数 == 记录数，每行都能 parse，末行有换行",
        st == 200 and parse_ok and text.endswith("\n")
        and text.count("\n") == len(http_lines) and len(http_lines) >= 2,
        f"换行 {text.count(chr(10))} / 非空行 {len(http_lines)} / "
        f"末行有换行 {text.endswith(chr(10))} / 每行可 parse {parse_ok}")
    st, _b, h = req_bytes(A_FILES, token=SECRET)
    got = []
    try:
        with tarfile.open(fileobj=io.BytesIO(_b), mode="r:gz") as tf:
            got = tf.getnames()
    except Exception as e:
        got = [f"!! {type(e).__name__}: {e}"]
    chk("B7 /files → 200 + 合法 gzip+tar，里面真装着工作间与上传",
        st == 200 and _b[:2] == b"\x1f\x8b" and any("workshop/w1/index.html" == n for n in got),
        f"{st} {got}")

    st, d, _h = req(A_INFO + "?token=" + SECRET)
    chk("B8 🔴 `?token=` → **400**（导出端点有意与房子别处不同：密钥不许拼进 URL）",
        st == 400 and "token_in_query_not_allowed" in json.dumps(d),
        f"实际 {st} {json.dumps(d, ensure_ascii=False)[:80]}")
    st, _b, _h = req_bytes(A_DB, token="WRONG")
    chk("B9 错密钥 → 401", st == 401, f"实际 {st}")

    st, _b, _h = req_bytes(A_DB, method="POST", token=SECRET)
    chk("B10 🔴 只读：POST /db → 405（这个文件里没有写方法）", st == 405, f"实际 {st}")

    # 🔴 导出期间写不受影响：先导出一次（记下行数）→ 往库里写一条 → 再导出
    srcp = house_dir(tmp) / "relay.db"
    st1, b1, _h = req_bytes(A_DB, token=SECRET)
    n_before = _msg_count_of_blob(b1)
    conn = sqlite3.connect(str(srcp))
    conn.execute("INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
                 ("2026-09-19T12:05:00+08:00", "in", "user", "导出之后写进来的一条", "{}"))
    conn.commit()
    conn.close()
    st2, b2, _h = req_bytes(A_DB, token=SECRET)
    n_after = _msg_count_of_blob(b2)
    c = sqlite3.connect(str(srcp))
    n_src = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    c.close()
    chk("B11 🔴 导出期间写不受影响（再导一次就把新那条带上了）",
        st1 == 200 and st2 == 200 and n_after == n_src and n_after == n_before + 1,
        f"第一次 {n_before} · 库里 {n_src} · 第二次 {n_after}")

    st, body, h = req_bytes(A_DB, token=SECRET)
    chk("B12 响应头 no-store / nosniff（导出物不许被中间层留下）",
        "no-store" in hget(h, "Cache-Control").lower()
        and "nosniff" in hget(h, "X-Content-Type-Options").lower(),
        f"cache={hget(h,'Cache-Control')} nosniff={hget(h,'X-Content-Type-Options')}")

    # 🔴 B13 只认**这次调用新建的**快照目录。
    #    旧写法扫的是"全局临时目录里还有没有 `kael-archive-*`" ⇒ 只要**上一轮**跑
    #    留下过一个孤儿（见 C 组那段注释：那是真发生过的事），这一条就**假红**，
    #    而它说的不是"我们这套坏了"，是"上一轮没扫干净"。验收假红和假绿一样坏。
    left = _wait_snap_gone(new_snaps_since(snaps_before))
    chk("B13 临时快照不残留（BackgroundTask 清理）", not left, str(left[:3]))


# ---------------------------------------------------------------------------
# C 组 · 接线 / 幂等 / 红线 / 空库
# ---------------------------------------------------------------------------

def part_c(tmp: Path) -> None:
    dep = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    arc = (DEPLOY / "app_ext" / "archive.py").read_text(encoding="utf-8")
    va = (HERE / "verify_all.py").read_text(encoding="utf-8")

    chk("C1 register 摘要里有 archive 这一项",
        '"archive"' in dep and 'summary["archive"]' in dep)
    chk("C2 `_ROUTES` 里列了四条导出路由",
        all(p in dep for p in ("/app/ext/archive/info", "/app/ext/archive/db",
                               "/app/ext/archive/jsonl", "/app/ext/archive/files")))
    chk("C3 逃生开关在（导出可单独关掉）", "APP_EXT_ARCHIVE_DISABLED" in dep)

    code = re.sub(r"#.*$", "", arc, flags=re.M)          # 去掉行注释再查装饰器
    gets = len(re.findall(r'\.app\.get\(', code))
    others = len(re.findall(r'\.app\.(post|put|patch|delete)\(', code))
    chk("C4 🔴 **只注册 GET**（导出没有写方法 —— 导入明确不做）",
        gets == 4 and others == 0, f"GET {gets} / 写方法 {others}")

    imported = set()
    for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", arc, flags=re.M):
        imported.add(m.group(1).split(".")[0])
    banned = {"scheduler", "requests", "urllib", "KaelLife", "mcp"}
    hit = imported & banned
    chk("C5 🔴 不 import mcp / 不碰 KaelLife / 不走网络（它不是房间）",
        not hit, str(sorted(hit)))

    from app_ext import archive as A

    class _FakeApp:
        def __init__(self):
            self.routes = []

        def _dec(self, path, methods):
            def wrap(fn):
                self.routes.append((path, tuple(methods)))
                return fn
            return wrap

        def get(self, path):
            return self._dec(path, ["GET"])

    class _FakeRelay:
        app = _FakeApp()

    A._INSTALLED = False
    A.install(_FakeRelay(), PREFIX)
    n1 = len(_FakeRelay.app.routes)
    A.install(_FakeRelay(), PREFIX)
    chk("C6 install 幂等（第二次什么都不做）",
        len(_FakeRelay.app.routes) == n1 and n1 == 4, f"{n1} → {len(_FakeRelay.app.routes)}")
    chk("C7 四条路由都在 /app/ext/archive/* 下",
        all(p.startswith("/app/ext/archive/") for p, _m in _FakeRelay.app.routes),
        str([p for p, _m in _FakeRelay.app.routes]))
    chk("C8 verify_all.py 里接了 archive_check",
        "archive_check" in va and "archive_check.py" in va)

    # 🔴 空库（"出事那天"最常见的样子）
    fresh = house_dir(tmp / "freshr")
    fresh.mkdir(parents=True, exist_ok=True)
    proc, logf, log_path = start_house(fresh, PORT_FRESH)
    try:
        if not wait_port(PORT_FRESH):
            chk("C9 空库房子能起来", False, "健康检查超时")
            return
        log = log_path.read_text(encoding="utf-8", errors="replace")
        chk("C9 空库上：导出层照样装上（日志说了）", "导出就绪" in log, "")
        b = f"http://127.0.0.1:{PORT_FRESH}{PREFIX}"
        st, d, _h = req(b + "/app/ext/archive/info", token=SECRET)
        chk("C10 空库上：info 200（不是 404）", st == 200, f"实际 {st}")
        # 🔴 先记下"这次之前有哪些快照目录"，才能只认这次新建的那一个。
        snaps_c = _snap_dirs()
        st, body, h = req_bytes(b + "/app/ext/archive/db", token=SECRET)
        ok = False
        try:
            p = Path(tempfile.mkdtemp(prefix="kael-arch-c-")) / "f.db"
            p.write_bytes(body)
            s = sqlite3.connect(str(p))
            s.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            s.close()
            ok = True
        except Exception:
            ok = False
        # 🔴🔴 **先等清理落地，再关房子**（2026-09-25 真踩，每跑一轮留一个孤儿）：
        #    清理挂在 `BackgroundTask` 上 = **响应体发完之后**才跑；而下面 `finally`
        #    里的 `stop_house()` 是立刻 terminate 那个 uvicorn 进程 —— 两者**赛跑**，
        #    输了就留下一个 `kael-archive-*` 目录。它不是产品缺陷（线上没人下载完
        #    就掐进程），是**这套验收自己制造的垃圾**：每跑一轮留一个，攒够了就把
        #    下一轮的 B13 染红。⇒ 这里等它落地（顺带把"清理真的会跑"也验了）。
        left_c = _wait_snap_gone(new_snaps_since(snaps_c))
        chk("C11 🔴 空库上也能导出（第一次真动库之前，这条路就得能用）+ 那次快照随后被清掉",
            st == 200 and ok and not left_c,
            f"{st} {len(body)} 字节 leftover={[p.name for p in left_c][:2]}")
    finally:
        stop_house(proc, logf)


# ---------------------------------------------------------------------------
# D 组 · 页面与接线
# ---------------------------------------------------------------------------

def _strip_js_comments(src: str) -> str:
    """去掉注释再查关键字 —— 页面里那段"为什么不能把 token 拼进 URL"的注释
    本身含 `token=`，不去掉会假红。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)
    return src


def part_d(tmp: Path, base: str) -> None:
    page = REPO / "web" / "archive.html"
    chk("D1 存档页在（web/archive.html）", page.is_file(), str(page))
    if not page.is_file():
        return
    raw = page.read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    idx = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    sw = (REPO / "web" / "sw.js").read_text(encoding="utf-8")

    chk("D2 🔴 密钥不进 URL（没有 ?token= 这种写法）",
        "token=" not in code, "页面代码里出现了 token= 拼 URL 的写法")
    chk("D3 下载走 fetch + Blob（URL.createObjectURL + a.download）",
        "createObjectURL" in code and "download" in code and "authHeaders" in code)
    chk("D4 页面只硬编码 info 一条；其余三个面的路径由后端 `faces` 给（单一真相源）",
        "/app/ext/archive/info" in code and "face.path" in code
        and "/app/ext/archive/db" not in code,
        "页面自己另抄了一份端点路径 —— 后端改了名字页面不会跟着改")
    chk("D5 页面上明说「不做导入」（免得她以后找不到而以为漏了）",
        "导入" in raw and "没有导入" in raw)
    chk("D6 菜单里有 Archive 这一项",
        'data-menu="archive"' in idx and ">Archive<" in idx)
    chk("D7 菜单点 Archive 会去 archive.html",
        'item.dataset.menu === "archive"' in idx and '"archive.html"' in idx)
    chk("D8 🔴 sw.js 的 CACHE 提过版本（不提的话装过的客户端永远看旧壳）",
        "v4-workshop" not in sw and re.search(r'CACHE\s*=\s*"kael-home-v\d+', sw) is not None,
        (re.search(r'CACHE\s*=\s*"([^"]+)"', sw).group(1)
         if re.search(r'CACHE\s*=\s*"([^"]+)"', sw) else ""))

    st, d, _h = req(base + "/archive.html")
    body = (d.get("_raw") or "")
    chk("D9 实跑：/relay/archive.html 拿得到（200）", st == 200, f"实际 {st}")
    chk("D10 实跑：页面上就是存档页（没被别的页顶掉）",
        "The Archive" in body, body[:80].replace("\n", " "))


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kael-archcheck-"))
    # 🔴 开跑前先扫掉**上一轮**留下的孤儿快照（见 `sweep_orphan_snaps`）：
    #    不清的话它们会一轮一轮堆下去，而且每一轮都把这一轮的 B13 染红。
    swept = sweep_orphan_snaps()
    if swept:
        print(f"[清扫] 上一轮残留的临时快照目录 {swept} 个（系统临时目录里，已删）")
    started = []
    try:
        print("=" * 78)
        print("A 组 · 纯逻辑（快照一致性 / jsonl / 文件抽屉）")
        print("=" * 78)
        part_a(tmp)

        home = house_dir(tmp)
        home.mkdir(parents=True, exist_ok=True)
        seed_messages(home / "relay.db")
        seed_workshop_and_uploads(home)

        print("\n" + "=" * 78)
        print("B 组 · HTTP（起真房子）")
        print("=" * 78)
        proc, logf, _log = start_house(home, PORT_OK)
        started.append((proc, logf))
        base = f"http://127.0.0.1:{PORT_OK}{PREFIX}"
        if not wait_port(PORT_OK):
            chk("房子起得来", False, "健康检查超时")
        else:
            part_b(tmp, base)

            print("\n" + "=" * 78)
            print("D 组 · 页面与接线（实跑）")
            print("=" * 78)
            part_d(tmp, base)
    finally:
        for p, f in started:
            stop_house(p, f)

    print("\n" + "=" * 78)
    print("C 组 · 接线 / 幂等 / 红线 / 空库")
    print("=" * 78)
    try:
        part_c(tmp)
    except Exception as e:
        chk("C 组跑完", False, f"{type(e).__name__}: {e}")

    lines = []
    lines.append("导出 / 快照验收 —— P2-0")
    lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"仓库：{REPO}")
    lines.append(f"临时目录：{tmp}")
    lines.append("")
    for name, ok, detail in results:
        lines.append(f"[{'OK' if ok else 'XX'}] {name}"
                     + (f"   -> {detail}" if detail and not ok else ""))
    n_ok = sum(1 for _n, ok, _d in results if ok)
    n_bad = len(results) - n_ok
    lines.append("")
    lines.append(f"共 {len(results)} 项，通过 {n_ok}，失败 {n_bad}")
    lines.append("总检查：全部通过 ✅" if n_bad == 0 else "总检查：有失败 ❌")
    report = "\n".join(lines)
    (HERE / "archive_report.txt").write_text(report, encoding="utf-8")
    print("\n" + report)
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
