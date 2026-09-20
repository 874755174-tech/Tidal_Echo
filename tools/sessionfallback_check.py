#!/usr/bin/env python3
"""会话列表兜底 · 三场景验收
================================================================================
验证 deploy/sessions_fallback.py 在三种情况下都给出「可理解的结果」：

  场景 1  api_loop 不在        → 200，body 里 fallback=true，列表从数据库算出来
  场景 2  api_loop 在但报错    → 200，body 里 fallback=true，列表仍然是真的
  场景 3  api_loop 正常        → 200，body 里**没有** fallback，一个字节都没被改

同时验证：
  · 前缀剥离后兜底仍然生效（模拟 Zeabur 上 /relay/app/sessions）
  · 未鉴权时仍然是 401（兜底**不能**绕过鉴权）
  · 前端确实会在降级/失败时给出提示文案

用法：.venv\\Scripts\\python.exe tools\\sessionfallback_check.py
"""
from __future__ import annotations

import json
import os
import socket
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

import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"

# ⚠️ 必须用 kael-home 自己的 .venv（里面装了 fastapi/uvicorn）。
#    托管解释器 C:\Users\86187\.workbuddy\binaries\python\... 只有标准库。
PROJECT_VENV = REPO / ".venv" / "Scripts" / "python.exe"
PY = PROJECT_VENV if PROJECT_VENV.exists() else Path(sys.executable)

SECRET = "fallback-check-secret"

results: list[tuple[bool, str]] = []

# boot() 需要把"这个场景的 LOOP_INGEST_URL"显式传进去（不能只靠 os.environ，
# 因为 Python 启动子进程时会继承，但我们要保证每个场景互不串味）
boot_env_overrides: dict[str, str] = {}


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"   {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def boot(tmp: Path, *, public_prefix: str, port: int,
         fake_loop: str = "none") -> subprocess.Popen:
    """起一个 serve:app。

    fake_loop:
        none  → 什么都不起（api_loop 不在）
        error → 起一个 500 的假 api_loop
        ok    → 起一个正常的假 api_loop
    """
    env = dict(os.environ)
    env["RELAY_SECRET"] = SECRET
    env["RELAY_PORT"] = str(free_port())
    env["RELAY_DB"] = str(tmp / "test.db")
    env["RELAY_WEB_DIR"] = str(REPO / "web")
    env["RELAY_PUBLIC_PREFIX"] = public_prefix
    env["PYTHONPATH"] = str(DEPLOY)
    env.update(boot_env_overrides)

    launcher = tmp / "_launch.py"
    launcher.write_text(
        "import sys, uvicorn\n"
        f"sys.path.insert(0, r'{DEPLOY}')\n"
        "import serve\n"
        f"uvicorn.run(serve.app, host='127.0.0.1', port={port}, log_level='warning')\n",
        encoding="utf-8",
    )
    return subprocess.Popen([str(PY), str(launcher)], env=env,
                            cwd=str(DEPLOY),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fake_loop_server(tmp: Path, port: int, *, mode: str) -> subprocess.Popen:
    src = tmp / f"_fakeloop_{mode}.py"
    if mode == "error":
        handler = "        self.send_response(500)\n        self.end_headers()\n        self.wfile.write(b'boom')\n"
    else:
        payload = json.dumps({
            "active_session": "sess-real",
            "sessions": [{"id": "sess-real", "title": "真会话", "since_id": 3,
                          "created_at": "2026-09-12T10:00:00", "pinned": False}],
        })
        handler = (
            "        b = " + repr(payload.encode()) + "\n"
            "        self.send_response(200)\n"
            "        self.send_header('content-type','application/json')\n"
            "        self.send_header('content-length',str(len(b)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b)\n"
        )
    src.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        + handler +
        "    def log_message(self,*a): pass\n"
        f"HTTPServer(('127.0.0.1',{port}),H).serve_forever()\n",
        encoding="utf-8",
    )
    return subprocess.Popen([str(PY), str(src)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def seed(tmp: Path) -> None:
    """往测试库里塞两条不同会话的消息（直接建表，不依赖后端启动）。"""
    import sqlite3
    db = tmp / "test.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, direction TEXT, kind TEXT, text TEXT, meta TEXT
        );
        """
    )
    rows = [
        ("2026-09-10T09:00:00", "in", "text", "会话A 第一句", '{"api_session":"sess-A"}'),
        ("2026-09-10T09:05:00", "out", "text", "会话A 第二句", '{"api_session":"sess-A"}'),
        ("2026-09-11T20:00:00", "in", "text", "会话B 第一句", '{"api_session":"sess-B"}'),
        ("2026-09-09T08:00:00", "in", "text", "没有标签的旧消息", '{}'),
    ]
    con.executemany(
        "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)", rows)
    con.commit()
    con.close()


def req(url: str, *, token: str | None = None):
    r = urllib.request.Request(url)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def scenario(name: str, *, mode: str, prefix: str = "") -> None:
    print(f"\n【{name}】")
    # ⚠️ 运行目录放**系统 temp**（2026-09-13 改）：以前是 REPO/_runtime，
    #    会往仓库里落测试数据库。虽然这里不删东西，但测试不该污染项目目录。
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix=f"kaelhome-fb-{mode or 'x'}-"))
    tmp.mkdir(parents=True, exist_ok=True)
    seed(tmp)

    port = free_port()
    loop_proc = None
    # ⚠️ 变量名必须是 RELAY_LOOP_INGEST_URL（app.py:88）。
    #    loop_base_url() 从它取 scheme+netloc 拼 base（app.py:444-448）。
    #    写成 LOOP_INGEST_URL 是**静默失效**：后端会用默认的 127.0.0.1:3020，
    #    表现为"每个场景都 502"，很容易误判成兜底写错了。
    if mode != "none":
        loop_port = free_port()
        loop_proc = fake_loop_server(tmp, loop_port, mode=mode)
        wait_port(loop_port)
        env_loop = f"http://127.0.0.1:{loop_port}/loop/ingest"
    else:
        # 指向一个死端口，确保"连不上"
        env_loop = f"http://127.0.0.1:{free_port()}/loop/ingest"
    boot_env_overrides["RELAY_LOOP_INGEST_URL"] = env_loop

    srv = boot(tmp, public_prefix=prefix, port=port)
    try:
        if not wait_port(port):
            check(False, "服务起来了吗", "超时")
            return

        base = f"http://127.0.0.1:{port}{prefix}"
        st, body = req(f"{base}/app/sessions", token=SECRET)
        check(st == 200, f"GET {prefix or ''}/app/sessions → 200", f"实际 {st}")

        try:
            d = json.loads(body)
        except Exception as e:
            check(False, "返回是合法 JSON", str(e))
            return

        ids = sorted(s.get("id") for s in d.get("sessions", []))
        if mode == "ok":
            check("fallback" not in d, "透传：没有 fallback 标记（原样）")
            check("sess-real" in ids, "透传：看到 api_loop 给的会话", str(ids))
        else:
            check(d.get("fallback") is True, "兜底：body 里有 fallback=true")
            check("sess-A" in ids and "sess-B" in ids,
                  "兜底：从数据库算出了 sess-A / sess-B", str(ids))
            check("__legacy__" in ids, "兜底：无标签旧消息单独成组")
            if mode == "error":
                check("fallback_reason" in d, "兜底：带上了失败原因",
                      str(d.get("fallback_reason")))

        # 鉴权不能被兜底绕过
        st401, b401 = req(f"{base}/app/sessions")
        check(st401 == 401, "没带密钥仍然 401（兜底不绕过鉴权）", f"实际 {st401}")
        check("\"sessions\"" not in b401,
              "401 的 body 里没有泄露会话列表", b401[:80])

        st401b, _ = req(f"{base}/app/sessions", token="wrong-key")
        check(st401b == 401, "密钥错误仍然 401", f"实际 {st401b}")
    finally:
        srv.terminate()
        if loop_proc:
            loop_proc.terminate()
        boot_env_overrides.pop("RELAY_LOOP_INGEST_URL", None)


def frontend_notice() -> None:
    print("\n【前端提示文案】")
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    check("sessionNotice" in html, "提示元素已加进页面")
    check("setSessionNotice" in html, "提示函数已定义")
    # 2026-09-13：前端改成"新端点优先、退回老端点"，fallback 的文案抽成了
    # FALLBACK_NOTICE 常量并在两处按 `d && d.fallback` 判断。断言跟着改，
    # 但**验的还是同一件事**：前端必须读后端的 fallback 标记。
    check("FALLBACK_NOTICE" in html and "d.fallback" in html,
          "会读后端的 fallback 标记")
    check("暂时读不到会话列表" in html, "连接失败时有可读的中文提示")
    check("AI 身体暂时不在" in html, "降级时说明了原因")
    # 原来的静默 catch 必须已经不在了
    check("apiSessions = [];\n    activeApiSession = LEGACY_SESSION_ID;\n  }" not in html,
          "旧的静默 catch 已被替换")


def main() -> int:
    print("=" * 78)
    print("会话列表兜底 · 三场景验收")
    print("=" * 78)

    scenario("场景 1：api_loop 不在", mode="none")
    scenario("场景 2：api_loop 在但报错", mode="error")
    scenario("场景 3：api_loop 正常（必须完全透传）", mode="ok")
    scenario("场景 4：Zeabur 前缀 /relay + api_loop 不在", mode="none", prefix="/relay")
    frontend_notice()

    ok = sum(1 for p, _ in results if p)
    print("\n" + "=" * 78)
    print(f"结果：{ok}/{len(results)} 通过")
    for p, label in results:
        if not p:
            print(f"  未通过：{label}")
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
