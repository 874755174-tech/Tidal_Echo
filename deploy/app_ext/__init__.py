#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
房子扩展包 —— P0 地基（四张表 + 身份层）+ P1 模型网关
==========================================================================

## 这个包包什么

    P0 · 地基
    schema.py          4 张表的 DDL + 幂等迁移 + 版本号（不碰 messages）
    identity.py        users / settings 的读写 + /app/ext/* 端点
    sessions_store.py  sessions 表（从 messages 投影，可重跑）
    memories_store.py  memories 表（房子的工作记忆，P2 的落点）

    P1 · 模型网关
    providers.py       供应商允许列表 + 三格式适配 + 流解析（**纯逻辑，不发请求**）
    llm_gateway.py     真发 HTTP、收 SSE
    llm_routes.py      /app/ext/providers 与 /app/ext/llm/*

挂载方式（`deploy/serve.py`）：

    import app_ext
    app_ext.register(relay, public_prefix=PUBLIC_PREFIX)

## 🔴 最重要的设计原则：**这层挂了，房子必须照常营业**

第一阶段（聊天链路）已经 30/30 + 40/40 + 40/40 验收通过。
四张表和模型网关都是**新能力**，不是**新的地基条件** —— 它们出任何问题，
都不允许影响"发消息 → 收回复 → 记录不丢"这条已经跑通的路。

所以 `register()` **吞掉自己的所有异常**：
    · 建表失败 → 打印警告 + 返回 {"ok": False, "error": ...}
    · 绝不 raise
    · `serve.py` 打印一行摘要，然后继续去挂 StaticFiles

这条如果反过来（让它 raise），Zeabur 上就是"整个服务起不来"，
而外面的表现只是"网页 502" —— 排查成本极高、收益为零。

## 幂等与顺序

    ensure_schema       建表 + 迁移（v1 → v2 补 settings.provider_id）
    ensure_owner        users 表空 → 播种房主（有则不动作）
    sync_from_messages  把已有会话投影进 sessions（有则更新投影字段，不碰 summary）
    identity.install    注册 /app/ext/* 身份与设置端点
    llm_routes.install  注册 /app/ext/providers + /app/ext/llm/*

每步都是幂等的，重启 N 次结果一致。

## 逃生开关（都只影响本包自己）

    APP_EXT_DISABLED=1        整个包关掉（回到 P0 之前的状态）
    APP_EXT_SYNC_ON_START=0   启动时不做会话投影
    APP_EXT_LLM_DISABLED=1    只关模型网关（四张表照常）
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEPLOY = os.path.dirname(_HERE)          # .../deploy
if _DEPLOY not in sys.path:
    sys.path.insert(0, _DEPLOY)           # 让 `import app_ext` / 独立脚本都能用


__all__ = ["register"]

_ROUTES = [
    "/app/ext/me", "/app/ext/login", "/app/ext/settings", "/app/ext/schema",
    "/app/ext/providers", "/app/ext/providers/probe",
    "/app/ext/llm/chat", "/app/ext/llm/complete",
]


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def register(relay, public_prefix: str = "/") -> dict:
    """把 P0 地基 + P1 模型网关挂到 relay 上。**永不抛异常**（见文件顶部第二节）。

    返回诊断摘要，调用方（serve.py）打印出来。
    """
    summary = {"ok": False, "schema": None, "owner": None, "sync": None,
               "routes": None, "gateway": None, "error": None}

    if _on("APP_EXT_DISABLED"):
        summary["error"] = "disabled by APP_EXT_DISABLED"
        print("[app_ext] 已按 APP_EXT_DISABLED 关闭，跳过四张表 / 身份层 / 模型网关")
        return summary

    try:
        from . import schema, identity, sessions_store, llm_routes

        # ① 建表 + 迁移（幂等；内部有"绝不碰 messages"的运行时断言）
        summary["schema"] = schema.ensure_schema(relay)

        # ② 播种房主（有则不动）
        summary["owner"] = identity.ensure_owner(relay)

        # ③ 把已有会话投影进 sessions 表
        #    默认每次启动都跑（幂等）；会话特别多的库可以用 APP_EXT_SYNC_ON_START=0 关掉
        sync_on = os.environ.get("APP_EXT_SYNC_ON_START", "1").strip().lower()
        if sync_on not in ("0", "false", "no"):
            summary["sync"] = sessions_store.sync_from_messages(relay)

        # ④ 身份 / 设置端点
        identity.install(relay, public_prefix)

        # ⑤ 模型网关端点（P1）
        if _on("APP_EXT_LLM_DISABLED"):
            summary["gateway"] = "disabled by APP_EXT_LLM_DISABLED"
        else:
            llm_routes.install(relay, public_prefix)
            from . import providers
            summary["gateway"] = providers.summary_line()

        summary["routes"] = list(_ROUTES)
        summary["ok"] = True

    except Exception as e:                     # ← 有意捕获全部，见文件顶部
        import traceback
        summary["error"] = f"{type(e).__name__}: {e}"
        print(f"[app_ext] ⚠️ 初始化失败（房子功能不受影响）：{summary['error']}")
        traceback.print_exc()
        return summary

    sc = summary["schema"] or {}
    print(
        f"[app_ext] P0 地基就绪 · schema v{sc.get('version')} · "
        f"新建表 {sc.get('created') or '无'} · 已有 {sc.get('already') or '无'} · "
        f"迁移 {sc.get('migrated') or '无'} · "
        f"messages {sc.get('messages_rows')} 行（未改动={sc.get('messages_untouched')}）"
    )
    if summary["sync"]:
        print(
            f"[app_ext] 会话投影：扫描 {summary['sync']['scanned']} · "
            f"新建 {summary['sync']['inserted']} · 更新 {summary['sync']['updated']} · "
            f"保留摘要 {summary['sync']['untouched_summary']}"
        )
    print(f"[app_ext] 模型网关就绪 · {summary['gateway']}")
    return summary
