# -*- coding: utf-8 -*-
"""CoT 原始帧诊断 —— 一键跑（只读，不改任何东西）

为什么要有这个脚本（而不是让你手打 curl）：
  🔴 PowerShell 里 `curl.exe ... | python -m json.tool` 是**经典坑** —— PS 会把
     curl 的字节流按行拆成对象再喂给 python，`json.tool` 收到的是分行的字符串
     而不是完整 JSON，于是**卡住不打印**（看着就像"什么都没返回"）。
     本脚本用 Python 自己发请求、自己解析，不经过任何管道。

用法（在 kael-home 目录下）：

    .venv\\Scripts\\python.exe tools\\cot_probe.py --model claude-opus-4-6-thinking

secret 走**环境变量**或交互输入（输入时不回显），**绝不写进命令历史、不进 git**：

    $env:KK = "<你的 RELAY_SECRET>"; .venv\\Scripts\\python.exe tools\\cot_probe.py --model xxx

不加 --key 时会提示你输入（隐藏回显）。结果同时打印并写到 `tools/cot_probe_result.json`。
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("KAELHOME_BASE", "https://kaelnlily79.zeabur.app").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cot_probe_result.json")

# 🔴 不可见字符清洗器 —— 与 `web/index.html` 的 `sanitizeSecret()` **同一套逻辑**。
#
# 为什么必须有（2026-09-15 真实踩到）：从 Zeabur env 面板 / 备忘录 / 微信复制密钥时，
# **行尾换行**和**零宽空格 / NBSP / BOM** 会一起被复制进来。后果分三种：
#   · 尾部换行     → `urllib` 本地就抛 ValueError（Invalid header value）
#   · 零宽空格/BOM → 本地抛 UnicodeEncodeError（latin-1 编不了）
#   · NBSP         → 服务端 500
#   · 多行粘贴     → HTTP 层/边缘直接 **400 Bad Request**（根本不进我们的应用）
#
# 🔴🔴 只清**首尾**的换行/空白，**绝不动字符串内部**（2026-09-16 修）：
#   第一版写成"把所有空白一律删掉"，结果把**合法含空格的密钥**也改了 ——
#   用户的 `RELAY_SECRET` 本身就长这样：`ewfD......nk j6`（**中间有一个空格**）。
#   把那个空格删掉 → 长度对了、内容错了 → 稳定 401，而且看着像"密码不对"。
#   ⚠️ 教训：**空格可能是内容的一部分，不能当脏字符**。只裁剪首尾。
#
# 真正该清的是这两类（它们**不可能**是密钥内容）：
#   · 首尾的换行 / 制表 / 空格（复制时带上的）
#   · 任何位置的**零宽字符**与 BOM（肉眼不可见，且 HTTP 头不允许）
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u180e"     # 零宽 + BOM
_BAD_ANYWHERE = "\u2028\u2029"                            # 行分/段分（头里非法）


def sanitize_secret(s: str) -> str:
    """清洗密钥：去掉**零宽字符/BOM**（任何位置）与**首尾空白**。

    🔴 **保留内部的空格与可见字符** —— 它们可能是密钥内容本身。
    ⚠️ 但**内部的换行必须去掉**：它不可能是密钥内容，而 HTTP 头里出现换行
       会被边缘直接判 400（2026-09-15 用户真实踩到：多行粘贴）。
       也就是说"空格保留、换行删除"这条区别对待是**故意的**，别改成一刀切。
    """
    t = s or ""
    for ch in _ZERO_WIDTH + _BAD_ANYWHERE:
        t = t.replace(ch, "")
    t = t.replace("\u00a0", " ").replace("\u202f", " ")   # NBSP → 普通空格（不删，只归一）
    t = t.replace("\r", "").replace("\n", "")              # 🔴 内部换行：删（不可能是内容）
    return t.strip(" \t\v\f")                              # ⚠️ 只裁首尾（空格可能是内容）


def call(path: str, key: str, payload: dict, timeout: int = 60) -> tuple:
    """返回 (status, 解析后的对象或原始文本)。"""
    req = urllib.request.Request(
        BASE + path, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        raw = r.read().decode("utf-8", "replace")
        status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        return 0, {"_transport_error": f"{type(e).__name__}: {e}"}
    try:
        return status, json.loads(raw)
    except Exception:
        return status, {"_raw_text": raw[:4000]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="模型名（从 GET /app/ext/providers 抄准确）")
    ap.add_argument("--key", default="", help="RELAY_SECRET（不填则交互输入，隐藏回显）")
    ap.add_argument("--key-file", default="",
                    help="从文件读 secret（**最稳的方式**；避开终端粘贴夹带换行）")
    ap.add_argument("--prompt", default="在吗")
    ap.add_argument("--stream", default="true", choices=["true", "false"])
    ap.add_argument("--provider", default="relay")
    ap.add_argument("--max-tokens", type=int, default=32, help="省 token（诊断不需要长回答）")
    ap.add_argument("--max-frames", type=int, default=80)
    a = ap.parse_args()

    key_raw = (a.key or os.environ.get("KK") or os.environ.get("RELAY_SECRET") or "")
    kf = a.key_file
    # 🆕 没显式给 --key-file 时，**自动找项目根下的 _k.txt**（省得每次带参数）
    if not kf and not key_raw.strip():
        guess = os.path.join(os.path.dirname(HERE), "_k.txt")
        if os.path.exists(guess):
            kf = guess
            print(f"📄 自动读到 {guess}（要换密钥就改这个文件）")
    if kf:
        try:
            with open(kf, "r", encoding="utf-8") as f:
                key_raw = f.read()
        except Exception as e:
            print(f"❌ 读不到 {kf}：{type(e).__name__}: {e}")
            return 2
    if not key_raw.strip():
        try:
            key_raw = getpass.getpass("RELAY_SECRET（输入不回显，不落盘）: ")
        except Exception:
            key_raw = ""
    raw_s = key_raw or ""
    key = sanitize_secret(raw_s)

    # ── 密钥自证（🔴 绝不打印密钥本身，只打印可对账的指纹）──────────────────
    if not key:
        print("❌ 没拿到 secret。")
        return 2
    cleaned = len(raw_s) - len(key)
    print(f"🔑 长度 {len(key)}"
          f" | 首字符 {key[0]!r} 末字符 {key[-1]!r}"
          f" | sha256[:12] = {hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}")
    if cleaned:
        print(f"🧹 清洗掉 {cleaned} 个字符（首尾空白/零宽/BOM）")
    if " " in key:
        # ⚠️ 这一条很关键：密钥里合法带空格时，**不能被清掉**
        print(f"ℹ️  密钥内部含 {key.count(' ')} 个空格（已**保留** —— 它可能是内容的一部分）")
    if len(key) <= 3:
        print("⚠️ 长度异常短 —— 多半是交互粘贴没把整串交进来。"
              "改用文件方式：把密钥存成一行文本，然后 --key-file <文件名>")
    if "\n" in raw_s or "\r" in raw_s:
        print("ℹ️ 读到的内容里有多行（已只保留内容、裁掉首尾换行）")

    print(f"→ 目标 {BASE}")
    print(f"→ 模型 {a.model}   stream={a.stream}   max_tokens={a.max_tokens}")

    # ① 先拉允许列表，顺手核对模型名（名字错一个字符 upstream 会 400 model_not_allowed）
    req = urllib.request.Request(BASE + "/app/ext/providers",
                                 headers={"Authorization": "Bearer " + key})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        plist = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        code = e.code
        body = e.read()[:300].decode("utf-8", "replace")
        print(f"❌ 拉允许列表失败 HTTP {code}：{body}")
        # 🔴 按状态码分诊（2026-09-15/16：上一版把 400 也写成"secret 不对"，误导过一次）
        if code == 401:
            print("   → 服务端**不认识这把 key**。依次排查：")
            print("     ① 你用的这把是不是 `RELAY_SECRET`？（不是 PROVIDER_RELAY_KEY，也不是 PASSWORD）")
            print("     ② 确认你输入/文件里的值与 Zeabur 面板里 `RELAY_SECRET` 的**可见部分一致**")
            print("        （本脚本已打印长度 + 首末字符 + sha256 前 12 位，可逐项对）")
            print("     ③ 🔴 **改过 Zeabur 环境变量后必须重新部署才生效** ——")
            print("        环境变量是注入到容器进程里的，不重启仍用旧值。")
            print("     ④ 换一把新值试试（避免不可见字符残留）：改 env → 重新部署 → 重跑本脚本")
        elif code == 400:
            print("   → 400 通常是**请求头里混进了非法字符**（换行/零宽）。")
            print("     本脚本已自动清洗；若仍 400，检查 KAELHOME_BASE 是否多了空格或引号。")
        elif code in (502, 503, 504):
            print("   → 房子在重启 / 上游不通；稍等 30 秒重试（Zeabur redeploy 空窗期）")
        else:
            print(f"   → 非预期状态码，把上面这行原话发我")
        return 1
    except Exception as e:
        print(f"❌ 连不上 {BASE}：{type(e).__name__}: {e}")
        return 1

    relay = next((p for p in (plist.get("providers") or []) if p.get("id") == a.provider), None)
    models = (relay or {}).get("models") or []
    print(f"→ 允许列表里的模型：{models}")
    if a.model not in models:
        print(f"⚠️  '{a.model}' **不在**允许列表里 —— 加上去（PROVIDER_RELAY_MODELS）或用列表里的名字重跑。")
    if relay is None or not relay.get("available"):
        print("⚠️  relay 不可用（key/base/models 没配齐）")

    # ② 打诊断端点
    st, d = call("/app/ext/providers/raw", key, {
        "provider_id": a.provider, "model": a.model, "prompt": a.prompt,
        "stream": (a.stream == "true"), "max_tokens": a.max_tokens, "max_frames": a.max_frames,
    })

    print("\n" + "=" * 70)
    print(f"HTTP {st}")
    print("=" * 70)
    if isinstance(d, dict) and d.get("hints") is not None:
        h = d.get("hints") or {}
        print(f"🔎 hints.path = {h.get('path')}"
              f"   reasoning_fields={h.get('reasoning_fields')}"
              f"   inline_thinking={h.get('inline_thinking')}")
        print(f"   status={d.get('status')} frames={d.get('frame_count')} "
              f"truncated={d.get('truncated')} host={d.get('host')}{d.get('path')}")
        print("\n--- request_body（我们真正发出去的）---")
        print(json.dumps(d.get("request_body"), ensure_ascii=False, indent=2)[:1500])
        print("\n--- frames（上游原话，未经解析）---")
        for i, f in enumerate(d.get("frames") or []):
            print(f"[{i}] {f[:400]}")
        print("\n--- 判定 ---")
        p = h.get("path")
        if p == "A":
            print("路径 A：上游给了独立字段、被网关解析丢掉了 → 该动**网关**（llm_gateway/llm_routes）")
        elif p == "B":
            print("路径 B：CoT 内联在正文里 → 该动**前端**（stripInlineThinkingText），不是网关")
        elif p == "C":
            print("路径 C：上游压根没给 → 先配 PROVIDER_RELAY_EFFORT_PARAM 再打一次，一行代码都不用改")
        else:
            print(f"路径 {p}：A 和 B 都有 → 两处都要处理")
    else:
        print(json.dumps(d, ensure_ascii=False, indent=2)[:3000])

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"http": st, "response": d}, f, ensure_ascii=False, indent=2)
    print(f"\n📄 已写入 {OUT}（把它发给我）")
    return 0 if st == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
