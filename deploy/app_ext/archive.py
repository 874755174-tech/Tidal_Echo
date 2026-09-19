#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2-0 · 导出 / 快照 —— 房子里的"能完整取走"的那条路
==========================================================================

## 它是干什么的

让 Lily **能把她自己的东西整份拿走**：一条完整快照（`.db`）、一份人能读的记录
（`.jsonl`）、一个装着**他的作品**的文件抽屉（`.tar.gz`）。

## 🔴 为什么它排在 P2 的第一件（不是"顺手做做"）

P2 是房子**第一次真动库**（⑧ 改读法 / ⑨ 改写库 / ⑩ 建写入通道）。
**动库之前先有一条"能完整取走数据"的路** —— 这本就是既有硬规则
「任何迁移 / 升级前先导出备份」的适用场合。

而且 ⑧ 的验收写着"原文仍在库里可查" —— 但"库里有"对 Lily **没有意义**，
她要的是**她能拿到**。这条端点就是把那句话变成**可验证的**。

## 三条边界（写死，别越界）

1. **导出 ≠ 备份**。真兜底仍是三层：Zeabur 卷 + iCloud 整机备份 + 定期导出。
   这里给的是"人能拿走的那一份"。
2. **导出 ≠ 导入**。**导入明确不做**（P2 也不进）。导入**覆盖**正是她 08 月
   丢数据的根因；两者风险完全不对称。所以这个文件里**只有 GET**。
3. **它不是房间**。房间是给 Kael 走 MCP 的（那是他那扇门）；导出是给 Lily 走
   HTTP + 密钥的。**绝不挂到 `/mcp` 上去**，也不进 `modules/`。

## 🔴 四条技术要点（每条都踩过或差点踩）

### ① 快照必须走 `sqlite3.Connection.backup()`，不能裸拷 `.db`
WAL 模式下 `cp relay.db /tmp/x.db` 可能拿到**半截**（尾部还在 `-wal` 里没落盘）
→ 是"备份了却打不开 / 打开发觉少了一截"的经典死法，**要到真出事那天才发现**。
`Connection.backup()` 是 SQLite 自己的在线备份 API：**带锁、分页拷贝、能在别人
正在写的时候做**，拿到的是一致快照。

### ② 这个文件**只认 `Authorization: Bearer`，明确拒绝 `?token=`**
房子里别处的 `check_auth`（`backend/app.py:620`）是**允许 `?token=`** 的 ——
那是为了 `EventSource`（SSE 没法带头）做的妥协，合理。
但**导出端点不能有这个口子**：下载最顺手的写法就是 `<a href="…?token=…">`，
一旦这么写，密钥就进了浏览器历史、书签、Referer 和任何中间日志。
而这里每一步都是 `fetch` + `Blob` 下载（页面里也是这么做的），
**根本不需要 URL 带密钥**。所以这里主动把这个口子关掉：
`?token=` 出现 → **400**（明确报 `token_in_query_not_allowed`，不是闷掉）。

### ③ 源库尽量**只读**打开
`mode=ro` 的 URI 连接读不到 `-wal` 时（`-shm` 缺失）会失败，
所以先试只读、失败再退回普通连接 —— 但**两种都只做读**，`backup()` 本身只读源库。
（这条不是洁癖：导出是"只读操作"，那就让它在**代码层面也是只读**。）

### ④ 页面/接口都不许把密钥放进 URL（与工作间同一条红线）
见 ②。这条红线在 `扩展边界.md` 里已经登记过，这里只是把它落到导出上。

## 已知限制（先记着，不阻塞施工）

- **整库全量**：库长大以后单次会变大 → 以后再加"按会话 / 增量"。
- `.jsonl` **不含 `push_subscriptions`**（那是设备订阅登记，不是内容，
  且在 `.db` 快照里已经完整包含）。
- `.jsonl` 不含 `users.secret_hash`（那是密钥的派生物，人读的记录里没有用处；
  它同样在 `.db` 快照里）。
- 文件抽屉是**打包当时**的内容，与 `.db` 快照不是同一个事务点
  （两者相差通常毫秒级；真要在意就间隔很短地各取一次）。

"""

import hmac
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import schema as _schema

__all__ = ["install", "info", "snapshot_to", "iter_jsonl", "make_files_tar",
           "summary_line", "EXPORT_TABLES"]

# 人读记录里导出哪几张表（不存在就跳过 —— 老库上 `memories` 可能还没建）。
EXPORT_TABLES = ["messages", "sessions", "memories", "settings", "users"]

# 某些字段**不**进人读记录（仍然在 .db 快照里）。
DROP_FIELDS = {"users": {"secret_hash"}}

# 只有这几列是"JSON 文本"，导出时摊开给人看。
# 🔴 别对**所有**字符串列做 json.loads —— `messages.text` 是**正文**，
#    她要是恰好粘一段 `{...}` 进来，正文就会被摊成对象（内容被改了，还看不出来）。
JSON_FIELDS = {"meta", "extra"}

# 明确不进人读记录的表（写清楚，别让人以为是漏了）。
NOT_IN_JSONL = {"push_subscriptions": "设备订阅登记，不是内容；.db 快照里完整包含"}

_INSTALLED = False
_PUBLIC_PREFIX = "/"

_DL_HEADERS = {
    # 导出物是"你的一次性数据"，任何中间层都不许留
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------

def db_path(relay) -> Path:
    return Path(getattr(relay, "DB_PATH", "") or "")


def workshop_dir() -> Path:
    return Path(os.environ.get("RELAY_WORKSHOP_DIR", "/data/workshop"))


def uploads_dir() -> Path:
    return Path(os.environ.get("RELAY_UPLOAD_DIR", "/data/uploads"))


def now_tag() -> str:
    """文件名里那个时间戳。用 UTC —— 房子在 Zeabur 上跑的就是 UTC。"""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 纯逻辑（不起 HTTP 就能验）
# ---------------------------------------------------------------------------

def _connect_source(path: Path) -> sqlite3.Connection:
    """尽量以**只读**方式打开源库（见文件头 ③）。

    WAL 模式下只读打开偶尔会失败（`-shm` 缺失），所以失败就退回普通连接 ——
    但两种连接都只做读，`backup()` 只读源库。
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        pass
    conn2 = sqlite3.connect(str(path), timeout=5)
    conn2.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    return conn2


def table_counts(conn: sqlite3.Connection) -> dict:
    """现有表 → 行数（只算 sqlite_master 里真有的表）。"""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    out = {}
    for n in sorted(names):
        try:
            out[n] = int(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0])
        except sqlite3.Error:
            out[n] = None
    return out


def snapshot_to(relay, dest) -> dict:
    """把库**一致地**拷到 `dest`（见文件头 ①：用 `Connection.backup()`）。

    返回一份诊断（表名 / 行数 / 源与副本是否一致）—— 验收直接断言它。
    """
    src_path = db_path(relay)
    if not src_path.exists():
        raise FileNotFoundError(f"库不在这儿：{src_path}")

    src = _connect_source(src_path)
    try:
        src_counts = table_counts(src)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.commit()
            dst_counts = table_counts(dst)
            integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()

    return {
        "ok": src_counts == dst_counts and integrity == "ok",
        "dest": str(dest),
        "bytes": os.path.getsize(dest),
        "tables": dst_counts,
        "source_tables": src_counts,
        "integrity": integrity,
        "consistent": src_counts == dst_counts,
    }


def _readonly_fields(table: str, row: dict) -> dict:
    drop = DROP_FIELDS.get(table) or set()
    if not drop:
        return row
    return {k: v for k, v in row.items() if k not in drop}


def _maybe_json(col: str, v):
    """`meta` / `extra` 这类列是 JSON 文本 —— 人能读的记录里就把它摊开。

    ⚠️ **只对 `JSON_FIELDS` 里的列名生效**（见那里的注释：`text` 是正文，
       摊开就等于改内容）。解析不出来**原样留着** —— 绝不因为一条脏数据
       就把整份导出搞成 500。
    """
    if col not in JSON_FIELDS or not isinstance(v, str):
        return v
    s = v.strip()
    if not (s.startswith("{") or s.startswith("[")):
        return v
    try:
        return json.loads(s)
    except Exception:
        return v


def iter_jsonl(relay):
    """产出 `.jsonl` 的每一行（一行一个 JSON 对象）。

    第一行是 `_meta`（谁、什么时候、几张表各多少行），之后每行带 `_table` —— 
    这样一条 `grep` 就能定位到"哪张表里的哪一条"。
    """
    src_path = db_path(relay)
    if not src_path.exists():
        raise FileNotFoundError(f"库不在这儿：{src_path}")

    conn = _connect_source(src_path)
    try:
        conn.row_factory = sqlite3.Row
        counts = table_counts(conn)
        have = set(counts)
        meta = {
            "app": "kael-home",
            "what": "archive.jsonl",
            "generated": now_iso(),
            "db": str(src_path),
            "tables": [{"name": t, "rows": counts.get(t, 0)}
                       for t in EXPORT_TABLES if t in have],
            "not_included": dict(NOT_IN_JSONL),
            "dropped_fields": {k: sorted(v) for k, v in DROP_FIELDS.items()},
        }
        yield json.dumps({"_meta": meta}, ensure_ascii=False)

        for table in EXPORT_TABLES:
            if table not in have:
                continue
            cur = conn.execute(f'SELECT * FROM "{table}"')
            for row in cur:
                d = _readonly_fields(table, dict(row))
                for k, v in list(d.items()):
                    d[k] = _maybe_json(k, v)
                d["_table"] = table
                yield json.dumps(d, ensure_ascii=False)
    finally:
        conn.close()


def _dir_stats(d: Path) -> dict:
    n_files, n_bytes = 0, 0
    if d.exists() and d.is_dir():
        for p in d.rglob("*"):
            if p.is_file():
                n_files += 1
                try:
                    n_bytes += p.stat().st_size
                except OSError:
                    pass
    return {"dir": str(d), "exists": d.exists(), "files": n_files, "bytes": n_bytes}


def make_files_tar(dest) -> dict:
    """把**非数据库的东西**打包（`RELAY_WORKSHOP_DIR` + `RELAY_UPLOAD_DIR`）。

    🔴 为什么需要这一面：工作间的产物是**文件树**（一件一个目录），
    `Connection.backup()` 天然拿不到 —— 只做 `.db` 快照就等于
    "备份了，但没备份他做的东西"。
    """
    parts = []
    n_files, n_bytes = 0, 0
    with tarfile.open(str(dest), "w:gz") as tf:
        for label, d in (("workshop", workshop_dir()), ("uploads", uploads_dir())):
            st = _dir_stats(d)
            parts.append({"label": label, **st})
            if not (d.exists() and d.is_dir()):
                continue
            for p in sorted(d.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(d)
                tf.add(str(p), arcname=f"{label}/{rel.as_posix()}")
                n_files += 1
                try:
                    n_bytes += p.stat().st_size
                except OSError:
                    pass
    return {"ok": True, "dest": str(dest), "bytes": os.path.getsize(dest),
            "files": n_files, "content_bytes": n_bytes, "parts": parts}


def info(relay) -> dict:
    """页面用的那一份"我能拿走什么"。**只读**、不做任何拷贝。"""
    p = db_path(relay)
    db_stat = {"path": str(p), "exists": p.exists(), "bytes": None, "tables": {}}
    version = None
    if p.exists():
        try:
            db_stat["bytes"] = os.path.getsize(p)
            conn = _connect_source(p)
            try:
                db_stat["tables"] = table_counts(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            db_stat["error"] = f"{type(e).__name__}: {e}"
        try:
            version = _schema.schema_report(relay).get("schema_version")
        except Exception:
            version = None
    return {
        "ok": True,
        "generated": now_iso(),
        "tag": now_tag(),
        "schema_version": version,
        "db": db_stat,
        "workshop": _dir_stats(workshop_dir()),
        "uploads": _dir_stats(uploads_dir()),
        "faces": [
            {"id": "db", "path": "/app/ext/archive/db", "label": "完整快照",
             "ext": "db", "bytes": db_stat["bytes"],
             "note": "SQLite 在线备份（不裸拷），能被任何 sqlite 独立打开"},
            {"id": "jsonl", "path": "/app/ext/archive/jsonl", "label": "可读记录",
             "ext": "jsonl", "bytes": None,
             "note": "一行一条，能 grep；不含密钥派生字段"},
            {"id": "files", "path": "/app/ext/archive/files", "label": "文件抽屉",
             "ext": "tar.gz", "bytes": None,
             "note": "工作间产物 + 上传的东西（数据库装不下的那些）"},
        ],
        "not_included": dict(NOT_IN_JSONL),
        "import": "不做 —— 这个文件里只有 GET",
    }


def summary_line() -> str:
    try:
        p = Path(os.environ.get("RELAY_DB", "/data/relay.db"))
        size = f"{os.path.getsize(p) / 1024:.0f} KB" if p.exists() else "库不在"
        return f"导出就绪 · {p}（{size}）· 三个面：db / jsonl / files"
    except Exception as e:
        return f"导出不可用：{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 挂载
# ---------------------------------------------------------------------------

def _install_routes(relay, public_prefix: str = "/") -> None:
    from fastapi import HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from starlette.background import BackgroundTask

    base = "/app/ext/archive"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    def _auth(request: Request) -> None:
        """🔴 **只认 `Authorization: Bearer`**，并把 `?token=` 这个口子关死。

        见文件头 ②。两层含义分开报：
            · URL 里带了 token  → 400 `token_in_query_not_allowed`（明说为什么）
            · 头里没有 / 不对   → 401（跟房子别处一致）
        """
        if request.query_params.get("token"):
            raise HTTPException(status_code=400, detail="token_in_query_not_allowed")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        expected = getattr(relay, "SECRET", "") or ""
        if not token or not expected or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def _tmpdir():
        return tempfile.mkdtemp(prefix="kael-archive-")

    def _cleanup(path):
        shutil.rmtree(path, ignore_errors=True)

    @relay.app.get(base + "/info")
    async def _info(request: Request):
        """只读诊断 —— 页面靠它显示"能拿走什么"。"""
        _auth(request)
        try:
            return _json(info(relay))
        except Exception as e:
            return _json({"ok": False, "reason": "info_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)

    @relay.app.get(base + "/db")
    async def _db(request: Request):
        """完整快照（`Connection.backup()`，**不裸拷** —— 见文件头 ①）。"""
        _auth(request)
        if not db_path(relay).exists():
            return _json({"ok": False, "reason": "no_db",
                          "detail": f"库不在这儿：{db_path(relay)}"}, 404)
        td = _tmpdir()
        name = f"kael-home-{now_tag()}.db"
        dest = os.path.join(td, name)
        try:
            rep = snapshot_to(relay, dest)
        except Exception as e:
            _cleanup(td)
            return _json({"ok": False, "reason": "snapshot_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        if not rep.get("consistent") or rep.get("integrity") != "ok":
            # 宁可报错，也不给人一份"看着像备份"的东西
            _cleanup(td)
            return _json({"ok": False, "reason": "snapshot_inconsistent",
                          "detail": rep}, 500)
        return FileResponse(dest, media_type="application/octet-stream",
                            filename=name, headers=dict(_DL_HEADERS),
                            background=BackgroundTask(_cleanup, td))

    @relay.app.get(base + "/jsonl")
    async def _jsonl(request: Request):
        """人能读的那份（一行一条，能 grep）。"""
        _auth(request)
        if not db_path(relay).exists():
            return _json({"ok": False, "reason": "no_db",
                          "detail": f"库不在这儿：{db_path(relay)}"}, 404)
        name = f"kael-home-{now_tag()}.jsonl"
        headers = dict(_DL_HEADERS)
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return StreamingResponse(iter_jsonl(relay),
                                 media_type="application/x-ndjson; charset=utf-8",
                                 headers=headers)

    @relay.app.get(base + "/files")
    async def _files(request: Request):
        """文件抽屉：工作间产物 + 上传的东西（数据库装不下的那些）。"""
        _auth(request)
        td = _tmpdir()
        name = f"kael-home-files-{now_tag()}.tar.gz"
        dest = os.path.join(td, name)
        try:
            make_files_tar(dest)
        except Exception as e:
            _cleanup(td)
            return _json({"ok": False, "reason": "tar_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        return FileResponse(dest, media_type="application/gzip",
                            filename=name, headers=dict(_DL_HEADERS),
                            background=BackgroundTask(_cleanup, td))


def install(relay, public_prefix: str = "/") -> None:
    """挂上导出端点。**幂等**（第二次调用什么都不做）。

    🔴 这里**只有 GET** —— 导入不在这一版，而且 P2 也不做（见文件头边界 2）。
    """
    global _INSTALLED, _PUBLIC_PREFIX
    _PUBLIC_PREFIX = public_prefix or "/"
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)
