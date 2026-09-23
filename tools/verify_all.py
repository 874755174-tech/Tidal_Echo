# -*- coding: utf-8 -*-
"""一次性跑完全部验收。

会依次跑：
  1. tools/secaudit.py              访问控制体检（21 项）
  2. tools/sessioncheck.py          会话数据层 + 兜底（19 项）
  3. tools/sessionfallback_check.py 兜底四场景 + 鉴权红线（34 项）
  4. tools/app_ext_check.py         P0 地基：五张表 + 身份层 + 迁移（55 项）
  5. tools/providers_check.py       P1 模型网关：允许列表 + 三格式 + 真 HTTP + 参数下发 + 原始帧诊断 + CoT 透传（166 项）
  6. tools/jscheck.py               web/ 下各页面（index / album / workshop / archive）内联 JS 语法
  7. tools/model_ui_check.mjs       设置页模型/参数前端（jsdom 真跑 index.html，35 项）
  8. tools/session_ui_check.mjs     会话归档/删除/改名前端（jsdom 真跑 index.html，40 项）
  9. tools/workshop_check.py        房间层：工作间 + MCP 门（存储 / 注册表 / 真 MCP 客户端 / REST 安全 / 接线）
 10. tools/archive_check.py         P2-0 导出 / 快照（一致快照 / 只读 / 密钥不进 URL / 空库也能导）
 11. tools/context_check.py         P2 ⑧ 上下文管理（注入 / 摘要 / 迁移 / **从出口倒着验**）
 12. tools/generate_check.py        P2 ⑨ 停止 / 重答 / 多版本（**假身体 + 慢上游**，
                                    专照"停止变成偷偷换模型重跑"这个病）
 13. tools/memory_check.py          P2 ⑩-a 记忆层（`source` 缝 / **真造 v3 老库升 v4** /
                                    写入路径 / 无 delete / salience 不外泄）
                                   ⚠️ 它**不起端口**（进程内 ASGI），不参与抢端口
 14. tools/activity_check.py        P2 ⑪ 自主活动带回上下文（窗口 / 确定性拼接 /
                                    **只插不删** / 不调 LLM / 前端卡片契约）
                                   ⚠️ 它**不起端口、不连外网**（本层不需要上游）
 15. tools/usage_check.py           P2 usage 记账（四家形状归一 / **命中率口径** /
                                    **缺口留痕** / **没有 HTTP 写入口** / 出站索要账单 /
                                    **线上真样本：中转站的"双命名"账单**）
                                   ⚠️ 它**不起端口、不连外网**
 16. git diff -- backend/ examples/ channel/  （红线，必须为空）

⚠️ 跑之前先确认 **8080 端口是空的** —— `tools/secaudit.py` 写死用它起测试服务，
   被占（比如那个 `kael-probe` 模型探测器还开着）会让 1/13 整套红，
   而且失败信息**不会告诉你是端口冲突**（2026-09-15 吃过这个假红）。
   （另有 8796/8797 归 archive_check、8798/8799 归 workshop_check、
   8800/8801/8802 归 context_check、8810~8813 归 generate_check，都是自用，不跟 8080 抢；
   memory_check / activity_check / usage_check 都不占端口。）
⚠️ 若你的环境设了 `HTTP_PROXY` / `HTTPS_PROXY`：`workshop_check.py` 会自己补
   `NO_PROXY=127.0.0.1,localhost`（官方 mcp SDK 的 httpx 默认 trust_env，会把本机地址
   也塞进代理 → 报出来只有一句 `ExceptionGroup`，看着像"门坏了"其实不是；2026-09-19 吃过）。

用法：.venv\\Scripts\\python.exe tools\\verify_all.py
"""
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

# jsdom 装在 WorkBuddy 托管 Node 的隔离 workspace 里（不污染用户环境），
# 那两个前端验收脚本用绝对路径 import 它 —— 这里把同样的路径传下去。
NODE = r"C:\Users\86187\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
NODE_MODULES = r"C:\Users\86187\.workbuddy\binaries\node\workspace\node_modules"

# (标签, [可执行文件, 脚本], 额外 env)
SUITES = [
    ("1/15  访问控制体检", [PY, "secaudit.py"], {}),
    ("2/15  会话数据层 + 兜底", [PY, "sessioncheck.py"], {}),
    ("3/15  兜底四场景 + 鉴权红线", [PY, "sessionfallback_check.py"], {}),
    ("4/15  P0 地基：五张表 + 身份层", [PY, "app_ext_check.py"], {}),
    ("5/15  P1 模型网关：允许列表 + 三格式 + 真 HTTP + 参数下发 + 原始帧诊断 + CoT 透传", [PY, "providers_check.py"], {}),
    ("6/15  web/ 各页面内联 JS 语法", [PY, "jscheck.py"], {}),
    ("7/15  设置页模型/参数前端（jsdom 真跑）", [NODE, "model_ui_check.mjs"],
     {"NODE_PATH": NODE_MODULES}),
    ("8/15  会话归档/删除/改名前端（jsdom 真跑）", [NODE, "session_ui_check.mjs"],
     {"NODE_PATH": NODE_MODULES}),
    ("9/15  房间层：工作间 + MCP 门", [PY, "workshop_check.py"],
     {"RELAY_WORKSHOP_DIR": ""}),
    ("10/15 P2-0 导出 / 快照：一致快照 + 只读 + 密钥不进 URL", [PY, "archive_check.py"], {}),
    ("11/15 P2 ⑧ 上下文管理：注入 / 摘要 / 迁移（从出口倒着验）", [PY, "context_check.py"], {}),
    ("12/15 P2 ⑨ 停止/重答/多版本（假身体 + 慢上游，专照「偷偷换模型重跑」）",
     [PY, "generate_check.py"], {}),
    ("13/15 P2 ⑩-a 记忆层：source 缝 + 迁移 v3→v4 + 写入路径（进程内 ASGI，不占端口）",
     [PY, "memory_check.py"], {}),
    ("14/15 P2 ⑪ 自主活动带回上下文：窗口 + 确定性拼接 + 只插不删（不起端口、不连外网）",
     [PY, "activity_check.py"], {}),
    ("15/15 P2 usage 记账：四家归一 + 命中率口径 + 缺口留痕 + 无写入口"
     "（不起端口、不连外网）",
     [PY, "usage_check.py"], {}),
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
        print(f"[跳过] 没找到 node（{NODE}）")
        skipped.append(label)
        continue
    env = dict(os.environ)
    env.update(extra_env)
    # 🔴 有意**不**给子进程塞 PYTHONIOENCODING（2026-09-19 试过又撤了）：
    #    "房子里的 print 必须 GBK 安全"（见 扩展边界.md 红线）是要被**验**的约束。
    #    给它塞 `gbk:replace` 会把"编不出 → UnicodeEncodeError → 房子起不来"
    #    软化成"编不出 → 变成 ? → 房子照样起" ——
    #    恰好把 workshop_check 的 E8 组（空库能不能起来）要抓的那个 bug 藏掉了。
    #    子进程自己的报告打印靠各脚本头部的 sys.stdout.reconfigure(errors="replace") 兜。
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
    print("[有改动]")
    print(out)
    fails.append("红线：原生目录被动过")
else:
    print("[OK] 空 — 一个字符都没动")

print("\n" + "=" * 78)
if skipped:
    print("以下套件被跳过（环境缺依赖）：")
    for s in skipped:
        print("  [跳过]", s)
if fails:
    print("总检查：未通过")
    for f in fails:
        print("  [失败]", f)
    sys.exit(1)
print("总检查：全部通过 [OK]")
print("=" * 78)
