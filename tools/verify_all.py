# -*- coding: utf-8 -*-
"""一次性跑完全部验收（B 方案交付前的总检查）。

会依次跑：
  1. tools/secaudit.py              访问控制体检（21 项）
  2. tools/sessioncheck.py          会话数据层 + 兜底（19 项）
  3. tools/sessionfallback_check.py 兜底四场景 + 鉴权红线（34 项）
  4. tools/jscheck.py               index.html 内联 JS 语法
  5. git diff -- backend/ examples/ channel/  （红线，必须为空）

用法：.venv\\Scripts\\python.exe tools\\verify_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

SUITES = [
    ("1/4  访问控制体检", ["secaudit.py"]),
    ("2/4  会话数据层 + 兜底", ["sessioncheck.py"]),
    ("3/4  兜底四场景 + 鉴权红线", ["sessionfallback_check.py"]),
    ("4/4  index.html 内联 JS 语法", ["jscheck.py"]),
]

fails = []
for label, args in SUITES:
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    r = subprocess.run([PY, os.path.join(HERE, args[0])], cwd=ROOT)
    if r.returncode != 0:
        fails.append(label)

print("\n" + "=" * 78)
print("红线：backend/ examples/ channel/ 必须零改动")
print("=" * 78)
d = subprocess.run(["git", "diff", "--stat", "--", "backend/", "examples/", "channel/"],
                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
out = (d.stdout or "").strip()
if out:
    print("❌ 有改动：")
    print(out)
    fails.append("红线：原生目录被动过")
else:
    print("✅ 空 — 一个字符都没动")

print("\n" + "=" * 78)
if fails:
    print("总检查：未通过")
    for f in fails:
        print("  ✗", f)
    sys.exit(1)
print("总检查：全部通过 ✅")
print("=" * 78)
