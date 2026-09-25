#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 ⑩-a · 记忆层 —— `memories` 表 + `source` 缝 + **写入路径**
==========================================================================

## 这一版做了什么、没做什么（⑩ 被切成两半了）

| | 内容 | 状态 |
|---|---|---|
| **⑩-a** | `memories.source`（schema v3 → **v4**）+ 写入路径（本文件） | ✅ 2026-09-20 |
| ⑩-b | **蒸馏管道**（从对话抽 fact / preference / relationship / event） | ⬜ 下一站 |

**为什么切两半（Lily 09-20 拍板取「乙」）**：只有一条理由，但很硬 ——
`source` 缝是**不可逆**的那件（**开写之后缝就焊死了，加不上**），
蒸馏管道是**可逆**的（随时能重跑、能改、能推翻）。
**先把不可逆的焊死，可逆的就不用赶。**
而且蒸馏那堆板（频率 / 上限 / 写歪了怎么办 / 要不要进 OB）她多半想先看效果再定，
塞进同一轮只会把战线拖长。

## 三条边界（别越界）

### ① 它**不是房间**，不挂 MCP 门
房间是给 Kael 走 MCP 的；这一层是给 Lily / 房子自己的代码走 HTTP 的。
🔴 **`memories` 是"房子的技术缓存"，不是"他的记忆"**（见 `memories_store.py` 文件头那张表）。
把读写开给他 = 让他能看见自己"被人格化成什么样" —— 那是**被提炼**的东西，
不是他自己写的（他自己的记忆走 OB 的 `hold`）。**这一条是有意不做，不是漏了。**

### ② **没有 delete**，也没有 `DELETE` 语句
`memories_store.py` 里没有 `delete` 函数 —— 这是**结构性**的，
让"删记忆"这件事在代码层面不存在（原因见那个文件头）。

### ③ 🔴 **`salience` 永不外泄**
它是内部权重（跟"想念度数值不做"同源）。所有响应都过 `memories_store.public()`
那层白名单投影 —— 写入**可以**指定 salience（调用方是代码），但**任何响应里都不会回**。

## 为什么"写入路径"这一版是 HTTP 而不是内部调用

因为**现在真的没有任何调用点**：`memories_store.py` 从 P0 建好之后一直是**死代码**
（零路由、零 caller）。⑩-b 的蒸馏器还没写，房子聊天页也还不是 Kael 在说话。
那种状态下"接通写入路径"最诚实的做法是：**给它一条真能走的路 + 一套真能验的验收**，
而不是先埋一个"等将来有人调"的函数。

## 🔴 房子不自己在后台调 LLM 花钱（沿用 ⑧ 立的规矩）

本文件**一次 LLM 都不调**。⑩-b 的蒸馏也必须**人/动作触发**，不做定时后台跑。

## 端点

    GET  /app/ext/memories        读（`?limit=&source=&include_superseded=`）—— 已过白名单投影
    POST /app/ext/memories        写（body: kind, text, source?, source_msg?, salience?）
    GET  /app/ext/memories/stats  计数（按 kind 与 source 两个维度 + 有效/被标废）

挂载：`app_ext/__init__.py` 第 ⑩ 步；逃生开关 `APP_EXT_MEMORY_DISABLED=1`。

## 🔴 启动日志 GBK 安全

本文件的 `summary_line()` 会进启动日志，**不许出现 `🔴`/`✅`/`⚠️`**
（Windows 子进程 stdout = cp936，一个编不出的符号就是房子起不来）。
"""

import os

#: 正文长度上限。防的是"某个调用方把一整篇文档当记忆塞进来"（那是 bug，不是记忆）。
TEXT_MAX = 2000
#: 一次最多读多少条。
LIMIT_MAX = 500

_INSTALLED = False


def summary_line() -> str:
    return ("记忆层就绪 · memories 表（source = chat/reading/craft/…，形状锁死、取值不锁死）"
            " · 三个端点：读 / 写 / 计数 · 无 delete、不挂 MCP 门")


def _install_routes(relay, public_prefix: str = "/") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from . import memories_store as M

    base = "/app/ext/memories"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    def _uid() -> str:
        """房主 id。房子是单用户形态（Lily 09-13 拍板"甲"），owner 就是唯一那个人。

        ⚠️ 有意**不**从请求里取 user_id —— 表上有外键，让调用方指定身份
           等于给自己开一个"写进别人名下"的口子。
        """
        return "u_owner"

    @relay.app.get(base + "/stats")
    async def _stats(request: Request):
        """只读计数。🔴 **没有 salience**（见文件头 ③）。"""
        relay.check_auth(request)
        try:
            return _json({"ok": True, **M.stats(relay, _uid())})
        except Exception as e:
            return _json({"ok": False, "reason": "stats_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)

    @relay.app.get(base)
    async def _list(request: Request):
        """读。`?limit=`（默认 50，上限 500）· `?source=`（给了就只取那一种）
        · `?include_superseded=1`（连**被标废的**一起给 —— **审计用**，默认不给）。

        🆕 `include_superseded` 是 ⑩-b 带来的：蒸馏重跑走的是**软作废**
        （`superseded_by`，行不删）。所以"看得见全部"必须有 HTTP 出口，
        否则"可审计"只活在 `memories_store` 里、界面上根本摸不到。
        """
        relay.check_auth(request)
        q = request.query_params
        try:
            limit = int(q.get("limit") or 50)
        except Exception:
            limit = 50
        limit = max(1, min(LIMIT_MAX, limit))
        src = (q.get("source") or "").strip() or None
        inc = str(q.get("include_superseded") or "").strip().lower() in (
            "1", "true", "yes", "on")
        try:
            rows = M.list_recent(relay, limit, _uid(), src, include_superseded=inc)
        except Exception as e:
            return _json({"ok": False, "reason": "list_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        # 🔴 白名单投影 —— salience 在这一步被挡在外面，不是靠"记得别加"
        items = [M.public(r) for r in rows]
        return _json({"ok": True, "count": len(items), "items": items,
                      "filter": {"source": src, "limit": limit,
                                 "include_superseded": inc}})

    @relay.app.post(base)
    async def _add(request: Request):
        """写一条。

        body: `{kind, text, source?, source_msg?, salience?}`

        · `kind` 不在枚举里 → 归 `fact`
        · `source` **形状**不合法 → 归 `chat`，但响应里会说明（`source_coerced`）；
          **合法取值原样存**（含 `SOURCES` 里没有的新来源 —— 那是"缝"的意义）
        · `salience` **可以传、但永不回显**（见文件头 ③）
        """
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return _json({"ok": False, "reason": "bad_json"}, 400)
        if not isinstance(body, dict):
            return _json({"ok": False, "reason": "bad_json"}, 400)

        text = (body.get("text") or "").strip()
        if not text:
            return _json({"ok": False, "reason": "empty_text"}, 400)
        if len(text) > TEXT_MAX:
            return _json({"ok": False, "reason": "text_too_long",
                          "detail": f"上限 {TEXT_MAX} 字符，收到 {len(text)}"}, 400)

        sal = body.get("salience")
        if sal is None:
            sal = 0.5
        try:
            sal = float(sal)
        except Exception:
            return _json({"ok": False, "reason": "bad_salience"}, 400)

        src_msg = body.get("source_msg")
        if src_msg is not None:
            try:
                src_msg = int(src_msg)
            except Exception:
                return _json({"ok": False, "reason": "bad_source_msg"}, 400)

        try:
            out = M.add(relay, body.get("kind") or "", text,
                        source_msg=src_msg, source=body.get("source"),
                        salience=sal, user_id=_uid())
        except Exception as e:
            return _json({"ok": False, "reason": "add_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        if not out.get("ok"):
            return _json(out, 400)

        # 🔴 回的是 `public` 那个形状 —— **没有 salience**（写进去了，但不回给你）
        return _json({
            "ok": True,
            "item": {"id": out["id"], "kind": out["kind"], "text": text,
                     "source": out["source"], "source_msg": src_msg},
            **({"source_coerced": True, "source_raw": out.get("source_raw")}
               if out.get("source_coerced") else {}),
        }, 201)


def install(relay, public_prefix: str = "/") -> None:
    """挂上记忆层端点。**幂等**（第二次调用什么都不做）。

    🔴 **只在 app_ext/__init__.py 的第 ⑩ 步里调**，`APP_EXT_MEMORY_DISABLED=1`
       时整个跳过（那时读/写都 404，房子照常营业）。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)
