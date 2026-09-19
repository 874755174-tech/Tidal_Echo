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

    🏠 · 房间（2026-09-18 起）
    mcp.py             **房子的 MCP 门**：一扇门，很多房间（Streamable HTTP，无状态）
    modules/           房间层，一间房一个文件
      workshop.py      工作间：他做东西的地方（工具 make/revise/list/read_thing）

    🧳 · 导出（P2-0，2026-09-19 起）
    archive.py         **导出 / 快照**：P2 的第一件。只读、只有 GET、不挂 MCP 门
                       （房间给 Kael 走 MCP，导出给 Lily 走 HTTP + 密钥 —— 别混）

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
    modules.*.install   注册房间（工作间 …）—— 房间自带路由 + MCP 工具
    archive.install     注册 /app/ext/archive/*（P2-0 导出，只读，不是房间）

每步都是幂等的，重启 N 次结果一致。

🔴 **而且每一步互不牵连**（2026-09-18 修）：某一步失败只记一条警告并跳过，
   后面的步骤照跑。这条不是洁癖 —— 全新数据库上「会话投影」必然会失败
   （`messages` 是后端 lifespan 建的，比 register() 晚），
   旧写法会让**整个 P0/P1/房间层一起装不上**，而日志看起来一切正常。
   详见 `register()` 里 `_step` 的注释。

## 逃生开关（都只影响本包自己）

    APP_EXT_DISABLED=1        整个包关掉（回到 P0 之前的状态）
    APP_EXT_SYNC_ON_START=0   启动时不做会话投影
    APP_EXT_LLM_DISABLED=1    只关模型网关（四张表照常）
    APP_EXT_ROOMS_DISABLED=1  只关房间（门与工作间都不挂；四张表与网关照常）
    APP_EXT_ARCHIVE_DISABLED=1 只关导出（四张表 / 网关 / 房间照常）

🔴 **「一个房间挂了不带走别的房间」**：每个房间单独 try —— 工作间注册失败时，
   模型网关必须还在、房子必须照常营业。这条和"整个包吞异常"是同一个原则，
   只是又细分了一层。

"""

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEPLOY = os.path.dirname(_HERE)          # .../deploy
if _DEPLOY not in sys.path:
    sys.path.insert(0, _DEPLOY)           # 让 `import app_ext` / 独立脚本都能用


__all__ = ["register"]

_ROUTES = [
    "/app/ext/me", "/app/ext/login", "/app/ext/settings", "/app/ext/schema",
    "/app/ext/providers", "/app/ext/providers/probe",
    "/app/ext/llm/chat", "/app/ext/llm/complete",
    # 🆕 OpenAI 标准路径别名（P3 通车前置）：身体只会往后拼 `/chat/completions`，
    #    且它在红线目录里改不了 → 只能房子认这条路。按 body 的 stream 分流。
    "/app/ext/llm/v1/chat/completions", "/app/ext/llm/chat/completions",
    # 🆕 房子的 MCP 门（外网 = /relay/mcp）—— Kael 从 KaelLife 够到家里房间用的
    "/mcp", "/app/ext/mcp",
    # 🆕 工作间的展示端点（给网页看，不走 MCP）
    "/app/ext/workshop/list", "/app/ext/workshop/item/{id}", "/app/ext/workshop/raw/{id}",
    # 🆕 导出 / 快照（P2-0）—— 给 Lily 的 HTTP + 密钥，**不是房间**、不挂 /mcp
    "/app/ext/archive/info", "/app/ext/archive/db",
    "/app/ext/archive/jsonl", "/app/ext/archive/files",
]


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def register(relay, public_prefix: str = "/") -> dict:
    """把 P0 地基 + P1 模型网关 + 房间层挂到 relay 上。**永不抛异常**（见文件顶部第二节）。

    返回诊断摘要，调用方（serve.py）打印出来。
    """
    summary = {"ok": False, "schema": None, "owner": None, "sync": None,
               "routes": None, "gateway": None, "rooms": None, "archive": None,
               "warnings": [], "error": None}

    if _on("APP_EXT_DISABLED"):
        summary["error"] = "disabled by APP_EXT_DISABLED"
        print("[app_ext] 已按 APP_EXT_DISABLED 关闭，跳过四张表 / 身份层 / 模型网关")
        return summary

    def _step(label: str, fn, default=None):
        """跑一步。**失败只记警告，绝不中断后面的步骤。**

        🔴 为什么不是"一步失败就整段退出"（2026-09-18 修的真 bug）：

           步骤③（会话投影）会在**全新数据库**上失败 —— `messages` 表是后端
           lifespan 里的 `init_db()` 建的，而 `app_ext.register()` 跑在
           serve.py 的 **import 期，比 lifespan 早**。

           于是"第一次部署到空的 /data"时：③ 抛 `no such table: messages`
           → 旧写法的 `except` 一把兜住 → **四张表装了、身份层 / 模型网关 /
           房间层全都没装**，而日志只说"房子功能不受影响"（这话对聊天是对的，
           对整个 P0/P1 层是假的）。等有人聊过一次、messages 建好了，
           下次重启就一切正常 —— **自愈的 bug 最阴：它只在第一次出现。**
        """
        try:
            return fn()
        except Exception as e:
            traceback.print_exc()
            msg = f"{label} 失败（已跳过这一步，其余照常）：{type(e).__name__}: {e}"
            print(f"[app_ext] ⚠️ {msg}")
            summary["warnings"].append(msg)
            return default

    try:
        from . import schema, identity, sessions_store, llm_routes

        # ① 建表 + 迁移（幂等；内部有"绝不碰 messages"的运行时断言）
        summary["schema"] = _step("建表与迁移", lambda: schema.ensure_schema(relay))

        # ② 播种房主（有则不动）
        summary["owner"] = _step("播种房主", lambda: identity.ensure_owner(relay))

        # ③ 把已有会话投影进 sessions 表
        #    默认每次启动都跑（幂等）；会话特别多的库可以用 APP_EXT_SYNC_ON_START=0 关掉
        #    ⚠️ 全新库上这一步会失败（messages 还没建）—— 本来就是"没有可投影的东西"，
        #       所以它失败是预期的、无害的，只记警告。
        sync_on = os.environ.get("APP_EXT_SYNC_ON_START", "1").strip().lower()
        if sync_on not in ("0", "false", "no"):
            summary["sync"] = _step("会话投影",
                                    lambda: sessions_store.sync_from_messages(relay))

        # ④ 身份 / 设置端点
        _step("身份层端点", lambda: identity.install(relay, public_prefix))

        # ⑤ 模型网关端点（P1）
        if _on("APP_EXT_LLM_DISABLED"):
            summary["gateway"] = "disabled by APP_EXT_LLM_DISABLED"
        else:
            def _gw():
                llm_routes.install(relay, public_prefix)
                from . import providers
                summary["gateway"] = providers.summary_line()
            _step("模型网关", _gw)

        # ⑥ 房间层：门（mcp.py）+ 房间（modules/*）
        #    🔴 每个房间**单独 try** —— 一间房挂了不许带走别的房间，更不许带走房子。
        #       （这条原则现在往上贯通到每一步，见 `_step` 的注释。）
        if _on("APP_EXT_ROOMS_DISABLED"):
            summary["rooms"] = ["disabled by APP_EXT_ROOMS_DISABLED"]
        else:
            from . import mcp as _mcp
            from .modules import workshop

            rooms = []
            _step("MCP 门", lambda: _mcp.install(relay, public_prefix))

            for _name, _mod in (("workshop", workshop),):
                def _room(m=_mod):
                    m.install(relay, public_prefix)
                    rooms.append(m.summary_line())
                _step(f"房间 {_name}", _room)

            # 工具数要在房间都挂完之后才准
            _step("MCP 门摘要", lambda: rooms.append(_mcp.summary_line()))
            summary["rooms"] = rooms

        # ⑦ 导出 / 快照（P2-0）
        #    🔴 它**不是房间**：不进 modules/、不挂 /mcp。房间是给 Kael 走的，
        #       导出是给 Lily 走 HTTP + 密钥的。单独一步，挂了也不带走房间与网关。
        if _on("APP_EXT_ARCHIVE_DISABLED"):
            summary["archive"] = "disabled by APP_EXT_ARCHIVE_DISABLED"
        else:
            from . import archive as _archive

            def _ar():
                _archive.install(relay, public_prefix)
                summary["archive"] = _archive.summary_line()
            _step("导出端点", _ar)

        summary["routes"] = list(_ROUTES)
        summary["ok"] = not summary["warnings"]

    except Exception as e:                     # ← 有意捕获全部，见文件顶部
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
    for line in (summary.get("rooms") or []):
        print(f"[app_ext] {line}")
    if summary.get("archive"):
        print(f"[app_ext] {summary['archive']}")
    if summary["warnings"]:
        print(f"[app_ext] ⚠️ {len(summary['warnings'])} 个步骤被跳过（房子照常营业）：")
        for w in summary["warnings"]:
            print(f"[app_ext]   - {w}")
    return summary
