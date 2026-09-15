# -*- coding: utf-8 -*-
"""一次性跑完全部验收。

会依次跑：
  1. tools/secaudit.py              访问控制体检（21 项）
  2. tools/sessioncheck.py          会话数据层 + 兜底（19 项）
  3. tools/sessionfallback_check.py 兜底四场景 + 鉴权红线（34 项）
  4. tools/app_ext_check.py         P0 地基：四张表 + 身份层 + 迁移（55 项）
  5. tools/providers_check.py       P1 模型网关：允许列表 + 三格式 + 真 HTTP + 参数下发（131 项）
  6. tools/jscheck.py               index.html 内联 JS 语法
  7. tools/model_ui_check.mjs       设置页模型/参数前端（jsdom 真跑 index.html，35 项）
  8. tools/session_ui_check.mjs     会话归档/删除/改名前端（jsdom 真跑 index.html，40 项）
  9. git diff -- backend/ examples/ channel/  （红线，必须为空）

⚠️ 跑之前先确认 **8080 端口是空的** —— `tools/secaudit.py` 写死用它起测试服务，
   被占（比如那个 `kael-probe` 模型探测器还开着）会让 1/8 整套红，
   而且失败信息**不会告诉你是端口冲突**（2026-09-15 吃过这个假红）。

用法：.venv\\Scripts\\python.exe tools\\verify_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

# jsdom 装在 WorkBuddy 托管 Node 的隔离 workspace 里（不污染用户环境），
# 那两个前端验收脚本用绝对路径 import 它 —— 这里把同样的路径传下去。
NODE = r"C:\Users\86187\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
NODE_MODULES = r"C:\Users\86187\.workbuddy\binaries\node\workspace\node_modules"

# (标签, [可执行文件, 脚本], 额外 env)
SUITES = [
    ("1/8  访问控制体检", [PY, "secaudit.py"], {}),
    ("2/8  会话数据层 + 兜底", [PY, "sessioncheck.py"], {}),
    ("3/8  兜底四场景 + 鉴权红线", [PY, "sessionfallback_check.py"], {}),
    ("4/8  P0 地基：四张表 + 身份层", [PY, "app_ext_check.py"], {}),
    ("5/8  P1 模型网关：允许列表 + 三格式 + 真 HTTP + 参数下发", [PY, "providers_check.py"], {}),
    ("6/8  index.html 内联 JS 语法", [PY, "jscheck.py"], {}),
    ("7/8  设置页模型/参数前端（jsdom 真跑）", [NODE, "model_ui_check.mjs"],
     {"NODE_PATH": NODE_MODULES}),
    ("8/8  会话归档/删除/改名前端（jsdom 真跑）", [NODE, "session_ui_check.mjs"],
     {"NODE_PATH": NODE_MODULES}),
]

fails = []
skipped = []
for label, args, extra_env in SUITES:
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    exe = args[0]
    if exe == NODE and not os.path.exists(NODE):
        # 环境缺 node 时**响亮地跳过**，既不要假装通过、也不要假装失败
        print(f"⚠️ 跳过：没找到 node（{NODE}）")
        skipped.append(label)
        continue
    env = dict(os.environ)
    env.update(extra_env)
    r = subprocess.run([exe, os.path.join(HERE, args[1])], cwd=ROOT, env=env)
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
if skipped:
    print("以下套件被跳过（环境缺依赖）：")
    for s in skipped:
        print("  ⚠️", s)
if fails:
    print("总检查：未通过")
    for f in fails:
        print("  ✗", f)
    sys.exit(1)
print("总检查：全部通过 ✅")
print("=" * 78)
