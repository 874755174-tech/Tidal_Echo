# -*- coding: utf-8 -*-
"""
会话数据层 + 列表兜底 实测
================================================================================
要回答的问题：
  1. messages 表里会话是怎么存的？（答：只是 meta 里的一个标签）
  2. 按会话过滤历史（history_for_session）是原生的吗？
  3. /app/sessions 在没有 api_loop 时**还能不能用**？（兜底装好之后：能）
  4. 兜底会不会绕过鉴权？（答：不会）

⚠️ 这个脚本以前是"观察型"的（只打印现象，不下结论）。
   现在改成**断言型**：每条都判 PASS/FAIL，最后给总数，出问题会以非 0 退出。
   原因：兜底上线后，"会话列表 502" 从"已知现象"变成了"回归"——
   如果它又 502 了，必须让脚本失败，而不是让人去读一堆日志。

用法：.venv\\Scripts\\python.exe tools\\sessioncheck.py
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SECRET = "SESSION_TEST_KEY"

results = []


def check(ok, label, detail=""):
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def log(s=""):
    print(s)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def req(path, token=None, method="GET", body=None, timeout=8):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ⚠️ 运行目录放**系统 temp**，不要放项目文件夹里。
#    2026-09-13 踩过：以前这里是 `ROOT/_runtime_sess`，脚本开头的
#    `os.remove(...)` 会删项目目录里的文件，被沙箱拦成"批量删除"并要求
#    用户授权 —— 一个纯测试脚本不该让人确认删东西。tempdir 每次新建，
#    既不用删、也不会污染仓库（tempfile.mkdtemp 已保证唯一）。
import tempfile

RUN = tempfile.mkdtemp(prefix="kaelhome-sesscheck-")
os.makedirs(RUN, exist_ok=True)
DB = os.path.join(RUN, "relay.db")

ENV = os.environ.copy()
ENV.update({
    "RELAY_SECRET": SECRET,
    "RELAY_DB": DB,
    "RELAY_UPLOAD_DIR": os.path.join(RUN, "uploads"),
    "RELAY_BRAIN_FILE": os.path.join(RUN, "brain_target"),
    "LOOP_CONFIG": os.path.join(RUN, "api_loop.config.json"),
    # 指向一个死端口：模拟"第二阶段换身体后 api_loop 不存在"
    "RELAY_LOOP_INGEST_URL": f"http://127.0.0.1:{free_port()}/loop/ingest",
})

launcher = os.path.join(RUN, "_l.py")
with open(launcher, "w", encoding="utf-8") as f:
    f.write(
        "import sys\n"
        f"sys.path.insert(0, r'{os.path.join(ROOT, 'deploy')}')\n"
        "import uvicorn, serve\n"
        f"uvicorn.run(serve.app, host='127.0.0.1', port={PORT}, log_level='error')\n"
    )

log("=" * 78)
log("会话数据层 + 列表兜底 实测（不开 api_loop）")
log("=" * 78)

proc = subprocess.Popen([PY, launcher], cwd=ROOT, env=ENV,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
ready = False
for _ in range(40):
    time.sleep(0.4)
    st, _ = req("/healthz")
    if st == 200:
        ready = True
        break
log(f"\n[启动] relay 就绪 = {ready}")
if not ready:
    try:
        log(proc.stdout.read().decode("utf-8", "replace")[:1500])
    except Exception:
        pass
    proc.kill()
    sys.exit(1)

try:
    log("\n── 1. 空库 + api_loop 不在：应该给一个「合法但空」的列表，而不是 502 ──")
    st, b = req("/app/sessions", token=SECRET)
    check(st == 200, "空库时 GET /app/sessions → 200", f"实际 {st}")
    d = json.loads(b)
    check(d.get("fallback") is True, "标了 fallback=true（前端据此提示）")
    check(d.get("sessions") == [], "空库时列表是空数组（不是报错）")

    log("\n── 2. 造三条分属不同会话的消息（直接写库，模拟已有历史）──")
    conn = sqlite3.connect(DB)
    rows = [
        ("2026-09-13T05:00:00Z", "in", "user", "第一会话的消息", {"api_session": "sess-A"}),
        ("2026-09-13T05:01:00Z", "in", "user", "第二会话的消息", {"api_session": "sess-B"}),
        ("2026-09-13T05:02:00Z", "in", "user", "没有会话标签的旧消息", {}),
    ]
    conn.executemany(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
        [(a, b_, c, d_, json.dumps(e, ensure_ascii=False)) for a, b_, c, d_, e in rows],
    )
    conn.commit()
    conn.close()
    log("  已写入 3 条：sess-A / sess-B / 无标签")

    log("\n── 3. 按会话过滤历史（history_for_session 是原生的吗？）──")
    for sid, want in (("sess-A", "第一会话"), ("sess-B", "第二会话"),
                      ("__legacy__", "没有会话标签")):
        st, b = req(f"/app/history?session_id={sid}", token=SECRET)
        ok = st == 200 and want in b
        check(ok, f"?session_id={sid} → 200 且拿到自己的消息", f"HTTP {st}")

    st, b_all = req("/app/history", token=SECRET)
    n_all = len(json.loads(b_all).get("messages", []))
    check(st == 200 and n_all == 3, "不带 session_id → 拿到全部 3 条", f"实际 {n_all}")

    log("\n── 4. 有数据时：兜底列表必须真的列出两个会话 + 旧消息组 ──")
    st, b = req("/app/sessions", token=SECRET)
    check(st == 200, "GET /app/sessions → 200", f"实际 {st}")
    d = json.loads(b)
    ids = sorted(s.get("id") for s in d.get("sessions", []))
    check(ids == ["__legacy__", "sess-A", "sess-B"], "列出了 sess-A / sess-B / __legacy__", str(ids))
    check(d.get("fallback_reason", "").startswith("upstream"),
          "带上了失败原因（前端能说清为什么降级）", str(d.get("fallback_reason")))

    log("\n── 5. 兜底不能绕过鉴权 ──")
    st, b = req("/app/sessions")
    check(st == 401, "没带密钥 → 401", f"实际 {st}")
    check("sess-A" not in b, "401 的 body 没泄露会话数据", b[:60])
    st, _ = req("/app/sessions", token="wrong")
    check(st == 401, "密钥错误 → 401", f"实际 {st}")

    log("\n── 6. 兜底不能干扰其他端点 ──")
    st, b = req("/app/history?session_id=sess-A", token=SECRET)
    check(st == 200 and "fallback" not in b, "/app/history 没被兜底污染")
    st, _ = req("/app/status", token=SECRET)
    check(st == 200, "/app/status 正常")
    st, _ = req("/app/sessions", token=SECRET, method="POST",
                body={"title": "x", "activate": True})
    check(st in (200, 500, 502, 503, 504), "POST /app/sessions（新建）不被兜底接管",
          f"实际 {st}（502 = api_loop 不在，符合预期）")

    log("\n── 7. 发一条带会话标签的新消息 ──")
    st, b = req("/app/send", token=SECRET, method="POST",
                body={"text": "写入 sess-A 的新消息", "api_session": "sess-A"})
    check(st == 200, "POST /app/send → 200", f"实际 {st}")
    st, b = req("/app/history?session_id=sess-A", token=SECRET)
    n = len(json.loads(b).get("messages", []))
    check(n == 2, "sess-A 现在有 2 条消息", f"实际 {n}")

    log("\n── 8. 兜底列表会跟着新消息更新 ──")
    st, b = req("/app/sessions", token=SECRET)
    d = json.loads(b)
    a = next((s for s in d.get("sessions", []) if s.get("id") == "sess-A"), None)
    check(a is not None and a.get("_n") == 2, "sess-A 的消息数变成 2", str(a and a.get("_n")))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

log("\n" + "=" * 78)
ok = sum(1 for p, _ in results if p)
log(f"结果：{ok}/{len(results)} 通过")
for p, label in results:
    if not p:
        log(f"  未通过：{label}")
log("=" * 78)

with open(os.path.join(HERE, "report.txt"), "w", encoding="utf-8") as f:
    f.write(f"sessioncheck: {ok}/{len(results)}\n")
    for p, label in results:
        f.write(f"{'PASS' if p else 'FAIL'}\t{label}\n")

sys.exit(0 if ok == len(results) else 1)
