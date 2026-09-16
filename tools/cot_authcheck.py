# -*- coding: utf-8 -*-
"""一键定位 401：到底是「key 不对」还是「env 改了没重新部署」

用法（在 kael-home 目录下，**把你在 Zeabur 面板里看到的 RELAY_SECRET 填进去**）：

    .venv\\Scripts\\python.exe tools\\cot_authcheck.py --key "<从面板复制的值>"

它会做三件事，每件都给出明确结论：

  1. 打印这把 key 的**指纹**（长度 / 首末字符 / sha256 前 12 位）
     → 和你在 Zeabur 面板里看到的对一下（🔴 不会打印完整密钥）
  2. 分别用 **Bearer header** 和 **?token=** 打 `/app/ext/providers`
     → 两种都 401 = 鉴权通路正常、纯粹是「值不等」；某种能通 = 投递姿势问题
  3. 给出一份**按可能性排序**的排查清单

改完 Zeabur 环境变量后**必须重新部署**才生效 —— 这是最常见的 401 根因：
面板上看到的是新值，容器进程里跑的还是旧值。
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cot_probe import BASE, sanitize_secret   # 复用同一套清洗逻辑，避免两处漂移


def hit(path: str, headers=None, timeout: int = 25) -> tuple:
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="从 Zeabur 面板复制的 RELAY_SECRET")
    a = ap.parse_args()

    raw = a.key or ""
    key = sanitize_secret(raw)
    print("=" * 72)
    print("这一步只做「鉴权」诊断，不打上游、不花钱")
    print("=" * 72)
    print(f"目标            : {BASE}")
    print(f"长度            : {len(key)}    (原始 {len(raw)}，清洗掉 {len(raw) - len(key)})")
    if key:
        print(f"首/末字符       : {key[0]!r} / {key[-1]!r}")
        print(f"内部空格数      : {key.count(' ')}")
        print(f"sha256[:12]     : {hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}")
        print(f"可见预览        : {key[:4]}……{key[-4:]}" if len(key) > 10 else f"可见预览        : {key[:2]}……")
    print()

    print("─ 投递方式 1：Authorization: Bearer <key>")
    s1, b1 = hit("/app/ext/providers", {"Authorization": "Bearer " + key})
    print(f"  HTTP {s1}  {b1[:120]}")

    print("─ 投递方式 2：?token=<key>（原版 app.py 也认这种）")
    s2, b2 = hit("/app/ext/providers?token=" + urllib.parse.quote(key))
    print(f"  HTTP {s2}  {b2[:120]}")
    print()

    if s1 == 200 or s2 == 200:
        print("✅ 通了！key 是对的。直接继续跑 cot_probe.py 打诊断端点。")
        if s1 != 200:
            print("   ℹ️ 但 Bearer 那种投递没通 → 头里可能有被剥掉的字符（少见）")
        return 0

    if s1 == 400 or s2 == 400:
        print("⚠️ 400 = 请求头/URL 里有非法字符。本脚本已清洗过，仍 400 请把上面原话发我。")
        return 1

    # 两种都 401 → 值不等
    print("❌ 两种投递都是 401 → **鉴权通路正常，问题就是「这把值 ≠ 容器里的 RELAY_SECRET」**")
    print()
    print("按可能性排序，逐条排查：")
    print("  ① 🔴 改过 Zeabur 环境变量但**没有重新部署** —— 最常见。")
    print("     环境变量是注入容器进程的，不重启就一直用旧值。")
    print("     面板显示的是新值，容器里跑的是旧值 → 永远 401。")
    print("     办法：Zeabur 面板 → 服务 → 重新部署（Redeploy）。")
    print("  ② 复制的**不是** `RELAY_SECRET`，而是 `PROVIDER_RELAY_KEY` / `PASSWORD`：")
    print("     `RELAY_SECRET` 才是网页登录 + `/app/ext/*` 的鉴权密钥。")
    print("  ③ 面板里的值本身就含**不可见字符**（从别处粘进去时带上的）：")
    print("     最稳的修法是**在面板里删掉整行、手打一遍新值**（用纯 ASCII / 数字），")
    print("     再重新部署，然后用新值跑本脚本。")
    print("  ④ 你输的值里少了/多了字符（对比上面的长度与 sha256 前 12 位）：")
    print("     本脚本打印的「长度」要和面板里那条值的**字符数**一致。")
    print()
    print("👉 最快的一步：先去 Zeabur 点一次「重新部署」，再原样重跑本命令。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
