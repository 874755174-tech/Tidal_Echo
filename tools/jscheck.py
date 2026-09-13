#!/usr/bin/env python3
"""从 web/index.html 里抽出 <script> 内联块，用 node --check 验证语法。

用途：改完 index.html 的 JS 后，确认没有语法错误（尤其是模板字符串、引号配对）。
用法：.venv\\Scripts\\python.exe tools\\jscheck.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML = HERE.parent / "web" / "index.html"

NODE = Path(r"C:\Users\86187\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")


def main() -> int:
    html = HTML.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        print("没找到内联 script 块")
        return 1

    print(f"{HTML.name}: 找到 {len(blocks)} 个内联块")
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

    print("结果：" + ("全部通过" if not bad else f"{bad} 个块有语法错误"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
