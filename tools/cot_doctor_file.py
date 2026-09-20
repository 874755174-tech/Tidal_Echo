# -*- coding: utf-8 -*-
"""🔑 CoT 体检 · 双击版（不需要命令行、不需要按键交互）

为什么有这一版（2026-09-16）：
  PowerShell 里 `getpass` **对"粘贴"支持极差** —— Windows 下它走 msvcrt 逐字符读，
  粘贴整串经常只读到 Ctrl+V 的控制码 `\\x16` 就返回（长度 1）。
  用户已经在这条路上白试了 4 次。→ **彻底不用按键输入。**

本脚本改用「记事本 + 文件」：
  1. 双击 `tools\\CoT体检.bat`（或在这里跑 `python tools/cot_doctor_file.py`）
  2. 它用**记事本**打开一个临时文件，提示你把密钥粘进去、保存、关闭
  3. 你保存关闭后，脚本自动读文件、清洗、并跑完整诊断

  记事本不会吃掉粘贴内容，也不会夹带题外字符（换行会被清洗掉）。

🔴 它顺手帮你做掉的判断（你不用记任何东西）：
  · 模型名 —— 自动从房子的允许列表里取，你不用抄
  · 密钥有没有粘全 —— 打印长度/首末字符/sha256，并跟自己比一次
  · key 用错了没 —— 401 时直接告诉你去查哪个变量
  · 房子在不在 —— 5xx 时告诉你在重启，不是你错
"""
import hashlib
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from cot_probe import BASE, sanitize_secret      # 复用清洗逻辑

KEYFILE = os.path.join(ROOT, "_k.txt")
TEMPLATE = (
    "# ================================================================\r\n"
    "#  把 RELAY_SECRET 粘到**下面这一行**（整行替换掉 PASTE_HERE）\r\n"
    "#  然后 Ctrl+S 保存，再关掉记事本\r\n"
    "#\r\n"
    "#  · 只粘值本身：不要加引号、不要加分号、不要加空格\r\n"
    "#  · 如果它本来就**中间带一个空格**，那个空格是内容，要一起粘上\r\n"
    "#  · 以 # 开头的行都会被忽略，上面这些提示留着没关系\r\n"
    "# ================================================================\r\n"
    "PASTE_HERE\r\n"
)


def _read_key_line() -> str:
    """读文件里第一行『非注释、非空、非占位符』的内容 = 密钥。"""
    try:
        with open(KEYFILE, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return ""
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or s in ("PASTE_HERE", "PASTE_HERE\r"):
            continue
        return ln
    return ""


def ask_via_notepad() -> str:
    """让用户在**记事本**里粘密钥（唯一在 Windows 上稳定可靠的粘贴方式）。"""
    with open(KEYFILE, "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    print("\n" + "=" * 68)
    print(" 📝 记事本马上就要弹出来了")
    print("=" * 68)
    print(f"    文件：{KEYFILE}")
    print("    做法：把 RELAY_SECRET 粘到 PASTE_HERE 那一行 → Ctrl+S → 关掉记事本")
    print("\n    其余全自动，你不需要打任何命令。\n")
    try:
        subprocess.Popen(["notepad.exe", KEYFILE])
    except Exception as e:
        print(f"❌ 打不开记事本：{e}")
        print(f"   请手动编辑 {KEYFILE}，把密钥写进去（一行），然后重跑本脚本。")
        return ""

    print("⏳ 等你保存并关闭记事本……（每 1.5 秒看一次）")
    # ── 等记事本放手 ──────────────────────────────────────────────────────
    # 记事本在编辑期间会**独占**文件，所以"能写进这个文件"就说明它已经关了。
    # 这一步比轮询窗口更可靠，也顺便覆盖了"根本没打开记事本"的情况（第一次就成功）。
    closed = False
    for _ in range(400):                       # 最多等 10 分钟
        time.sleep(1.5)
        try:
            with open(KEYFILE, "r+b"):
                pass
            closed = True
            break
        except Exception:
            continue
    if not closed:
        print("⚠️  等太久了（10 分钟）。如果你已经关掉记事本，请直接重跑一次。")
        return ""
    time.sleep(0.8)                            # 给文件系统一点落盘时间

    # ── 看门狗：关了但没粘 ────────────────────────────────────────────────
    # 🔴 这一条是必须的：手快会出现"打开了但什么都没粘就关了"，
    #    旧写法会把 PASTE_HERE 之后的空行当成结果 → 报"没读到密钥"，
    #    用户看着像"我又做错了"。这里明确告诉她"没粘就关了"，再给一次机会。
    for attempt in (1, 2, 3):
        keyline = _read_key_line()
        if keyline.strip():
            return keyline
        print(f"\n⚠️  文件里还没有内容 —— 你可能是**还没粘就关掉了记事本**（第 {attempt}/3 次）。")
        print("    我再开一次，这次粘完记得 **Ctrl+S 保存** 再关。")
        time.sleep(1.0)
        try:
            subprocess.Popen(["notepad.exe", KEYFILE])
        except Exception as e:
            print(f"❌ 打不开记事本：{e}；请手动编辑 {KEYFILE} 后重跑。")
            return ""
        # 用文件号判断"这次的确写了东西"：轮询内容出现即可（不必等关闭）
        got = ""
        for _ in range(200):
            time.sleep(1.5)
            got = _read_key_line()
            if got.strip():
                break
        if got.strip():
            return got
    return ""


def main() -> int:
    print("=" * 68)
    print(" CoT 体检 —— 看看中转站把『思考过程』藏在哪")
    print("=" * 68)
    print(" 全程**只读**：不改你家 Kael 的任何数据、不动代码、不写线上。")
    print(" 唯一的成本：最后会向中转站要一句很短的回答（32 token 以内）。\n")

    keyline = ""
    if os.environ.get("COT_REUSE") == "1":
        keyline = _read_key_line()
        if keyline.strip():
            print(f"📄 复用已有 {KEYFILE}")

    if not keyline.strip():
        keyline = ask_via_notepad()

    key = sanitize_secret(keyline)
    if not key:
        print(f"\n❌ 仍然没读到密钥。")
        print(f"   最省事的办法：用记事本打开 {KEYFILE}，把密钥写在第 8 行（一行），保存后重跑。")
        return 2

    print("\n" + "-" * 68)
    print(f"🔑 密钥：长度 {len(key)} | 首 {key[0]!r} 末 {key[-1]!r} "
          f"| 内部空格 {key.count(' ')} | sha256[:12] {hashlib.sha256(key.encode()).hexdigest()[:12]}")
    print("   （只打印指纹，不打印密钥本身 —— 你也不用担心它会外泄）")
    if len(key) < 8:
        print("⚠️  太短了，八成没粘全。请重跑并确认粘的是完整那一串。")
        return 2

    # ① 鉴权（不花上游的钱）
    print("\n[1/3] 问房子：这把钥匙能用吗？")
    req = urllib.request.Request(BASE + "/app/ext/providers",
                                 headers={"Authorization": "Bearer " + key})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read()[:300].decode("utf-8", "replace")
        print(f"      ❌ HTTP {code}：{body}")
        if code == 401:
            print("      → 房子不认这把钥匙。按顺序查这三处（别慌，基本是第 ① 或 ③）：")
            print("        ① 你粘的是 `RELAY_SECRET` 吗？（不是 PROVIDER_RELAY_KEY、不是 PASSWORD）")
            print("        ② 有没有把它中间那个空格删掉？（如果有空格，那是内容的一部分）")
            print("        ③ 上次改完环境变量后，**重新部署过吗**？")
            print("           环境变量是开机时注入容器的，不重启还是用旧值 —— 这条最容易踩。")
        elif code == 400:
            print("      → 400 说明请求头里混进了非法字符（换行/零宽）。")
            print("        本脚本已自动清洗过；若仍 400，请把 `_k.txt` 删掉重来一次。")
        elif code in (502, 503, 504):
            print("      → 房子正在重启（Zeabur 部署空窗期）。等 30 秒重跑，**不是你做错了**。")
        else:
            print("      → 把上面这行原话发我。")
        return 1
    except Exception as e:
        print(f"      ❌ 连不上 {BASE}：{type(e).__name__}: {e}")
        return 1
    print("      ✅ 通过 —— 钥匙是对的，房子活着")

    # ② 自己挑模型（用户不用抄）
    print("\n[2/3] 挑一个模型来问（自动挑，你不需要记名字）")
    relay = next((p for p in (d.get("providers") or []) if p.get("id") == "relay"), None)
    models = (relay or {}).get("models") or []
    sel = (d.get("selected") or {}).get("model_id")
    print(f"      relay 里配的模型：{models}")
    model = sel or (models[0] if models else "")
    if not model:
        print("      ❌ relay 没有配模型清单（Zeabur 里的 `PROVIDER_RELAY_MODELS` 是空的）")
        return 2
    print(f"      → 用这个：{model}" + ("（设置页当前选的）" if sel else "（列表第一个）"))

    # ③ 打诊断端点，抄上游原话
    print("\n[3/3] 问中转站一句话，把它原话抄回来……")
    payload = {"provider_id": "relay", "model": model, "prompt": "在吗",
               "stream": True, "max_tokens": 32, "max_frames": 80}
    req = urllib.request.Request(
        BASE + "/app/ext/providers/raw", method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=90)
        st, res = r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        st = e.code
        res = e.read()[:1500].decode("utf-8", "replace")
    except Exception as e:
        st, res = 0, f"{type(e).__name__}: {e}"

    print("\n" + "=" * 68)
    print(f" 结果：HTTP {st}")
    print("=" * 68)
    if st != 200:
        print(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)[:2000])
        return 1

    h = res.get("hints") or {}
    p = h.get("path")
    print(f"🔎 判定 path = {p}")
    print(f"   reasoning_fields = {h.get('reasoning_fields')}")
    print(f"   inline_thinking  = {h.get('inline_thinking')}")
    print(f"   frames={res.get('frame_count')} truncated={res.get('truncated')} "
          f"上游={res.get('host')}{res.get('path')}")
    print("\n--- request_body（我们真正发出去的）---")
    print(json.dumps(res.get("request_body"), ensure_ascii=False, indent=2)[:1200])
    print("\n--- frames（上游原话，未经解析）---")
    for i, f in enumerate(res.get("frames") or []):
        print(f"[{i}] {str(f)[:300]}")

    print("\n" + "=" * 68)
    print(" 这意味着什么（你只要把这几行发我，我来动）")
    print("=" * 68)
    print({
        "A": "路径 A：中转站**给了**独立字段，但被网关解析时丢掉了\n"
             "      → 要动的是房子里面的网关，改动小、能离线验收",
        "B": "路径 B：思考链是**内联在正文里**的\n"
             "      → 要动的是网页的显示逻辑（把 <thinking> 抽出来单独显示）",
        "C": "路径 C：中转站**压根没给**思考链\n"
             "      → 一行代码都不用改，先在 Zeabur 配一个参数再重跑",
        "A+B": "A 和 B 都有 → 两处都要处理",
    }.get(p, f"路径 {p}：把结果发我，我看细节"))

    outp = os.path.join(HERE, "cot_probe_result.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump({"http": st, "response": res}, f, ensure_ascii=False, indent=2)
    print(f"\n📄 结果已存到：{outp}")
    print(f"   （把这个文件、或者上面这一屏，原样发给我就行）")
    print(f"\n🗑️  顺手清理：可以把 {KEYFILE} 删掉（里面是明文密钥）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n（中断了，什么都没改。）")
        sys.exit(130)
