#!/usr/bin/env python3
"""抽出 web/ 下每个 html 的内联 <script> 块，用 node --check 验证语法。

用途：改完页面 JS 后，确认没有语法错误（尤其是模板字符串、引号配对）。
      新加页面（workshop.html 这种）不用改这里 —— 扫的是整个 web/*.html。
用法：.venv\\Scripts\\python.exe tools\\jscheck.py
"""
from __future__ import annotations

import re
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"

NODE = Path(r"C:\Users\86187\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")


def check_file(html_path: Path) -> tuple[int, int]:
    """返回 (块数, 坏块数)。"""
    html = html_path.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        print(f"  （{html_path.name}：没有内联 script 块）")
        return 0, 0

    print(f"{html_path.name}: 找到 {len(blocks)} 个内联块")
    bad = 0
    for i, body in enumerate(blocks, 1):
        # 顶层 await / import 会让 --check 误报，用模块模式兜一下
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
            tmp = fh.name
        r = subprocess.run([str(NODE), "--check", tmp],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode == 0:
            print(f"  [OK]   块 {i}  ({body.count(chr(10))} 行)")
        else:
            bad += 1
            print(f"  [FAIL] 块 {i}  ({body.count(chr(10))} 行)")
            print((r.stderr or r.stdout or "").strip()[:1200])
        Path(tmp).unlink(missing_ok=True)
    return len(blocks), bad


def main() -> int:
    pages = sorted(p for p in WEB.glob("*.html") if p.is_file())
    if not pages:
        print(f"没找到页面：{WEB}/*.html")
        return 1

    total_blocks = total_bad = 0
    for p in pages:
        n, bad = check_file(p)
        total_blocks += n
        total_bad += bad

    print(f"共 {len(pages)} 个页面 / {total_blocks} 个内联块 —— "
          + ("全部通过" if not total_bad else f"{total_bad} 个块有语法错误"))
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
