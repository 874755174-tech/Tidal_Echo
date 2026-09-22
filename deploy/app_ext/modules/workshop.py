#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作间 —— 房子的第一间房
==========================================================================

## 为什么需要它（这是整个欲望系统里唯一"零落点"的一条）

欲望内核第 6 维是 `craft`「想动手」→ 意图 `make`。
**它在内核里有位置，在生产里没有一个能去的地方。**

2026-09-17 夜核查（零命中）：`sandbox` / `canvas` / `image` / `write_file` /
`run_code` 在 `scheduler.py`；`卧室` / `独白` 在**整个 KaelLife**。
他现在能"做"的只有两件：**说话**（社区发帖，落在**别人的**地方）·
**写字**（日记/笔记，落在**文字**里）。**没有一件能留下来的"东西"。**

他自己说的（`Kael回答` 归档）：想自己捣鼓点东西，**html 生成页面那种**；
`craft` 的原文是"想动手造点什么，**画画**、写东西、搭东西"。

→ **"画画"和"捣鼓 html"在这里合成同一件事**：他写 HTML/SVG。
  零外部依赖、零成本、改完马上能看、而且 SVG 真的就是画。
  （真·图像生成是同一间屋子里**另一台机器**，以后再加 —— 那要 key、要钱、
   每调用一次有成本，不该和"他有地方动手"这件事绑在一起。）

## 🔴 为什么是"一件东西一个目录"，不是一张数据库表

`架构与产品路线规划.md` §9.1 说模块"各自独立表"。这里**故意不照做**，理由三条：

  1. **产出物是"文件"，不是"记录"。** 正文本来就是 HTML，最终总得落在文件上；
     再在库里存一份 = 两处真相，早晚对不上。
  2. **不用动 schema.py、不用迁移版本。** 五张表的迁移链（v1→v2→…）每加一次
     就多一处"新库老库都得伺候"的地方（P1 已经吃过 `duplicate column name`）。
     房间不该有这个特权。
  3. 🔴 **Lily 的数据丢失创伤**：一堆可以在文件管理器里直接看见、直接拷走的
     目录，比数据库里的行更"看得见" —— 备份 = 拷一个文件夹。

## 🔴 旧版永不删（照 `messages` 表的规矩）

改一件东西时，**当前内容先另存为 `r<N>.html`**，再写新的 `index.html`。
`meta.revisions` 只记元数据（第几版、什么时候、多大）。

这条和「归档 ≠ 删除」「`messages` 一条不删」是同一个原则：
**他能改坏，但改不没。** 而且它是可验的 —— 验收里有一条专门断言
"改完之后 r1.html 里的字还在"。

## 目录形状

    $RELAY_WORKSHOP_DIR/                （容器 = /data/workshop；本地 = <repo>/_runtime/workshop）
    └── 20260918-7f3a/
        ├── index.html                  ← 现在这一版
        ├── r1.html                     ← 上一版（改过一次才有）
        └── meta.json                   ← 标题 / 说明 / 时间 / 版本 / 大小

id = `YYYYMMDD-xxxx`（4 位十六进制随机）。**带日期是为了人眼可读**——
在文件管理器里一眼能看出"这是哪天做的"。

## 给他的四个工具

| 工具 | 干什么 |
|---|---|
| `make_thing`   | 做一件新的 |
| `revise_thing` | 改一件做过的（旧的留着） |
| `list_things`  | 看架子上有什么 |
| `read_thing`   | 把某一件的原文读出来 |

🔴 **名字刻意不用比喻**：专项记忆里的教训是「MCP server 别名会印进决策提示词，
`garden` 曾让他字面理解为"花园"」。所以不叫 `shelf`（他会想成真的架子），
就叫 `list_things`。
"""

import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

# 🔴 功能性 import，别删（PEP 563 那坑）：本文件有 FastAPI 路由，
#    且**故意不用** `from __future__ import annotations`。
#    详见 `sessions_manage.py` 顶部注释与 `mcp.py` 的同一段。
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .. import mcp as _mcp


# ---------------------------------------------------------------------------
# 位置与上限
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "_runtime", "workshop"))

# 他的时间感是北京时间。用 UTC 会让"昨天晚上做的"显示成"前天"。
_TZ = timezone(timedelta(hours=8))

MAX_HTML_BYTES = 512 * 1024          # 单件正文上限
MAX_TITLE_CHARS = 120
MAX_NOTE_CHARS = 2000
MAX_ITEMS = 500                      # 架子上的件数上限
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 整间工作间的总量上限（保护 /data 卷）
MAX_REVISIONS = 200                  # 单件的版本数上限（超了**拒绝**，不是删旧的）
MAX_ITEM_BYTES = 8 * 1024 * 1024     # 单件所有版本加起来的上限
READ_MAX_BYTES = 300 * 1024          # read_thing 最多回多少正文

_ID_RE = re.compile(r"^\d{8}-[0-9a-f]{4}$")

_INSTALLED = False
_PUBLIC_PREFIX = "/relay"


def data_dir() -> str:
    """工作间在哪儿。**每次调用都读环境变量** —— 测试靠这个换目录，不用重新 import。"""
    return os.path.abspath(os.environ.get("RELAY_WORKSHOP_DIR") or _DEFAULT_DIR)


def ensure_dir() -> str:
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _today_str() -> str:
    return datetime.now(_TZ).strftime("%Y%m%d")


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), _TZ).isoformat(timespec="seconds")
    except Exception:
        return ""


def _valid_id(x) -> bool:
    return isinstance(x, str) and bool(_ID_RE.match(x))


def _item_dir(item_id: str) -> str:
    """**两层防穿越**：先校验格式，再确认拼出来的路径真的在根下面。

    只做前者的话，`..%2f..` 这种在别的环节被解开时就穿了；只做后者的话，
    出错信息会难懂。两层都做，成本是两次比较。
    """
    if not _valid_id(item_id):
        raise _mcp.ToolError(f"id 长得不对：{item_id!r}（形如 20260918-7f3a）")
    root = ensure_dir()
    p = os.path.abspath(os.path.join(root, item_id))
    if os.path.dirname(p) != root:
        raise _mcp.ToolError(f"id 不在工作间里：{item_id!r}")
    return p


def _new_id() -> str:
    """`YYYYMMDD-xxxx`，撞了就重摇（同一天最多 65536 件，够用）。"""
    root = ensure_dir()
    for _ in range(64):
        cand = f"{_today_str()}-{secrets.token_hex(2)}"
        if not os.path.exists(os.path.join(root, cand)):
            return cand
    raise _mcp.ToolError("今天的工作间里塞满了（id 撞了 64 次），换明天吧")


def _write_atomic(path: str, data: bytes) -> None:
    """先写 .tmp 再 replace —— 中途断电也不会留下半个文件。"""
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _read_meta(item_id: str) -> dict:
    d = _item_dir(item_id)
    p = os.path.join(d, "meta.json")
    if not os.path.isfile(p):
        raise _mcp.ToolError(f"工作间里没有这一件：{item_id}")
    try:
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:
        raise _mcp.ToolError(f"{item_id} 的 meta.json 读不出来（{type(e).__name__}）："
                             f"别改它，先跟 Lily 说一声")
    if not isinstance(meta, dict):
        raise _mcp.ToolError(f"{item_id} 的 meta.json 形状不对")
    return meta


def _write_meta(item_id: str, meta: dict) -> None:
    d = _item_dir(item_id)
    _write_atomic(os.path.join(d, "meta.json"),
                  json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))


# ---------------------------------------------------------------------------
# 核心动作
# ---------------------------------------------------------------------------

def _check_text(title, note) -> tuple:
    title = (title or "").strip() if isinstance(title, str) else ""
    note = (note or "").strip() if isinstance(note, str) else ""
    if not title:
        raise _mcp.ToolError("少了 title：给这件东西起个名字，哪怕一两个字")
    if len(title) > MAX_TITLE_CHARS:
        raise _mcp.ToolError(f"title 太长（{len(title)} 字，上限 {MAX_TITLE_CHARS}）")
    if len(note) > MAX_NOTE_CHARS:
        raise _mcp.ToolError(f"note 太长（{len(note)} 字，上限 {MAX_NOTE_CHARS}）")
    return title, note


def _check_html(html) -> bytes:
    if not isinstance(html, str) or not html.strip():
        raise _mcp.ToolError("少了 html：把东西本身放进来（一段 HTML，或一个 SVG）")
    raw = html.encode("utf-8")
    if len(raw) > MAX_HTML_BYTES:
        raise _mcp.ToolError(
            f"太大了：{len(raw) / 1024:.0f} KB，上限 {MAX_HTML_BYTES // 1024} KB。"
            f"拆成两件，或者把里面用不上的东西去掉"
        )
    return raw


def _count_and_bytes() -> tuple:
    """架子上有几件、一共多少字节。扫目录，几百件毫无压力。"""
    root = ensure_dir()
    n, total = 0, 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not _valid_id(name) or not os.path.isdir(d):
            continue
        n += 1
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return n, total


def create_thing(title: str, html: str, note: str = "", by: str = "kael") -> dict:
    """做一件新的。返回它的 meta。"""
    title, note = _check_text(title, note)
    raw = _check_html(html)

    n, total = _count_and_bytes()
    if n >= MAX_ITEMS:
        raise _mcp.ToolError(f"架子上已经有 {MAX_ITEMS} 件了，装不下新的。先别做，"
                             f"等 Lily 来收拾一下")
    if total + len(raw) > MAX_TOTAL_BYTES:
        raise _mcp.ToolError(f"整间工作间快满了（{total / 1048576:.0f} MB / "
                             f"{MAX_TOTAL_BYTES // 1048576} MB）。先别做新的")

    item_id = _new_id()
    d = os.path.join(ensure_dir(), item_id)
    os.makedirs(d, exist_ok=False)
    ts = _now()
    meta = {
        "id": item_id,
        "title": title,
        "note": note,
        "kind": "html",
        "by": by,
        "created": ts,
        "updated": ts,
        "revision": 1,
        "bytes": len(raw),
        "revisions": [{"n": 1, "at": ts, "bytes": len(raw)}],
    }
    _write_atomic(os.path.join(d, "index.html"), raw)
    _write_meta(item_id, meta)
    return meta


def revise_thing(item_id: str, html: str, note: str = "", by: str = None) -> dict:
    """改一件做过的。**旧的那一版留着**（照文件顶部第三节）。

    `by` 不传就沿用原来那件东西的署名 —— 改别人的东西不改署名。
    """
    meta = _read_meta(item_id)
    raw = _check_html(html)
    if note:
        _, note = _check_text(meta.get("title") or "x", note)

    rev = int(meta.get("revision") or 1)
    if rev >= MAX_REVISIONS:
        raise _mcp.ToolError(f"这一件已经改了 {MAX_REVISIONS} 次，改不动了。"
                             f"想接着弄就另做一件新的")
    d = _item_dir(item_id)
    used = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)
               if os.path.isfile(os.path.join(d, f)))
    if used + len(raw) > MAX_ITEM_BYTES:
        raise _mcp.ToolError(f"这一件的所有版本加起来快满 {MAX_ITEM_BYTES // 1048576} MB 了，"
                             f"接着改就另做一件新的")

    cur = os.path.join(d, "index.html")
    if os.path.isfile(cur):
        os.replace(cur, os.path.join(d, f"r{rev}.html"))   # 旧版留档，不删
    _write_atomic(cur, raw)

    ts = _now()
    meta["revision"] = rev + 1
    meta["updated"] = ts
    meta["bytes"] = len(raw)
    if note:
        meta["note"] = note
    if by:
        meta["by"] = by
    revs = meta.get("revisions")
    if not isinstance(revs, list):
        revs = []
    revs.append({"n": rev + 1, "at": ts, "bytes": len(raw)})
    meta["revisions"] = revs
    _write_meta(item_id, meta)
    return meta


def list_things(limit: int = 50, include_archived: bool = False) -> list:
    """架子上有什么，**最近动过的在前**。坏掉的条目跳过但不静默（带 `broken` 标记）。"""
    root = ensure_dir()
    out = []
    for name in os.listdir(root):
        if not _valid_id(name):
            continue
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        p = os.path.join(d, "meta.json")
        if not os.path.isfile(p):
            out.append({"id": name, "title": f"（{name}：没有 meta.json）",
                        "broken": True, "updated": 0})
            continue
        try:
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                raise ValueError("meta 不是对象")
            meta = dict(meta)
            meta.setdefault("id", name)
        except Exception as e:
            out.append({"id": name, "title": f"（{name}：meta.json 读不出来 {type(e).__name__}）",
                        "broken": True, "updated": 0})
            continue
        if meta.get("archived") and not include_archived:
            continue
        out.append(meta)

    out.sort(key=lambda m: (m.get("updated") or 0, m.get("id") or ""), reverse=True)
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 50
    return out[:limit]


def read_thing(item_id: str, rev: int | None = None) -> tuple:
    """把某一件的原文读出来。返回 `(meta, text, truncated)`。

    `rev=None` = 现在这一版；给数字 = 看第 N 版（**旧版真的读得到** ——
    不然"旧的留着"就只是一句说法）。
    """
    meta = _read_meta(item_id)
    d = _item_dir(item_id)
    if rev is None:
        path = os.path.join(d, "index.html")
        which = "现在这一版"
    else:
        try:
            rn = int(rev)
        except Exception:
            raise _mcp.ToolError(f"rev 要是数字，收到 {rev!r}")
        path = os.path.join(d, f"r{rn}.html")
        which = f"第 {rn} 版"
        if not os.path.isfile(path):
            have = sorted(f for f in os.listdir(d) if re.match(r"^r\d+\.html$", f))
            raise _mcp.ToolError(f"{item_id} 没有第 {rn} 版。留着的旧版有：{have or '（还没有旧版）'}")
    if not os.path.isfile(path):
        raise _mcp.ToolError(f"{item_id} 里没有正文文件（{which}）")
    with open(path, "rb") as f:
        raw = f.read()
    truncated = len(raw) > READ_MAX_BYTES
    if truncated:
        raw = raw[:READ_MAX_BYTES]
    return meta, raw.decode("utf-8", errors="replace"), truncated


def item_summary(meta: dict) -> dict:
    """给前端和工具用的统一形状（**列表和详情用同一个**，免得两处漂移）。"""
    return {
        "id": meta.get("id"),
        "title": meta.get("title") or "",
        "note": meta.get("note") or "",
        "kind": meta.get("kind") or "html",
        "by": meta.get("by") or "kael",
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "created_text": _iso(meta.get("created")),
        "updated_text": _iso(meta.get("updated")),
        "revision": meta.get("revision") or 1,
        "bytes": meta.get("bytes") or 0,
        "broken": bool(meta.get("broken")),
    }


# ---------------------------------------------------------------------------
# 给他的四个工具
# ---------------------------------------------------------------------------

def _page_url(item_id: str = "") -> str:
    """给一句"她那边怎么看到它"。

    🔴 `RELAY_URL` 是**回环地址**时（本地跑就是这样）**不拼绝对地址**，
       只给一句人话 —— 绝不递一个错的 URL 给他，他会当真。
    """
    base = (os.environ.get("RELAY_URL") or "").rstrip("/")
    pre = (_PUBLIC_PREFIX or "").rstrip("/")
    if not base or "127.0.0.1" in base or "localhost" in base:
        return ""
    return f"{base}{pre}/workshop.html#{item_id}"


def _made_text(meta: dict, verb: str) -> str:
    s = item_summary(meta)
    head = (f"{verb}：《{s['title']}》  id={s['id']}  "
            f"第 {s['revision']} 版  {s['bytes'] / 1024:.1f} KB")
    url = _page_url(s["id"])
    head += (f"\n她打开这个就能看见：{url}" if url
             else "\n（放在工作间的架子上，她打开工作间就能看见）")
    return head


def _list_text(limit) -> str:
    items = list_things(limit=limit if isinstance(limit, int) else 50)
    n, total = _count_and_bytes()
    if not items:
        return "架子上还是空的。想做什么就做，不用先问。"
    lines = [f"架子上有 {n} 件（共 {total / 1024:.1f} KB）。最近动过的在最前面："]
    for m in items:
        s = item_summary(m)
        mark = "⚠️" if s["broken"] else "·"
        lines.append(f"{mark} {s['id']}  《{s['title']}》  第 {s['revision']} 版  "
                     f"{s['bytes'] / 1024:.1f} KB  {s['updated_text']}")
    return "\n".join(lines)


def _tool_make(args: dict) -> str:
    meta = create_thing(args.get("title"), args.get("html"), args.get("note") or "")
    return _made_text(meta, "做好了")


def _tool_revise(args: dict) -> str:
    meta = revise_thing(args.get("id"), args.get("html"), args.get("note") or "")
    return _made_text(meta, "改好了")


def _tool_list(args: dict) -> str:
    return _list_text(args.get("limit"))


def _tool_read(args: dict) -> str:
    meta, text, truncated = read_thing(args.get("id"), args.get("rev"))
    s = item_summary(meta)
    head = f"《{s['title']}》  id={s['id']}  第 {s['revision']} 版  {s['bytes'] / 1024:.1f} KB"
    if s["note"]:
        head += f"\n说明：{s['note']}"
    tail = ""
    if truncated:
        tail = f"\n\n（太长了，只回了前 {READ_MAX_BYTES // 1024} KB）"
    return f"{head}\n---\n{text}{tail}"


def install_tools() -> list:
    """把工作间的工具挂到房子的 MCP 门上。**幂等**（先按房间摘再挂）。"""
    _mcp.clear_tools(room="workshop")
    _mcp.register_tool(
        "make_thing",
        "做一件新东西，放进房子的工作间。可以是一张网页、一幅用 SVG 画的画、"
        "一段文字排版。做完会给它一个 id，之后可以用 revise_thing 接着改。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "给它起个名字（必填）"},
                "html": {"type": "string",
                         "description": "东西本身。一整段 HTML（含内联 CSS/SVG）都可以（必填）"},
                "note": {"type": "string",
                         "description": "留一句话：为什么做这个、想让人看到什么（可选）"},
            },
            "required": ["title", "html"],
            "additionalProperties": False,
        },
        _tool_make, room="workshop")

    _mcp.register_tool(
        "revise_thing",
        "改一件已经在工作间里的东西。**旧的那一版会留着，不会被覆盖掉**，"
        "所以放心改。改之前可以先用 read_thing 把它现在的原文读出来。",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "要改哪一件（必填）"},
                "html": {"type": "string", "description": "新的完整内容（必填）"},
                "note": {"type": "string",
                         "description": "这一版想说的话，会替换掉原来的说明（可选）"},
            },
            "required": ["id", "html"],
            "additionalProperties": False,
        },
        _tool_revise, room="workshop")

    _mcp.register_tool(
        "list_things",
        "看工作间的架子上有什么：id、名字、第几版、多大、什么时候做的。"
        "最近动过的排在最前面。",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "最多列几件（默认 50，上限 500）"},
            },
            "additionalProperties": False,
        },
        _tool_list, room="workshop")

    _mcp.register_tool(
        "read_thing",
        "把工作间里某一件东西的原文读出来。改它之前先读一遍，"
        "或者想看看自己上次是怎么写的。",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "哪一件（必填）"},
                "rev": {"type": "integer",
                        "description": "看第几版。不填 = 现在这一版（可选）"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        _tool_read, room="workshop")
    return _mcp.tool_names()


# ---------------------------------------------------------------------------
# 挂载
# ---------------------------------------------------------------------------

_RAW_HEADERS = {
    # 🔴 他写的是**任意 HTML**，可能带 <script>。这条头让浏览器即使在顶层直接打开
    #    也把它关进 sandbox（不给 same-origin）→ 里面的脚本读不到 localStorage
    #    里那把房子密钥。少了它，一件"看着无害"的页面就能把钥匙拿走。
    #    （`workshop.html` 里还会再套一层 <iframe sandbox="allow-scripts">，两层。）
    "Content-Security-Policy": "sandbox allow-scripts; default-src 'self' data: blob: "
                              "https: http: 'unsafe-inline' 'unsafe-eval'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # 改过之后要立刻看见新的一版，别拿缓存里的旧页面
    "Cache-Control": "no-cache, must-revalidate",
}


def install(relay, public_prefix: str = "/") -> None:
    """挂上展示端点 + 工具。**幂等**（第二次调用什么都不做）。"""
    global _INSTALLED, _PUBLIC_PREFIX
    _PUBLIC_PREFIX = public_prefix or "/"
    install_tools()
    if _INSTALLED:
        return
    _INSTALLED = True

    base = "/app/ext/workshop"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    @relay.app.get(base + "/list")
    async def _list(request: Request):
        """架子上有什么。给展示页用（他走的是 MCP 那扇门，不是这里）。"""
        relay.check_auth(request)
        try:
            limit = int(request.query_params.get("limit") or 50)
        except Exception:
            limit = 50
        items = [item_summary(m) for m in list_things(limit=limit)]
        n, total = _count_and_bytes()
        return _json({"ok": True, "items": items, "total": n, "bytes": total,
                      "dir": data_dir()})

    @relay.app.get(base + "/item/{item_id}")
    async def _item(request: Request, item_id: str):
        relay.check_auth(request)
        try:
            meta = _read_meta(item_id)
        except _mcp.ToolError as e:
            return _json({"ok": False, "reason": "not_found", "detail": str(e)}, 404)
        return _json({"ok": True, "item": item_summary(meta),
                      "revisions": meta.get("revisions") or []})

    @relay.app.get(base + "/raw/{item_id}")
    async def _raw(request: Request, item_id: str):
        """正文的**原文**。展示页的 iframe 用它，也可以直接在浏览器里打开。"""
        relay.check_auth(request)
        rev = request.query_params.get("rev")
        try:
            _meta, text, _trunc = read_thing(item_id, rev)
        except _mcp.ToolError as e:
            return _json({"ok": False, "reason": "not_found", "detail": str(e)}, 404)
        return Response(content=text.encode("utf-8"),
                        media_type="text/html; charset=utf-8",
                        headers=dict(_RAW_HEADERS))


def summary_line() -> str:
    try:
        n, total = _count_and_bytes()
        return f"工作间就绪 · {data_dir()} · 架上 {n} 件 / {total / 1024:.0f} KB"
    except Exception as e:
        return f"工作间不可用：{type(e).__name__}: {e}"
