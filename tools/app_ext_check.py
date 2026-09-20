#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 地基验收 —— 四张表 / 身份层 / 红线
==========================================================

覆盖三层：

  A. 库层（直接调 app_ext 的函数，不需要起服务）
     1.  全新空库 → 建出 4 张表
     2.  🔴 老库升级：只有 messages + push_subscriptions 的库 → 补齐 4 表
     3.  🔴 messages 的 DDL 快照**一字不变**
     4.  🔴 messages 的行数**一行不变**
     5.  幂等：连跑 3 次，结果一致、表数量不膨胀
     6.  user_version = 当前 SCHEMA_VERSION（v2 起跟着代码走，不写死数字）
     7.  播种房主：1 条、id=u_owner、role=owner
     8.  🔴 secret_hash 里**不含明文密钥**
     9.  verify_secret 能验通过
     10. 再播种 → seeded=False（幂等，不重复建）
     11. settings 默认读 → source=defaults
     12. 写 persona → 读回一致、source=db
     13. 部分更新（只改 temperature）→ persona **保留**
     14. 非法值（temperature=5）→ ValueError
     15. 🔴 白名单过滤：传 user_id / id / secret_hash → 全部丢弃
     16. sessions 投影：从 messages 建档（含 __legacy__）
     17. 投影带出 meta.title / meta.archived
     18. 🔴 sync 不覆盖 sessions.summary
     19. memories：add / list / top / stats
     20. 🔴 memories **没有 delete 接口**（设计上就不该存在）
     21. memories 非法 kind → 归到 fact

  B. HTTP 层（起 relay，打真实请求）
     22. 🔴 /app/ext/* 四个端点无密钥 → 401（fail-closed）
     23. 🔴 错误密钥 login → 401
     24. 正确密钥 login → 200 + user.id
     25. /app/ext/me 带密钥 → 200
     26. PUT /app/ext/settings → 200，落库
     27. GET /app/ext/settings → 读到刚写的值
     28. /app/ext/schema → tables_ok=True
     29. 非法值 PUT → 400
     30. 🔴 回归：原版 /app/history 仍 200
     31. 🔴 回归：第一阶段的 /app/sessions/manage 仍 200

  C. 红线
     32. git diff e7c9bf5 -- backend/ examples/ channel/ 为空

跑法（项目 venv）：
    .venv\\Scripts\\python.exe tools\\app_ext_check.py
结果写 tools/app_ext_report.txt
"""

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

# 自举：被托管解释器直接跑时（没有 fastapi），用项目 venv 重跑一遍
try:
    import fastapi  # noqa: F401
except Exception:
    if Path(PY).exists() and os.path.abspath(PY) != os.path.abspath(sys.executable):
        sys.exit(subprocess.call([PY, os.path.abspath(__file__)] + sys.argv[1:]))
    raise

sys.path.insert(0, str(DEPLOY))

SECRET = "test-secret-appext-0123456789"
PORT = 8792
BASE_COMMIT = "e7c9bf5"          # 房子的起点 commit（红线基准）

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def wait_port(port: int, timeout: float = 30.0) -> bool:
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


# ── 种子：一个"只有原版两张表"的老库 ────────────────────────────────────────

def seed_legacy_db(db_path: Path) -> None:
    """造一个第一阶段形态的库：messages（有数据）+ push_subscriptions，没有 4 张新表。"""
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
    m = lambda **kw: json.dumps(kw, ensure_ascii=False)
    rows = [
        ("2026-09-01T10:00:00Z", "in", "user", "早",
         m(api_session="sess-A", title="关于雾潮群岛")),
        ("2026-09-01T10:00:05Z", "out", "reply", "早呀",
         m(api_session="sess-A", title="关于雾潮群岛")),
        ("2026-09-01T10:02:00Z", "in", "user", "那封信我看了",
         m(api_session="sess-A", title="关于雾潮群岛")),
        ("2026-09-02T21:00:00Z", "in", "user", "睡了",
         m(api_session="sess-B", archived=1)),
        ("2026-09-02T21:00:30Z", "out", "reply", "晚安",
         m(api_session="sess-B", archived=1)),
        ("2026-08-20T09:00:00Z", "in", "user", "最早的记录一", "{}"),
        ("2026-08-20T09:01:00Z", "out", "reply", "最早的记录二", "{}"),
    ]
    conn.executemany(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)", rows
    )
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua, created) "
        "VALUES ('https://push.example/1','p','a','test',?)",
        ("2026-08-01T00:00:00Z",),
    )
    conn.commit()
    conn.close()


def ddl_signature(db_path: Path, table: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    conn.close()
    return " ".join((row[0] if row else "").split())


def table_names(db_path: Path) -> set:
    """库里的用户表。**排除 `sqlite_sequence`** —— 那是 SQLite 为 AUTOINCREMENT
    自动建的内部表（messages 有 AUTOINCREMENT 就会有它），不是我们建的。"""
    conn = sqlite3.connect(db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    return names - {"sqlite_sequence"}


def row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        n = -1
    conn.close()
    return n


def main() -> int:
    import app_ext
    from app_ext import schema as S, identity as I, sessions_store as SS, memories_store as MS

    tmp = Path(tempfile.mkdtemp(prefix="kaelhome_appext_"))
    db_path = tmp / "relay.db"
    log_path = tmp / "uvicorn.log"

    # 假 relay：库层只需要 DB_PATH / SECRET / HUMAN_NAME
    class FakeRelay:
        DB_PATH = str(db_path)
        SECRET = SECRET
        HUMAN_NAME = "Lily"

    R = FakeRelay()

    try:
        # ═══════ A. 库层 ═══════
        seed_legacy_db(db_path)

        msg_ddl_before = ddl_signature(db_path, "messages")
        msg_rows_before = row_count(db_path, "messages")
        tables_before = table_names(db_path)
        chk("种子老库已造好（7 条消息 / 只有原版 2 张表）",
            msg_rows_before == 7 and tables_before == {"messages", "push_subscriptions"},
            f"rows={msg_rows_before} tables={sorted(tables_before)}")

        # 1) 补齐 4 张表
        rep = S.ensure_schema(R)
        chk("① 老库升级补齐 4 张表",
            set(rep["created"]) == {"users", "settings", "sessions", "memories"},
            str(rep["created"]))
        names_after = table_names(db_path)
        chk("① 库里现在有 6 张表",
            {"users", "settings", "sessions", "memories"} <= names_after,
            str(sorted(names_after)))

        # 2) 🔴 messages 红线
        chk("🔴 ③ messages 的 DDL 一字未变",
            ddl_signature(db_path, "messages") == msg_ddl_before,
            ddl_signature(db_path, "messages")[:90])
        chk("🔴 ④ messages 的行数一行未变",
            row_count(db_path, "messages") == msg_rows_before,
            f"{row_count(db_path, 'messages')} vs {msg_rows_before}")
        chk("🔴 ③ push_subscriptions 也未被碰",
            row_count(db_path, "push_subscriptions") == 1,
            str(row_count(db_path, "push_subscriptions")))
        chk("⑤ ensure_schema 自报 messages_untouched=True",
            rep.get("messages_untouched") is True, str(rep.get("messages_untouched")))

        # 3) 幂等
        n_before = len(names_after)
        ok3 = True
        for _ in range(2):
            r2 = S.ensure_schema(R)
            if r2["created"]:
                ok3 = False
        chk("⑤ 连跑 3 次幂等（第 2/3 次不新建任何表）",
            ok3 and len(table_names(db_path)) == n_before,
            f"created={r2['created']} tables={len(table_names(db_path))}")

        # 4) 版本号
        conn = sqlite3.connect(db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        chk("⑥ user_version 已推到当前 SCHEMA_VERSION",
            ver == S.SCHEMA_VERSION, f"库里 {ver} / 代码 {S.SCHEMA_VERSION}")

        # 5) 播种房主
        seed1 = I.ensure_owner(R)
        chk("⑦ 首次启动播种房主",
            seed1.get("seeded") is True and seed1.get("id") == "u_owner", str(seed1))
        u = I.get_user(R)
        chk("⑦ 房主 id/role 正确",
            u and u["id"] == "u_owner" and u["role"] == "owner", str(u))

        conn = sqlite3.connect(db_path)
        raw_hash = conn.execute("SELECT secret_hash FROM users").fetchone()[0]
        conn.close()
        chk("🔴 ⑧ 库里存的 secret_hash 不含明文密钥",
            SECRET not in raw_hash and raw_hash.startswith("pbkdf2_sha256$"),
            raw_hash[:40] + "...")
        chk("⑨ verify_secret 能验通过真密钥", I.verify_secret(SECRET, raw_hash) is True)
        chk("⑨ verify_secret 拒绝错密钥", I.verify_secret("wrong", raw_hash) is False)

        seed2 = I.ensure_owner(R)
        chk("⑩ 再播种 → seeded=False（幂等）",
            seed2.get("seeded") is False and seed2.get("users") == 1, str(seed2))

        # 6) settings
        st0 = I.get_settings(R)
        chk("⑪ 默认设置 source=defaults", st0.get("source") == "defaults", str(st0.get("source")))

        res = I.save_settings(R, {"persona": "你是 Kael。", "max_tokens": 4096})
        chk("⑫ 写 persona 成功",
            res.get("ok") is True and set(res["changed"]) == {"persona", "max_tokens"},
            str(res.get("changed")))
        st1 = I.get_settings(R)
        chk("⑫ 读回一致 + source=db",
            st1["persona"] == "你是 Kael。" and st1["max_tokens"] == 4096
            and st1["source"] == "db", f"{st1['persona']} / {st1['max_tokens']}")

        I.save_settings(R, {"temperature": 0.8})
        st2 = I.get_settings(R)
        chk("⑬ 部分更新不影响其它字段",
            st2["temperature"] == 0.8 and st2["persona"] == "你是 Kael。",
            f"temp={st2['temperature']} persona={st2['persona']}")

        try:
            I.save_settings(R, {"temperature": 5.0})
            bad_ok = False
        except ValueError:
            bad_ok = True
        chk("⑭ 非法值 temperature=5 → ValueError", bad_ok)

        before_persona = I.get_settings(R)["persona"]
        res = I.save_settings(R, {"user_id": "hacked", "id": "x", "secret_hash": "y",
                                  "role": "owner", "effort": "high"})
        chk("🔴 ⑮ 白名单过滤：非白名单字段全部丢弃",
            res.get("changed") == ["effort"], str(res.get("changed")))
        chk("🔴 ⑮ user_id 没被改（仍是 u_owner）",
            I.get_user(R)["id"] == "u_owner" and I.get_settings(R)["persona"] == before_persona,
            str(I.get_user(R)))

        # 7) sessions 投影
        sy = SS.sync_from_messages(R)
        chk("⑯ 会话投影建出 3 条（sess-A / sess-B / __legacy__）",
            sy["scanned"] == 3 and sy["inserted"] == 3, str(sy))
        sids = {s["id"] for s in SS.list_sessions(R, include_archived=True)}
        chk("⑯ 投影的会话 id 正确",
            sids == {"sess-A", "sess-B", "__legacy__"}, str(sorted(sids)))

        a = SS.get_session(R, "sess-A")
        b = SS.get_session(R, "sess-B")
        chk("⑰ 投影带出 meta.title", a and a["title"] == "关于雾潮群岛", str(a and a["title"]))
        chk("⑰ 投影带出 meta.archived", b and b["archived"] == 1, str(b and b["archived"]))
        got_mc = [(s or {}).get("message_count") for s in (a, b)]
        chk("⑰ 投影的 message_count 正确", got_mc == [3, 2], f"A={got_mc[0]} B={got_mc[1]}")

        # 18) 🔴 summary 不被 sync 覆盖
        SS.set_summary(R, "sess-A", "这是我给 A 写的滚动摘要。")
        sy2 = SS.sync_from_messages(R)
        a2 = SS.get_session(R, "sess-A")
        chk("🔴 ⑱ sync 不覆盖 sessions.summary",
            a2["summary"] == "这是我给 A 写的滚动摘要。",
            str(a2["summary"]))
        chk("⑱ sync 报告 untouched_summary=1",
            sy2["untouched_summary"] == 1, str(sy2["untouched_summary"]))

        # 8) memories
        m1 = MS.add(R, "fact", "Lily 喜欢盐系手帐风", salience=0.9)
        m2 = MS.add(R, "event", "他第一次读到雾潮群岛的邀请函", source_msg=3, salience=0.6)
        m3 = MS.add(R, "nonsense-kind", "kind 不在枚举里", salience=0.3)
        chk("⑲ memories add 成功", m1.get("ok") and m2.get("ok") and m3.get("ok"))
        chk("⑲ memories list_recent 能读回 3 条", len(MS.list_recent(R)) == 3,
            str(len(MS.list_recent(R))))
        top = MS.top(R, limit=2)
        chk("⑲ memories top 按 salience 排序",
            [t["id"] for t in top] == [m1["id"], m2["id"]],
            str([t["text"] for t in top]))
        chk("⑲ memories stats 分类正确",
            MS.stats(R)["total"] == 3 and MS.stats(R)["by_kind"].get("fact") == 2,
            str(MS.stats(R)))
        chk("⑳ 🔴 memories 模块**没有** delete 接口",
            not any(hasattr(MS, n) for n in ("delete", "remove", "forget", "purge")),
            "（设计上就不该存在：记忆是最不该擅自动的东西）")
        chk("㉑ 非法 kind 归到 fact（直查库）",
            _kind_of(db_path, m3["id"]) == "fact", _kind_of(db_path, m3["id"]))
        chk("㉑ as_extra_for_prompt 不带 salience 数值",
            "salience" not in json.dumps(MS.as_extra_for_prompt(R), ensure_ascii=False))

        # ═══════ B. HTTP 层 ═══════
        env = dict(os.environ)
        env.update({
            "RELAY_SECRET": SECRET,
            "RELAY_DB": str(db_path),
            "RELAY_HUMAN_NAME": "Lily",
            "RELAY_PUBLIC_PREFIX": "/relay",
            "RELAY_BACKEND_DIR": str(BACKEND),
            "RELAY_WEB_DIR": str(REPO / "web"),
            "RELAY_UPLOAD_DIR": str(tmp / "uploads"),
            "PYTHONPATH": str(DEPLOY) + os.pathsep + str(BACKEND),
        })
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "serve:app", "--host", "127.0.0.1", "--port", str(PORT),
             "--app-dir", str(DEPLOY)],
            env=env, stdout=logf, stderr=subprocess.STDOUT,
        )
        try:
            if not wait_port(PORT):
                chk("㉒ relay 能启动", False, "健康检查超时")
                print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            else:
                chk("㉒ relay 能启动", True)
                base = f"http://127.0.0.1:{PORT}"

                # 22) 无密钥 → 401
                for method, path, body in [
                    ("GET", "/app/ext/me", None),
                    ("GET", "/app/ext/settings", None),
                    ("PUT", "/app/ext/settings", {"persona": "x"}),
                    ("GET", "/app/ext/schema", None),
                ]:
                    st, _ = req(base + path, method=method, body=body)
                    chk(f"🔴 ㉒ 无密钥 {method} {path} → 401", st == 401, f"实际 {st}")

                # 22b) 带上 /relay 前缀也要一样（中间件剥前缀后应命中同一端点）
                st, _ = req(base + "/relay/app/ext/me")
                chk("🔴 ㉒ 带 /relay 前缀且无密钥 → 401", st == 401, f"实际 {st}")

                # 23) 错误密钥
                st, _ = req(base + "/app/ext/login", method="POST", body={"secret": "wrong"})
                chk("🔴 ㉓ 错误密钥 login → 401", st == 401, f"实际 {st}")

                # 24) 正确密钥
                st, bd = req(base + "/app/ext/login", method="POST", body={"secret": SECRET})
                chk("㉔ 正确密钥 login → 200 + user.id",
                    st == 200 and bd.get("user", {}).get("id") == "u_owner",
                    f"{st} {bd.get('user')}")

                # 25) me
                st, bd = req(base + "/app/ext/me", token=SECRET)
                chk("㉕ /app/ext/me 带密钥 → 200",
                    st == 200 and bd.get("user", {}).get("id") == "u_owner", f"{st}")

                # 26/27) 写 + 读
                st, bd = req(base + "/app/ext/settings", method="PUT", token=SECRET,
                             body={"persona": "HTTP 写进来的 persona", "effort": "high"})
                chk("㉖ PUT settings → 200 且 changed 正确",
                    st == 200 and set(bd.get("changed") or []) == {"persona", "effort"},
                    f"{st} {bd.get('changed')}")
                st, bd = req(base + "/app/ext/settings", token=SECRET)
                chk("㉗ GET settings 读到刚写的值",
                    st == 200 and bd.get("persona") == "HTTP 写进来的 persona", f"{st}")

                # 28) schema 诊断
                st, bd = req(base + "/app/ext/schema", token=SECRET)
                chk("㉘ /app/ext/schema → tables_ok & up_to_date",
                    st == 200 and bd.get("tables_ok") is True and bd.get("up_to_date") is True,
                    f"{st} {bd.get('schema_version')} {bd.get('tables_present')}")

                # 29) 非法值
                st, _ = req(base + "/app/ext/settings", method="PUT", token=SECRET,
                            body={"temperature": 99})
                chk("㉙ 非法值 PUT → 400", st == 400, f"实际 {st}")

                # 30/31) 回归
                st, _ = req(base + "/app/history?since=0&limit=1", token=SECRET)
                chk("🔴 ㉚ 回归：原版 /app/history 仍 200", st == 200, f"实际 {st}")
                st, bd = req(base + "/app/sessions/manage", token=SECRET)
                chk("🔴 ㉛ 回归：第一阶段 /app/sessions/manage 仍 200",
                    st == 200 and isinstance(bd.get("sessions"), list), f"实际 {st}")
                st, _ = req(base + "/healthz")
                chk("🔴 ㉛ 回归：/healthz 仍 200（无需密钥）", st == 200, f"实际 {st}")

                # 32) messages 在服务跑过之后仍然没变
                chk("🔴 ㉜ 服务跑完 messages 行数仍为 7",
                    row_count(db_path, "messages") == 7, str(row_count(db_path, "messages")))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            logf.close()

        # ═══════ C. 红线 ═══════
        d = subprocess.run(
            ["git", "diff", "--stat", BASE_COMMIT, "--", "backend/", "examples/", "channel/"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        out = (d.stdout or "").strip()
        chk(f"🔴 ㉝ 红线：git diff {BASE_COMMIT} -- backend/ examples/ channel/ 为空",
            out == "", out[:300] or "（空 ✅）")

        # 也确认 app.py 本身没被改
        d2 = subprocess.run(
            ["git", "diff", "--stat", BASE_COMMIT, "--", "backend/app.py"],
            cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        chk("🔴 ㉝ backend/app.py 零改动", (d2.stdout or "").strip() == "",
            (d2.stdout or "").strip()[:200] or "（空 ✅）")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 汇总 ──
    ok_n = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    lines = []
    for name, ok, detail in results:
        lines.append(f"{'✅' if ok else '❌'} {name}" + (f"   [{detail}]" if detail else ""))
    lines.append("")
    lines.append(f"总计：{ok_n}/{total} 通过")
    report = "\n".join(lines)

    print(report)
    (HERE / "app_ext_report.txt").write_text(report, encoding="utf-8")
    return 0 if ok_n == total else 1


def _kind_of(db_path: Path, mid: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT kind FROM memories WHERE id = ?", (mid,)).fetchone()
    conn.close()
    return row[0] if row else ""


if __name__ == "__main__":
    sys.exit(main())
