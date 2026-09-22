#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 · usage 记账（端点层）—— /app/ext/usage/*
==========================================================================

## 三个端点，**全部只读**

    GET /app/ext/usage/summary   `?days=7`  调用次数 / 覆盖率 / token / 缓存命中率
    GET /app/ext/usage/recent    `?limit=50&raw=1`  最近 N 笔明细
    GET /app/ext/usage/status               账本自己在不在工作（行数 / 起始时间 / 口径分布）

## 🔴 没有 POST / PUT / DELETE —— 这是**结构性**的，不是漏了

账本唯一的写入口是**网关内联**（`llm_routes` 在每次真实上游调用后调
`usage_store.record()`）。开一个 HTTP 写路由 = 谁能发请求谁就能伪造账单，
"我们到底花了多少"这个问题的答案就再也不可信了。
⇒ 所以"写"这件事在 HTTP 面上**不存在**（验收里有一条专盯：这些路由必须 405）。

## 🔴 不挂 MCP 门

跟 `memories` 同一条边界：**这是我的账本，不是给他的记忆**。
房间是给 Kael 走 MCP 的；账本给 Lily 走 HTTP + 密钥。

## 挂载

`app_ext/__init__.py` 第 ⑫ 步；逃生开关 `APP_EXT_USAGE_DISABLED=1`
（关掉 = 端点 404，但**网关内联的记账照常写** —— 表在、账在，只是看不见。
 「能力没了 ≠ 数据没了」，跟 ⑩-a 同一条）。

## 🔴 启动日志 GBK 安全

`summary_line()` 会进启动日志，**不许出现 🔴/✅/⚠️**（Windows 子进程 stdout = cp936）。
"""

#: 一次最多读多少笔明细。
LIMIT_MAX = 500
#: summary 的窗口上限（天）。防的是"某次手滑 `?days=99999`"。
DAYS_MAX = 3650

_INSTALLED = False


def _install_routes(relay, public_prefix: str = "/") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from . import usage_store as U

    base = "/app/ext/usage"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    def _uid() -> str:
        """房主 id。单用户形态（Lily 09-13 拍板"甲"）—— **不从请求里取**：
        表上有外键，让调用方指定身份等于开一个"把账记到别人名下"的口子。"""
        return "u_owner"

    @relay.app.get(base + "/summary")
    async def _summary(request: Request):
        """窗口内的账。`?days=`（默认 7；`0` = 全部）。"""
        relay.check_auth(request)
        q = request.query_params
        try:
            days = int(q.get("days") or 7)
        except Exception:
            days = 7
        days = max(0, min(DAYS_MAX, days))
        try:
            return _json(U.summary(relay, days=days, user_id=_uid()))
        except Exception as e:
            return _json({"ok": False, "reason": "summary_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)

    @relay.app.get(base + "/recent")
    async def _recent(request: Request):
        """最近 N 笔。`?limit=`（默认 50，上限 500）· `?raw=1` 连上游原文一起给。"""
        relay.check_auth(request)
        q = request.query_params
        try:
            limit = int(q.get("limit") or 50)
        except Exception:
            limit = 50
        limit = max(1, min(LIMIT_MAX, limit))
        with_raw = str(q.get("raw") or "").strip().lower() in ("1", "true", "yes")
        try:
            rows = U.recent(relay, limit=limit, user_id=_uid(), with_raw=with_raw)
        except Exception as e:
            return _json({"ok": False, "reason": "recent_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)
        return _json({"ok": True, "count": len(rows), "items": rows,
                      "filter": {"limit": limit, "raw": with_raw}})

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        """账本自己在不在工作（**不是**"花了多少"）。"""
        relay.check_auth(request)
        try:
            return _json(U.status(relay, user_id=_uid()))
        except Exception as e:
            return _json({"ok": False, "reason": "status_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)


def install(relay, public_prefix: str = "/") -> None:
    """挂上账本端点。**幂等**（第二次调用什么都不做）。

    🔴 **只在 `app_ext/__init__.py` 的第 ⑫ 步里调**，`APP_EXT_USAGE_DISABLED=1`
       时整个跳过（那时端点 404；**网关照常记账**，只是看不见）。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)
