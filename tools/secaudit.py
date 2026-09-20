# -*- coding: utf-8 -*-
"""
kael-home 访问控制体检（可重复运行）

用途：换了密钥、或以后加了新接口，重跑一遍确认「只有持钥匙的人能进屋」。
不依赖 shell / 不连公网，纯本地起一个 relay 实例自测。

用法（PowerShell）：
    cd "C:\\Users\\86187\\WorkBuddy\\2026-07-26-20-49-33\\kael-home"
    .\\.venv\\Scripts\\python.exe tools\\secaudit.py

结果同时打印到终端、并写入本目录 report.txt（已被 .gitignore 忽略）。
"""
import json
import os
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

HERE = os.path.dirname(os.path.abspath(__file__))          # = kael-home/tools
ROOT = os.path.dirname(HERE)                               # = kael-home
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SECRET_GOOD = "CORRECT_SECRET_XYZ"
SECRET_BAD = "WRONG_SECRET_ABC"
PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"

lines = []


def log(s=""):
    lines.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def req(path, token=None, method="GET", body=None, timeout=6):
    """返回 (status, body_text)。不抛异常，方便断言。"""
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 启动服务
log("=" * 68)
log("kael-home 访问控制体检")
log("=" * 68)

RUN_DIR = os.path.join(ROOT, "_runtime")
os.makedirs(RUN_DIR, exist_ok=True)

PROC_ENV = os.environ.copy()
PROC_ENV.update({
    "PORT": str(PORT),
    "RELAY_SECRET": SECRET_GOOD,
    "RELAY_DB": os.path.join(RUN_DIR, "relay.db"),
    "RELAY_UPLOAD_DIR": os.path.join(RUN_DIR, "uploads"),
    "RELAY_BRAIN_FILE": os.path.join(RUN_DIR, "brain_target"),
    "LOOP_CONFIG": os.path.join(RUN_DIR, "api_loop.config.json"),
})

# 不用 uvicorn --app-dir（Windows + 非 ASCII 路径下会解析失败），
# 改成「先 sys.path 注入 deploy/，再程序化 uvicorn.run」。已验证可行。
launcher = os.path.join(RUN_DIR, "_launch.py")
with open(launcher, "w", encoding="utf-8") as f:
    f.write(
        "import sys\n"
        f"sys.path.insert(0, r'{os.path.join(ROOT, 'deploy')}')\n"
        "import uvicorn, serve\n"
        f"uvicorn.run(serve.app, host='127.0.0.1', port={PORT}, log_level='warning')\n"
    )

log(f"[启动] 解释器 = {PY}")
proc = subprocess.Popen(
    [PY, launcher], cwd=ROOT, env=PROC_ENV,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
)

ready = False
for _ in range(30):
    time.sleep(0.5)
    st, _b = req("/healthz")
    if st == 200:
        ready = True
        break

log(f"[启动] relay 就绪 = {ready}")
if not ready:
    try:
        log(proc.stdout.read().decode("utf-8", "replace")[:2000])
    except Exception:
        log("(读不到子进程输出)")
    proc.kill()
    sys.exit(1)

# ---------------------------------------------------------------- 测试
results = []


def check(name, ok, detail):
    results.append((name, ok, detail))
    log(f"  {'PASS' if ok else 'FAIL'}  {name}")
    log(f"        {detail}")


log("")
log("-- A. 网页本体 --")
st, body = req("/")
check("GET / 可打开网页壳子", st == 200, f"HTTP {st}, {len(body)} 字节")
st, _ = req("/sw.js")
check("GET /sw.js 可打开", st == 200, f"HTTP {st}")
st, html = req("/")
check("网页源码不含真实密钥", SECRET_GOOD not in html,
      "未发现密钥内联" if SECRET_GOOD not in html else "发现密钥泄露!")

log("")
log("-- B. 读接口（无密钥应 401）--")
for path in ["/app/history", "/app/status", "/app/brain", "/app/sessions",
             "/app/loop_config", "/app/vapid_public"]:
    st, b = req(path)
    check(f"无密钥 GET {path}", st == 401, f"HTTP {st} {b[:60]}")

log("")
log("-- C. 写接口（无密钥应 401）--")
for path, body in [("/app/send", {"text": "x"}),
                   ("/app/brain", {"target": "loop"}),
                   ("/app/ping", {})]:
    st, b = req(path, method="POST", body=body)
    check(f"无密钥 POST {path}", st == 401, f"HTTP {st} {b[:60]}")

log("")
log("-- D. 错误密钥应 401 --")
st, b = req("/app/history", token=SECRET_BAD)
check("错误密钥 GET /app/history", st == 401, f"HTTP {st} {b[:60]}")
st, b = req("/app/send", token=SECRET_BAD, method="POST", body={"text": "x"})
check("错误密钥 POST /app/send", st == 401, f"HTTP {st} {b[:60]}")

log("")
log("-- E. 正确密钥应 200 --")
st, b = req("/app/history", token=SECRET_GOOD)
check("正确密钥 GET /app/history", st == 200, f"HTTP {st} {b[:80]}")
st, b = req("/app/status", token=SECRET_GOOD)
check("正确密钥 GET /app/status", st == 200, f"HTTP {st} {b[:80]}")
st, b = req("/app/brain", token=SECRET_GOOD)
check("正确密钥 GET /app/brain", st == 200, f"HTTP {st} {b[:80]}")

log("")
log("-- F. ?token= 查询参数（EventSource 妥协通道）--")
st, b = req(f"/app/history?token={SECRET_GOOD}")
check("?token= 正确密钥", st == 200, f"HTTP {st} {b[:80]}")
st, b = req(f"/app/history?token={SECRET_BAD}")
check("?token= 错误密钥", st == 401, f"HTTP {st} {b[:80]}")

log("")
log("-- G. 附件（鉴权必须在文件查找之前）--")
st, b = req("/uploads/nonexistent.png")
check("无密钥 GET /uploads/... 应为 401（不是 404）", st == 401,
      f"HTTP {st} {b[:60]} <- 401 才对；404 说明鉴权在查找之后")

log("")
log("-- H. 未鉴权端点 --")
st, b = req("/healthz")
check("/healthz 公开（设计如此）", st == 200, f"HTTP {st} {b[:80]}")

# ---------------------------------------------------------------- 汇总
proc.terminate()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()

passed = sum(1 for _n, ok, _d in results if ok)
total = len(results)
log("")
log("=" * 68)
log(f"总计：{passed}/{total} 通过")
if passed < total:
    log("")
    log("未通过项：")
    for n, ok, d in results:
        if not ok:
            log(f"  X {n} -> {d}")
log("=" * 68)

with open(os.path.join(HERE, "report.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
