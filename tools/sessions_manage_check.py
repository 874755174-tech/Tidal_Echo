#!/usr/bin/env python3
"""
会话归档 / 恢复 / 清空 / 改名的验收（数据库层 + HTTP 层）
==========================================================

覆盖：
  A. 数据库层（直接调 sessions_manage 的函数，不需要起服务）
     1. 归档 → 列表里消失、消息数不变（**可逆的核心证据**）
     2. 取消归档 → 回到列表
     3. 改名 → 标题持久化，**虚拟会话 __legacy__ 也能改**（这是原版的 bug）
     4. 清空 → 从列表消失 + 消息数归零（对前端而言）
  B. HTTP 层（起 relay，打真实请求）
     5. 未带密钥 → 401（🔴 红线：所有新端点都 fail-closed）
     6. 错误密钥 → 401
     7. 正确密钥 → 能走通

跑法（项目 venv）：
    python tools/sessions_manage_check.py
结果写 tools/manage_report.txt
"""
from __future__ import annotations

import json
import os
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

SECRET = "test-secret-manage-0123456789"
PORT = 8791

results: list[tuple[str, bool, str]] = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def wait_port(port: int, timeout: float = 25.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def req(url: str, *, method="GET", token=None, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


# ── 种子数据：三个会话 + 若干无标签旧消息 ────────────────────────────────────

def seed(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}')"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS push_subscriptions ("
        " endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,"
        " ua TEXT, created TEXT NOT NULL, last_ok TEXT)"
    )
    rows = [
        # 无标签 → 虚拟会话 __legacy__
        ("2026-07-01T00:00:00Z", "in",  "user",  "旧消息一", {}),
        ("2026-07-01T00:01:00Z", "out", "reply", "旧回复一", {}),
        # 会话 A
        ("2026-07-02T00:00:00Z", "in",  "user",  "A 的问题", {"api_session": "sess-A"}),
        ("2026-07-02T00:01:00Z", "out", "reply", "A 的回答", {"api_session": "sess-A"}),
        # 会话 B
        ("2026-07-03T00:00:00Z", "in",  "user",  "B 的问题", {"api_session": "sess-B"}),
    ]
    for ts, d, k, t, meta in rows:
        conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
            (ts, d, k, t, json.dumps(meta, ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def db_count(db_path: Path, sid: str) -> int:
    conn = sqlite3.connect(db_path)
    if sid == "__legacy__":
        sql = ("SELECT COUNT(*) FROM messages WHERE json_extract(meta,'$.api_session') IS NULL "
               "OR json_extract(meta,'$.api_session') = ''")
        n = conn.execute(sql).fetchone()[0]
    else:
        n = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE json_extract(meta,'$.api_session') = ?",
            (sid,),
        ).fetchone()[0]
    conn.close()
    return n


def raw_count(db_path: Path) -> int:
    """库里物理行数（用来证明"归档/清空都不删行"）。"""
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    return n


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kaelhome-sessmgmt-"))
    db_path = tmp / "relay.db"
    seed(db_path)

    # ═══════ A. 数据库层（不启服务，直接 import）═══════
    sys.path.insert(0, str(DEPLOY))
    sys.path.insert(0, str(BACKEND))
    os.environ["RELAY_SECRET"] = SECRET
    os.environ["RELAY_DB"] = str(db_path)
    os.environ["RELAY_PORT"] = str(PORT)

    import sessions_manage as sm  # noqa: E402

    class FakeRelay:
        DB_PATH = str(db_path)

    R = FakeRelay()
    before_raw = raw_count(db_path)
    chk("种子数据已写好（5 条物理消息）", before_raw == 5, f"实际 {before_raw}")

    # 1) 列表基线
    lst = sm.list_sessions(R)
    ids = [s["id"] for s in lst["sessions"]]
    chk("列表含三个会话（legacy + A + B）", set(ids) == {"__legacy__", "sess-A", "sess-B"}, str(ids))
    a = next(s for s in lst["sessions"] if s["id"] == "sess-A")
    chk("会话 A 带消息数 count=2", a.get("count") == 2, str(a.get("count")))

    # 2) 归档 sess-A
    res = sm.archive_session(R, "sess-A", on=True)
    chk("归档 sess-A 成功", res.get("ok") is True, str(res))
    lst2 = sm.list_sessions(R)
    ids2 = [s["id"] for s in lst2["sessions"]]
    chk("归档后列表里没有 sess-A", "sess-A" not in ids2, str(ids2))
    chk("🔴 归档后数据库物理行数不变（消息没被删）", raw_count(db_path) == before_raw,
        f"{raw_count(db_path)} vs {before_raw}")
    chk("🔴 归档后 sess-A 消息数仍是 2", db_count(db_path, "sess-A") == 2,
        str(db_count(db_path, "sess-A")))

    # 3) 归档列表里能查到
    arc = sm.list_archived(R)
    chk("归档列表里能找到 sess-A", any(s["id"] == "sess-A" for s in arc["sessions"]),
        str([s["id"] for s in arc["sessions"]]))

    # 4) 取消归档 → 恢复
    res = sm.archive_session(R, "sess-A", on=False)
    chk("取消归档成功", res.get("ok") is True, str(res))
    ids3 = [s["id"] for s in sm.list_sessions(R)["sessions"]]
    chk("🔴 恢复后 sess-A 回到列表（归档可逆）", "sess-A" in ids3, str(ids3))
    chk("恢复后消息数仍是 2", db_count(db_path, "sess-A") == 2, str(db_count(db_path, "sess-A")))

    # 5) 改名 —— 真实会话
    res = sm.rename_session(R, "sess-B", "我给 B 起的名字")
    chk("改名 sess-B 成功", res.get("ok") is True, str(res))
    b = next(s for s in sm.list_sessions(R)["sessions"] if s["id"] == "sess-B")
    chk("改名后标题持久化", b["title"] == "我给 B 起的名字", b["title"])

    # 6) 改名 —— 🔴 虚拟会话（原版这里必然 404）
    res = sm.rename_session(R, "__legacy__", "旧主线的新名字")
    chk("🔴 虚拟会话 __legacy__ 也能改名（修复原版 404）", res.get("ok") is True, str(res))
    lg = next(s for s in sm.list_sessions(R)["sessions"] if s["id"] == "__legacy__")
    chk("虚拟会话标题已持久化", lg["title"] == "旧主线的新名字", lg["title"])

    # 7) 改名边界
    chk("空标题被拒", sm.rename_session(R, "sess-A", "   ").get("ok") is False)
    chk("不存在的会话改名 → not_found",
        sm.rename_session(R, "sess-NOPE", "x").get("reason") == "not_found")

    # 8) 清空 sess-B
    cnt = sm.count_session(R, "sess-B")["count"]
    chk("count 接口返回 sess-B 有 1 条", cnt == 1, str(cnt))
    res = sm.purge_session(R, "sess-B")
    chk("清空 sess-B 成功", res.get("ok") is True, str(res))
    ids4 = [s["id"] for s in sm.list_sessions(R)["sessions"]]
    chk("清空后列表里没有 sess-B", "sess-B" not in ids4, str(ids4))
    chk("🔴 清空后物理行数仍不变（标记式删除，可人工恢复）",
        raw_count(db_path) == before_raw, f"{raw_count(db_path)} vs {before_raw}")

    # 9) 归档不影响其他会话
    ids5 = [s["id"] for s in sm.list_sessions(R)["sessions"]]
    chk("清空 sess-B 不影响 __legacy__ 与 sess-A",
        "__legacy__" in ids5 and "sess-A" in ids5, str(ids5))

    # ═══════ B. HTTP 层（起 relay，验鉴权红线）═══════
    env = dict(os.environ)
    env.update({
        "RELAY_SECRET": SECRET,
        "RELAY_DB": str(db_path),
        "RELAY_PUBLIC_PREFIX": "/relay",
        "RELAY_BACKEND_DIR": str(BACKEND),
        "RELAY_WEB_DIR": str(REPO / "web"),
        "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
    })
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--app-dir", str(DEPLOY)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(PORT):
            chk("relay 能启动", False, "健康检查超时")
        else:
            chk("relay 能启动", True)
            base = f"http://127.0.0.1:{PORT}"

            # 无密钥
            for path, method, body in [
                ("/app/sessions/manage", "GET", None),
                ("/app/sessions/manage/archived", "GET", None),
                ("/app/sessions/manage/archive", "POST", {"session_id": "sess-A"}),
                ("/app/sessions/manage/purge", "POST", {"session_id": "sess-A"}),
                ("/app/sessions/manage/rename", "POST", {"session_id": "sess-A", "title": "x"}),
            ]:
                st, _ = req(base + path, method=method, body=body)
                chk(f"🔴 无密钥 {method} {path} → 401", st == 401, f"实际 {st}")

            # 错误密钥
            st, _ = req(base + "/app/sessions/manage", token="wrong-secret")
            chk("🔴 错误密钥 GET manage → 401", st == 401, f"实际 {st}")
            st, body_ = req(base + "/app/sessions/manage/purge", method="POST",
                            token="wrong", body={"session_id": "sess-A"})
            chk("🔴 错误密钥 purge → 401", st == 401, f"实际 {st}")

            # 正确密钥：读
            st, d = req(base + "/app/sessions/manage", token=SECRET)
            chk("正确密钥 GET manage → 200", st == 200, f"实际 {st}")
            chk("返回里含 sessions 数组", isinstance(d.get("sessions"), list), str(type(d.get("sessions"))))

            # 正确密钥：改名（虚拟会话，走完整 HTTP 链路）
            st, d = req(base + "/app/sessions/manage/rename", method="POST", token=SECRET,
                        body={"session_id": "__legacy__", "title": "HTTP 改名测试"})
            chk("正确密钥 rename __legacy__ → 200", st == 200, f"实际 {st} {d}")
            st, d = req(base + "/app/sessions/manage", token=SECRET)
            lg2 = next((s for s in d.get("sessions", []) if s["id"] == "__legacy__"), None)
            chk("HTTP 改名已生效", lg2 and lg2.get("title") == "HTTP 改名测试",
                str(lg2))

            # 正确密钥：归档 + 恢复
            st, _ = req(base + "/app/sessions/manage/archive", method="POST", token=SECRET,
                        body={"session_id": "sess-A", "archived": True})
            chk("正确密钥 archive → 200", st == 200, f"实际 {st}")
            st, d = req(base + "/app/sessions/manage", token=SECRET)
            chk("HTTP 归档后列表无 sess-A",
                "sess-A" not in [s["id"] for s in d.get("sessions", [])],
                str([s["id"] for s in d.get("sessions", [])]))
            st, _ = req(base + "/app/sessions/manage/archive", method="POST", token=SECRET,
                        body={"session_id": "sess-A", "archived": False})
            st, d = req(base + "/app/sessions/manage", token=SECRET)
            chk("HTTP 恢复后 sess-A 回来",
                "sess-A" in [s["id"] for s in d.get("sessions", [])],
                str([s["id"] for s in d.get("sessions", [])]))

            # 带 /relay 前缀也要能用（Zeabur 上前端就是这么发的）
            st, d = req(base + "/relay/app/sessions/manage", token=SECRET)
            chk("带 /relay 前缀也能读列表", st == 200, f"实际 {st}")

            # 原有端点未被影响
            st, _ = req(base + "/app/history", token=SECRET)
            chk("原版 /app/history 未受影响 → 200", st == 200, f"实际 {st}")
            st, _ = req(base + "/healthz")
            chk("原版 /healthz 仍未鉴权 → 200", st == 200, f"实际 {st}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 汇总 ──
    lines = []
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        extra = f"   [{detail}]" if detail and not ok else ""
        lines.append(f"[{mark}] {name}{extra}")
    head = f"会话归档/删除/改名验收：{passed}/{len(results)} 通过"
    lines.insert(0, head)
    out = "\n".join(lines) + "\n"
    (HERE / "manage_report.txt").write_text(out, encoding="utf-8")
    print(head)
    for l in lines[1:]:
        if l.startswith("[FAIL]"):
            print(l)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
