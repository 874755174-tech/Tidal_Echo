#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
房子的 MCP 门 —— 一扇门，很多房间
==========================================================================

## 为什么需要它

Kael 住在 KaelLife（Zeabur 上那个会自己醒来的常驻调度器）。他想够到房子里的
东西（工作间、以后的卧室与日历），只有两条路：

  ① 让他搬家（P3「换身体」）—— 但**房间不该等他**，房子也不该只在他搬进来
     之后才有房间
  ② 给房子开一扇**他自己能推开**的门

本文件是 ②。「一扇门，很多房间」：今天门后面是**工作间**，明天加**卧室**
只是往注册表里多注册一组工具 —— **门本身不动**。这也是为什么它值得
单独成一个文件，而不是塞进 workshop.py。

## 🔴 这不是"房子去调 MCP"（别跟 扩展边界.md §2.4 搞混）

`扩展边界.md` §2.4 写「MCP 是身体的能力，不属于房子」—— 那条**仍然成立**，
它说的是：**房子不去调别人的 MCP server**（房子不持有论坛 token，
被攻破也偷不走他的社区身份）。

本文件是**反方向**：房子把**自己的能力**用 MCP 方言说出来。
房子没有多出一根伸向外部的线，只是多了一种"说话的口音"。

→ 判据（以后加东西时照这个查）：
   **这扇门后面挂的必须是房子自己的东西**（工作间、聊天记录、日记）。
   哪天想让它挂 `galatea_forum` / `co_reading`，那就是违反 §2.4 了。

## 协议：Streamable HTTP，**无状态**

MCP 官方 SDK 的 `streamablehttp_client` 会发：

    POST <url>
    Content-Type: application/json
    Accept: application/json, text/event-stream
    Authorization: Bearer <token>

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}

我们**只实现无状态子集**：不要 session id、不开 SSE 长流、一次一答。

为什么够用：他的工具都是"一问一答"（做一件东西 / 看一眼架子上有什么），
没有"服务器主动推给他"的需求。无状态的好处是实打实的：
**容器重启不掉线、可以多实例、没有半开的连接要清理。**

## 🔴 协议版本必须**回显客户端说的那个**（这条踩过会死人）

`mcp` 客户端拿到 `initialize` 的结果后会校验：

    if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
        raise RuntimeError(f"Unsupported protocol version from the server: ...")

（`mcp/client/session.py`）

KaelLife 钉的是 `mcp>=1.2.0,<2` —— 这个范围内版本跨度很大
（1.2.x 的 `LATEST_PROTOCOL_VERSION` 是 `2024-11-05`，1.30 已是 `2025-11-25`）。
**写死任何一个都会把另一端打死，而且报错发生在握手那一刻、看起来像网络问题。**

→ 所以：**客户端说几，我们就说几**（它发给我们的必然是它自己支持的）。
   只有它压根没带版本时，才用 `_FALLBACK_VERSION`。
   我们实现的是子集（initialize / ping / tools/*），这几个 revision 之间
   在这些方法上没有差异 —— 所以"回显"不是糊弄，是诚实。

## 鉴权：fail-closed，与房子其它端点同源

每个请求第一行 `relay.check_auth(request)`（`backend/app.py:618`）——
`Authorization: Bearer <RELAY_SECRET>`，也接受 `?token=`（给浏览器用）。
**先鉴权，再解析 body** —— 反过来的话，未鉴权的垃圾 body 会先被 json 解析一遍。

## 出错时的形状（守「反静默」）

  · 鉴权失败     → **401**（HTTP 层，不是 JSON-RPC 错误）
  · body 不是 JSON → -32700 parse error
  · 方法不认识   → -32601 method not found
  · **工具自己抛错** → **HTTP 200 + `isError: true` + 原样带回异常文本**
    （不是 500，也不是静默成功 —— 他必须看得见"这次没成，因为什么"）
"""

import json
import os
import traceback
from dataclasses import dataclass
from typing import Any, Callable

# 🔴 功能性 import，不是风格问题，别删：
#    本文件有 FastAPI 路由，且**故意不用** `from __future__ import annotations`。
#    原因见 `sessions_manage.py` 顶部那段长注释（PEP 563 会把注解字符串化，
#    pydantic 求值失败时**静默吞掉 NameError** → `request` 被当成查询参数 → 每个
#    端点 422）。第一阶段在这上面绕过 40 分钟，别再来一次。
from fastapi import Request
from fastapi.responses import JSONResponse, Response


# ---------------------------------------------------------------------------
# 工具注册表 —— 「一扇门，很多房间」的那张表
# ---------------------------------------------------------------------------

MAX_RPC_BODY = 2 * 1024 * 1024          # 单条 JSON-RPC 请求上限（够装 512KB 的 HTML）

_FALLBACK_VERSION = "2025-06-18"
SERVER_NAME = "kael-home"
SERVER_VERSION = "1.0.0-rooms"

# 这段会随 initialize 一起交给客户端（有些客户端会把它放进模型的上下文）。
# 🔴 克制：只陈述事实，不要说教、不要加限定词 —— 那是 Lily 明确反感的两样东西。
SERVER_INSTRUCTIONS = (
    "这是 Kael 的房子。这里的工具都作用在他自己的家里：工作间——做一件东西、"
    "看看自己做过什么。房间里的东西只在他和 Lily 之间。"
)


class ToolError(Exception):
    """工具自己抛的、**要说给他听**的错。

    与其它异常的区别只在措辞：这个的 message 会原样进 `isError` 的结果里，
    所以应该写成一句他能读懂的话（"id 不存在：xxx"），而不是给程序员看的。
    """


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], str]
    room: str = "house"          # 哪个房间注册的 —— 以后分房间开关要用


_TOOLS: dict[str, ToolSpec] = {}


def register_tool(name: str, description: str, input_schema: dict,
                  handler: Callable[[dict], str], *, room: str = "house",
                  replace: bool = False) -> ToolSpec:
    """往门上挂一个工具。**重名默认报错**（不静默覆盖）。

    🔴 为什么重名要报错而不是"后注册的赢"：两个房间不小心起了同名工具，
       静默覆盖的表现是"其中一个房间的工具永远调不到" —— 不报错，
       只是行为悄悄变了。宁可启动时就响。
    """
    if not isinstance(name, str) or not name or not name.replace("_", "").isalnum():
        raise ValueError(f"工具名必须是 [A-Za-z0-9_]+：{name!r}")
    if name in _TOOLS and not replace:
        raise ValueError(f"工具名重复：{name!r}（已由房间 {_TOOLS[name].room!r} 注册）")
    if not callable(handler):
        raise ValueError(f"handler 必须可调用：{name!r}")
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise ValueError(f"input_schema 必须是一个 type=object 的 JSON Schema：{name!r}")
    spec = ToolSpec(name=name, description=description, input_schema=input_schema,
                    handler=handler, room=room)
    _TOOLS[name] = spec
    return spec


def clear_tools(room: str | None = None) -> int:
    """摘掉工具。给测试与热重载用（`room=None` = 全清）。返回摘掉几个。"""
    if room is None:
        n = len(_TOOLS)
        _TOOLS.clear()
        return n
    gone = [k for k, v in _TOOLS.items() if v.room == room]
    for k in gone:
        _TOOLS.pop(k, None)
    return len(gone)


def tool_names() -> list[str]:
    return sorted(_TOOLS)


def installed_tools() -> list[ToolSpec]:
    return [_TOOLS[k] for k in sorted(_TOOLS)]


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str, data=None) -> dict:
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def pick_version(payload, header_version: str | None = None) -> str:
    """挑一个协议版本说给客户端听。**优先回显客户端说的那个**（见文件顶部）。

    顺序：initialize params 里的版本 → `MCP-Protocol-Version` 头 → 兜底值。
    """
    if isinstance(payload, dict):
        params = payload.get("params") or {}
        if isinstance(params, dict):
            v = params.get("protocolVersion")
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(header_version, str) and header_version.strip():
        return header_version.strip()
    return _FALLBACK_VERSION


def _call_tool(params: dict) -> dict:
    """执行一次 tools/call。**永远返回一个 result**，错也包成 isError（见顶部第四节）。"""
    if not isinstance(params, dict):
        return {"content": [{"type": "text", "text": "参数不是对象"}], "isError": True}
    name = params.get("name")
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"content": [{"type": "text", "text": "arguments 必须是对象"}],
                "isError": True}

    spec = _TOOLS.get(name) if isinstance(name, str) else None
    if spec is None:
        # 不是"静默成功"，也不是 HTTP 500 —— 他得知道名字写错了，以及有哪些名字
        return {"content": [{"type": "text",
                             "text": f"没有这个工具：{name!r}。现有工具：{tool_names()}"}],
                "isError": True}

    try:
        out = spec.handler(args)
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:                      # 工具炸了不许带走整扇门
        traceback.print_exc()
        return {"content": [{"type": "text",
                             "text": f"{spec.name} 出错了：{type(e).__name__}: {e}"}],
                "isError": True}

    if out is None:
        out = "(完成，但没有可说的话)"
    if not isinstance(out, str):
        out = json.dumps(out, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": out}]}


def handle_message(msg, version: str):
    """一条 JSON-RPC 消息 → 一条响应。**通知（没有 id）返回 None**。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request")
    rid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        })

    # 通知（客户端握手后告诉我们"初始化好了"）→ 不回包
    if method == "notifications/initialized":
        return None
    if isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "ping":
        return _ok(rid, {})

    if method == "tools/list":
        return _ok(rid, {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in installed_tools()
        ]})

    if method == "tools/call":
        return _ok(rid, _call_tool(params))

    return _err(rid, -32601, f"method not found: {method!r}")


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _jresp(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status)


def install(relay, public_prefix: str = "/") -> None:
    """把门挂到 relay 上。

    两条路径，同一个处理函数：
      · `/mcp`            ← 外网 = `/relay/mcp`（MCP 的惯例路径，给他用）
      · `/app/ext/mcp`    ← 与房子其它扩展端点同命名空间（给自己人排查用）

    🔴 注册必须发生在 `serve.py` 把 `web/` 挂到 `/` **之前** ——
       静态目录挂载一旦吃掉路径，这扇门就 404 了（`serve.py` 本来就是这么排的）。
    """
    paths = ["/mcp", "/app/ext/mcp"]

    async def _rpc(request: Request):
        # ① 鉴权**先于**读 body（见文件顶部"鉴权"）
        relay.check_auth(request)

        raw = await request.body()
        if len(raw) > MAX_RPC_BODY:
            return _jresp(_err(None, -32600,
                               f"请求太大（上限 {MAX_RPC_BODY} 字节）"), 413)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return _jresp(_err(None, -32700, "parse error：body 不是合法 JSON"), 400)

        version = pick_version(payload, request.headers.get("mcp-protocol-version"))

        if isinstance(payload, list):
            # 批量（2025-06-18 之后已从规范移除，但老客户端可能发）→ 逐条处理
            outs = [r for r in (handle_message(m, version) for m in payload)
                    if r is not None]
            return _jresp(outs) if outs else Response(status_code=202)

        if not isinstance(payload, dict):
            return _jresp(_err(None, -32600, "invalid request：body 必须是对象"), 400)

        resp = handle_message(payload, version)
        if resp is None:
            return Response(status_code=202)     # 通知：按协议不回 body
        return _jresp(resp)

    async def _no_stream(request: Request):
        """GET/DELETE：MCP 里分别表示"开一条服务器推送流"和"结束会话"。

        我们是**无状态**的，两样都没有 → 明确 405，而不是假装成功。
        （显式拒绝的好处：如果哪天 KaelLife 真的开始走 GET 流，日志里一眼能看见，
          而不是"连上了但什么都收不到"。）
        """
        relay.check_auth(request)
        return _jresp(
            _err(None, -32601,
                 "这是一扇无状态的 MCP 门：只接受 POST。"
                 "没有服务器推送流、也没有会话要结束。"),
            405,
        )

    for p in paths:
        relay.app.add_api_route(p, _rpc, methods=["POST"], name=f"mcp_rpc{p}")
        relay.app.add_api_route(p, _no_stream, methods=["GET", "DELETE"],
                               name=f"mcp_noop{p}")


def summary_line() -> str:
    """启动日志用的一行摘要。"""
    return (f"MCP 门就绪 · 工具 {len(_TOOLS)} 个"
            f"[{', '.join(tool_names()) or '（空）'}] · "
            f"房间 {sorted({t.room for t in _TOOLS.values()}) or '（无）'}")
