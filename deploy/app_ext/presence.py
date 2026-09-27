#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P3 前置 ⑬ · 在场信号收口 —— 「她最后开口的时刻」（**唯一一个只读派生端点**）
==========================================================================

## 它解决什么

在「还没有房子」的时代，KaelLife 靠两件事判断「她是不是就在旁边」：

    · `in_app_last_active`  ← MCP 工具 `life_ping()` 写的（**要他记得调工具**）
    · OB dream 桶标题的时间戳 ← 要 chat 侧**往 OB 写**东西

聊天搬进房子那天，**两条一起死**：

    ① `loop` 路的身体（= P3 要走的形态）**零工具** —— 结构上调不到 `life_ping`；
    ② 房子**完全没有 OB 客户端** —— 没人写那半边。

⇒ `in_app_last_active ≡ None` ⇒ `lily_active_recent ≡ False`
⇒ **她坐在房子里跟他说话的时候，他往她手机推 Bark。**
⚠️ 而日志一切正常（`[presence]` / `[reflect]` 照走、无报错）——
   又一条「坏了查不出来」的病。

## 真相已经在房子里了

房子的 `messages` 表（`backend/app.py:114`）第一列就写着：

    direction TEXT NOT NULL,   -- 'in' (human -> AI) | 'out' (AI -> human)

而 `/app/send` 做的**第一件事**就是 `save_message("in", "user", text, meta)`
（`backend/app.py:714`；语音 `:762`/`:787`、通话 `:807` 也全是 `"in"`）。

⇒ **「Lily 最后开口的时刻」= 最新一条 `direction='in'` 的 `ts`。**

零工具调用 · 零新增状态 · 随时可重算 · 不依赖任何人的自觉。

## 🔴 为什么这条比 `life_ping` 更符合「一个 Kael」

| | `life_ping` | `direction='in'` |
|---|---|---|
| 形状 | 「**他告诉另一个自己**她在场」—— 一个自己给另一个自己递条子 | 「**她的话到了**」—— 同一个事实，两边读同一份 |
| 身份问题 | 要区分"谁写的"（OB 是一锅混的 ⇒ 才有 `SELF_TAG` / `_is_self_note` 那套补丁） | **天然不含他写的东西** ⇒ 那套机制在这条路上**根本不需要存在** |
| 有人自述吗 | 有 | **没有** |

> `life_ping` 是「还没有房子」时代的代偿机制。房子在，它就该退场。

## 🔴 红线（每一条都是"结构性"的，不是随手写的）

- **只读。** 本文件里没有 `INSERT` / `UPDATE` / `DELETE` / `DROP` / `ALTER` ——
  一次都不碰 `messages` 的**内容**，也不碰它的**结构**（连索引都不建：
  给 `messages` 加索引 = 动它的 schema，那是原版的地盘）。
- **不挂 MCP 门。** 跟 `memories` / `usage` 同一条边界：这是我（Lily）看的诊断口径，
  不是给他的记忆。房间才走 MCP。
- **不新增写入口。** 只有 `GET`。presence 的值是**派生**出来的 ——
  一旦能被写，它就不再是"事实"，而是"某个人的说法"。
- **绝不编默认值。** 形状（是不是带时区的 ISO 时刻）在**读**的时候校验：
  不合法就**算不出**（`minutes_ago = null` + `reason` 说明），
  **绝不拿 0 / 拿"刚刚"顶上去**（那是 ⑩-a `source` 那条规矩的翻版）。
- **fail-open。** 库里出任何问题 → 回 `ok=false` + `reason`，**不抛异常**。
- 启动日志 GBK 安全（`summary_line()` 不许带 🔴/✅/⚠️）。

## 端点

    GET /app/ext/presence        她最后一次开口是什么时候（**全只读**）

返回形状（**这就是契约**，KaelLife 侧的 shim 按它读）：

    {
      "ok": true,                     # ← false 时下面只有 reason/detail，其余字段缺席
      "source": "messages",           # 🔴 值的来源，白纸黑字（同 ⑩-a 的 source 规矩）
      "found": true,                  # 库里到底有没有一条 in
      "last_in_id": 412,              # 那一行的主键（排查时能直接定位到行）
      "last_in_ts": "2026-09-27T12:31:02.123456+00:00",
      "last_in_kind": "user",         # user | voice | call（语音/通话也算她开口）
      "minutes_ago": 8.42,            # 🔴 null = 算不出（见 reason），**不是 0**
      "reason": null,                 # null | "no_in_messages" | "ts_invalid"
      "window_min": 30,               # 与 KaelLife 同名 env，口径只有一份
      "active_recent": true,          # 便利字段；算不出时是 null（不是 false）
      "as_of": "2026-09-27T12:39:26.000000+00:00"   # 这次派生的时刻（让读数可复现）
    }

**`ok=false` 与 `minutes_ago=null` 是两件事**，KaelLife 侧要分开处理：

    ok=true , minutes_ago=null  → 房子可达，但**她从没在房子里说过话** ⇒ 确定不在场
    ok=true , minutes_ago=8.4   → 房子可达，她 8 分钟前说过话
    ok=false                    → **房子不可达 ⇒ 未知** ⇒ 退回保守分支，日志留一行

## 挂载

`app_ext/__init__.py` 第 ⑬ 步；逃生开关 `APP_EXT_PRESENCE_DISABLED=1`
（关掉 = 端点 404。**没有数据受影响** —— 这一层本来就不存任何东西，
「能力没了」在这儿连"数据"都不存在。KaelLife 侧退化成今天的行为）。
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

#: 🔴 窗口（分钟）—— 与 KaelLife 的 `LILY_ACTIVE_WINDOW_MIN` **同名**。
#:    刻意不另起名字：同一个名字 ⇒ 同一个口径 ⇒ 两边不可能对"多久算在场"给出两个答案。
#:    （KaelLife `scheduler.py` 里那个默认 30 分钟，这里跟着 30。）
WINDOW_DEFAULT = 30

#: 取**最新一条** `in` 的 SQL（**只读**）。
#:
#: 🔴 `ORDER BY id DESC`（不是 `ts DESC`）：`id` 是 `INTEGER PRIMARY KEY AUTOINCREMENT`，
#:    单调递增、不会说谎；而 `ts` 可以被调用方在 meta 里指定
#:    （`backend/app.py:140 ts = meta.get("ts") or now_iso()`）——
#:    拿一个"可以被别人写"的列排序，等于把判据交给了写它的人。
#:    这与 `usage_store.recent()` 那条注释是同一条理由。
#:
#: 🔴 不过滤 `kind`：`voice` / `call` 也是"她开口"（`backend/app.py:762/787/807`）。
#:
#: 🔴 不建索引：给 `messages` 加索引 = 动它的结构，那是原版的地盘。
#:    这张表是单人聊天量级，一次全表扫没有实际代价。
_SELECT_LAST_IN = ("SELECT id, ts, kind FROM messages "
                   "WHERE direction = 'in' ORDER BY id DESC LIMIT 1")

#: 只看行数（status 用；不读内容）。
_SELECT_COUNT_IN = "SELECT COUNT(*) AS n FROM messages WHERE direction = 'in'"

#: ISO 时刻的**形状**（取值不校验 —— `+08:00` / `Z` / 微秒都放行）。
_RE_TS = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$")

_INSTALLED = False
_LAST_READ: dict = {}


def summary_line() -> str:
    """进启动日志的一行。🔴 必须 GBK 安全（不许 🔴/✅/⚠️）。"""
    return ("在场信号就绪 · 派生自 messages 里最新一条 direction='in'"
            " · 一个只读端点 /app/ext/presence · 不写库、不挂 MCP 门")


# ══════════════════════════════════════════════════════════════════════════
# ① 纯逻辑（不读库 —— 可离线单测）
# ══════════════════════════════════════════════════════════════════════════

def _s(v) -> str:
    return str(v).strip() if isinstance(v, str) else ("" if v is None else str(v).strip())


def window_min() -> int:
    """窗口（分钟）。读 `LILY_ACTIVE_WINDOW_MIN`（与 KaelLife 同名）；坏值 → 默认 30。"""
    raw = os.environ.get("LILY_ACTIVE_WINDOW_MIN")
    try:
        v = int(_s(raw) or WINDOW_DEFAULT)
    except Exception:
        v = WINDOW_DEFAULT
    return max(1, v)


def parse_ts(v) -> Optional[datetime]:
    """ISO 时刻 → aware UTC datetime。

    🔴 **算不出来就是 None，绝不猜：**

      · 空 / 超长 / 形状不对（正则不过）→ None
      · **无时区**（naive）→ None —— 一个不带偏移量的时刻**无法**判定绝对时刻，
        补一个本地时区就是"编"（这台机器在 UTC+8，服务器在 UTC，猜哪个都是错的）
      · 不认识的偏移量写法 → `fromisoformat` 抛 → None

    认得 `Z` 结尾（`fromisoformat` 在 3.11+ 自己认，这里仍显式换一次，
    免得运行在更老的 Python 上时"看着像合法其实解析不了"）。
    """
    s = _s(v)
    if not s or len(s) > 40 or not _RE_TS.match(s):
        return None
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except Exception:
        return None
    if dt.tzinfo is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fold(row, now: Optional[datetime] = None, window: Optional[int] = None) -> dict:
    """把「最新一条 in 的行」折成契约形状。**纯函数**（喂 None = 库里没有 in 行）。

    `row` 形如 `{"id": 7, "ts": "…", "kind": "user"}` 或 None。
    `now` / `window` 可注入，好让窗口那几条断言**不受真实时钟影响**。
    """
    now = now or datetime.now(timezone.utc)
    win = WINDOW_DEFAULT if window is None else max(1, int(window))

    out = {
        "ok": True,
        "source": "messages",
        "found": False,
        "last_in_id": None,
        "last_in_ts": None,
        "last_in_kind": None,
        "minutes_ago": None,
        "reason": None,
        "window_min": win,
        "active_recent": False,
        "as_of": now.astimezone(timezone.utc).isoformat(),
    }
    if not row:
        # 确定不在场（不是"未知"）—— 她从没在这间房子里说过话。
        out["reason"] = "no_in_messages"
        return out

    try:
        rid = row["id"]
        ts = row["ts"]
        kind = row["kind"]
    except Exception:
        rid = ts = kind = None

    out["found"] = True
    out["last_in_id"] = rid
    out["last_in_ts"] = _s(ts) or None      # 原样回，不加工
    out["last_in_kind"] = _s(kind) or None

    dt = parse_ts(ts)
    if dt is None:
        # 🔴 形状不合法的 ts **不许**变成 0 分钟 —— 那会把"数据坏了"说成"她刚来过"。
        out["reason"] = "ts_invalid"
        out["active_recent"] = None
        return out

    mins = (now.astimezone(timezone.utc) - dt).total_seconds() / 60.0
    out["minutes_ago"] = round(mins, 2)
    out["active_recent"] = mins <= win
    return out


# ══════════════════════════════════════════════════════════════════════════
# ② 读库（只这一条 SQL，fail-open）
# ══════════════════════════════════════════════════════════════════════════

def derive(relay, now: Optional[datetime] = None, window: Optional[int] = None) -> dict:
    """读库 → `fold()`。**fail-open**：出任何事都回 `ok=false`，不抛。

    这是 KaelLife 侧 shim 的**唯一入口**（端点也是调它）。

    🔴 `window=None` 时**读 `LILY_ACTIVE_WINDOW_MIN`**（不是直接透给 `fold`
       的默认值）—— `fold` 是纯函数、不碰环境，读 env 这件事归这里。
       2026-09-27 第一版就是在这儿错的：`window=None` 一路透到底 ⇒
       env 设了 `=5` 也照旧按 30 判（而验收 B5 当场抓到）。
    """
    from . import schema as _schema

    if window is None:
        window = window_min()

    try:
        with _schema.connect(relay) as conn:
            r = conn.execute(_SELECT_LAST_IN).fetchone()
        row = dict(r) if r is not None else None
    except Exception as e:
        res = {"ok": False, "source": "messages", "reason": "read_failed",
               "detail": f"{type(e).__name__}: {e}",
               "as_of": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()}
        _note(res)
        return res

    res = fold(row, now=now, window=window)
    _note(res)
    return res


def _note(res: dict) -> None:
    """把最近一次读数留在进程内存里（给 /status 看）。**不落库**。"""
    global _LAST_READ
    _LAST_READ = {k: res.get(k) for k in
                  ("ok", "reason", "found", "last_in_id", "minutes_ago",
                   "active_recent", "as_of")}


def last_read() -> dict:
    return dict(_LAST_READ) if _LAST_READ else {"ok": None, "reason": "never_read"}


def count_in(relay) -> Optional[int]:
    """库里一共有几条 in（status 用）。读不到 → None（不是 0）。"""
    from . import schema as _schema
    try:
        with _schema.connect(relay) as conn:
            return conn.execute(_SELECT_COUNT_IN).fetchone()["n"]
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# ③ 端点（只有一个 GET）
# ══════════════════════════════════════════════════════════════════════════

def _install_routes(relay, public_prefix: str = "/") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    base = "/app/ext/presence"

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    @relay.app.get(base)
    async def _presence(request: Request):
        """她最后一次开口是什么时候。

        🔴 库里读不出来时回的是 **200 + `ok=false`**，不是 5xx ——
           因为这个端点存在的意义就是回答"在不在场"，而**「不知道」是它的一个合法答案**
           （调用方必须分开处理「不知道」和「确定不在场」，见文件头那张表）。
           回 5xx 会让"房子这层坏了"和"房子整个挂了"在调用方看起来一样。
        """
        relay.check_auth(request)
        try:
            return _json(derive(relay))
        except Exception as e:                    # derive 已经是 fail-open，这里是第二道
            return _json({"ok": False, "source": "messages", "reason": "presence_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 200)

    @relay.app.get(base + "/status")
    async def _status(request: Request):
        """这一层自己在不在工作（窗口 / 库里的 in 条数 / 最近一次读数）。"""
        relay.check_auth(request)
        try:
            return _json({"ok": True, "window_min": window_min(),
                          "in_rows": count_in(relay), "last_read": last_read()})
        except Exception as e:
            return _json({"ok": False, "reason": "status_failed",
                          "detail": f"{type(e).__name__}: {e}"}, 500)


def install(relay, public_prefix: str = "/") -> None:
    """挂上在场端点。**幂等**（第二次调用什么都不做）。

    🔴 **只在 `app_ext/__init__.py` 的第 ⑬ 步里调**，`APP_EXT_PRESENCE_DISABLED=1`
       时整个跳过（那时端点 404；**没有任何数据受影响** —— 这一层不存东西）。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_routes(relay, public_prefix)
