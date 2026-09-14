#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 · 身份层 —— users + settings
==========================================================================

## 解决什么问题

第一阶段的"登录"是：前端存一把 `RELAY_SECRET`，每次请求带在
`Authorization: Bearer` 或 `?token=` 上，后端 `check_auth()` 做常量时间比较。
**能用，但"设置属于谁"这件事没有落脚点** —— 于是：

  · persona / 模型 / 温度 只能塞环境变量 → 改一次要重新部署一次
  · 想加多一个身份（guest）就得再造一套隔离
  · 前端"设置页 10 项里 7 项是假按键"（记在专项记忆里）—— 没地方存

本文件把身份与设置变成真实载体。

## 🔴 最重要的设计决定：**不动现有鉴权**

现有 `check_auth()`（`backend/app.py:618`）**一个字不改**，它仍然比对
`RELAY_SECRET`。本文件新增的一切都是**附加能力**：

    GET  /app/ext/me          → 我是谁 + 我的设置
    POST /app/ext/login       → 用密钥换身份信息（证明"你是谁"）
    GET  /app/ext/settings    → 读设置
    PUT  /app/ext/settings    → 写设置（白名单字段）
    GET  /app/ext/schema      → 诊断：四张表在不在、版本号、行数

前四个端点**各自调用 `relay.check_auth(request)`**（fail-closed，
不依赖任何中间件顺序 —— 沿用 `sessions_manage.py` 的教训）。
`login` 是例外，它本身就是"证明你是谁"的动作，所以自己比对密钥。

**为什么不做成"取代 check_auth"**：
  一次改两个东西（存储 + 鉴权路径）= 出问题时分不清是谁的锅。
  先把身份存起来、能读能写，**等 P1 需要真正多身份时再切**。
  在那之前，这条路上的所有新增端点都是"多一层纸"，不是"换一根梁"。

## 🔴 secret 怎么存

    users.secret_hash = "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>"

不存明文、不存可逆值。当前**它不参与鉴权**（鉴权还是比 RELAY_SECRET），
存它是为了 P1 —— 到那时"登录"才真的查这张表。
⚠️ 现在如果有人改了 users.secret_hash，**登录行为不会变**。
   这是有意的（见上一节），但要知道，别误以为它已经生效。

## ⚠️ 关于 FastAPI 参数注解（老坑，别删注释）

本文件**没有** `from __future__ import annotations`，**必须**保留
`from fastapi import Request`。原因见 `sessions_manage.py` 顶部那段长注释：
PEP 563 会把注解字符串化，pydantic 求值失败时**静默吞掉 NameError**，
于是 `request` 被当成查询参数 → 每个端点 422。第一阶段在这上面绕过 40 分钟。
"""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone

# 🔴 功能性 import，不是风格问题，别删（见文件顶部第三节）
from fastapi import Request

from . import schema as _schema   # 包内相对导入（app_ext 是个包）


OWNER_ID = "u_owner"
PBKDF2_ITERATIONS = 120_000

# 允许通过 API 写入的 settings 字段（白名单）。
# 🔴 **绝不接受任意键** —— 否则前端能塞 `user_id` / 任意列名。
WRITABLE_FIELDS = {
    "persona": str,
    "model_id": str,
    "max_tokens": int,
    "temperature": float,
    "top_p": float,
    "context_keep": int,
    "context_trigger": int,
    "effort": str,
}

EFFORT_VALUES = {"low", "medium", "high", "max"}
PERSONA_MAX_LEN = 20_000
EXTRA_MAX_BYTES = 8_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 密钥哈希
# ---------------------------------------------------------------------------

def hash_secret(secret: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    """校验明文是否匹配存的哈希。格式不对一律返回 False（不抛错）。"""
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 播种：让 users 表里有一条"房主"
# ---------------------------------------------------------------------------

def ensure_owner(relay) -> dict:
    """users 表空的时候，用环境变量播种一条 owner。幂等。

    · id   = `u_owner`（固定、可读、可复现 —— 不用随机 UUID，重建也稳定）
    · handle = `RELAY_HUMAN_NAME` 小写（默认 "lily"）
    · secret_hash = pbkdf2(RELAY_SECRET)

    ⚠️ 如果 `RELAY_SECRET` 为空，后端在启动时就 SystemExit 了（app.py:91），
       所以这里拿到的密钥一定非空。
    """
    human = (getattr(relay, "HUMAN_NAME", "") or "lily").strip()
    handle = "".join(ch for ch in human.lower() if ch.isalnum() or ch in "-_") or "lily"
    secret = getattr(relay, "SECRET", "") or ""

    with _schema.connect(relay) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n:
            return {"seeded": False, "users": n}
        conn.execute(
            "INSERT INTO users (id, handle, display_name, secret_hash, role, created) "
            "VALUES (?,?,?,?,?,?)",
            (OWNER_ID, handle, human or "Lily", hash_secret(secret), "owner", now_iso()),
        )
        conn.commit()
    return {"seeded": True, "users": 1, "id": OWNER_ID, "handle": handle}


def get_user(relay, user_id: str = OWNER_ID):
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT id, handle, display_name, role, created FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_users(relay) -> list:
    with _schema.connect(relay) as conn:
        rows = conn.execute(
            "SELECT id, handle, display_name, role, created FROM users ORDER BY created"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 设置读写
# ---------------------------------------------------------------------------

def _default_settings(relay) -> dict:
    """库里还没有这一行时，返回的默认值。

    persona 默认**不在这里造** —— 它来自环境变量 `PERSONA`（如果后端定义了）。
    这里只做"读一次环境"，不写库。这样"改设置"永远是显式动作。
    """
    return {
        "persona": getattr(relay, "PERSONA", None) or None,
        "model_id": None,
        "max_tokens": None,
        "temperature": None,
        "top_p": None,
        "context_keep": None,
        "context_trigger": None,
        "effort": None,
        "extra": {},
        "updated": None,
        "source": "defaults",   # defaults | db
    }


def get_settings(relay, user_id: str = OWNER_ID) -> dict:
    with _schema.connect(relay) as conn:
        row = conn.execute(
            "SELECT * FROM settings WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return _default_settings(relay)
    d = dict(row)
    try:
        d["extra"] = json.loads(d.get("extra") or "{}")
    except Exception:
        d["extra"] = {}
    d["source"] = "db"
    return d


def _coerce(field: str, value):
    """按白名单类型做一次转换 + 范围校验。转换失败抛 ValueError。"""
    if value is None:
        return None
    typ = WRITABLE_FIELDS[field]
    if typ is str:
        if not isinstance(value, str):
            raise ValueError(f"{field} 必须是字符串")
        return value
    if typ is int:
        if isinstance(value, bool):
            raise ValueError(f"{field} 必须是整数")
        return int(value)
    return float(value)


def validate_patch(patch: dict) -> dict:
    """把外部传入的 patch 收敛成"只有白名单字段、类型正确、范围合理"的字典。

    🔴 一切不在白名单里的键**直接丢弃**（不是报错）——
       这样前端多传一个 `user_id` 也不会有什么后果。
       真正致命的字段（user_id / id / secret_hash）根本不在白名单里。
    """
    if not isinstance(patch, dict):
        raise ValueError("body 必须是一个 JSON 对象")

    clean = {}
    for k, v in patch.items():
        if k == "extra":
            if v is None:
                clean["extra"] = {}
            elif not isinstance(v, dict):
                raise ValueError("extra 必须是对象")
            else:
                s = json.dumps(v, ensure_ascii=False)
                if len(s.encode("utf-8")) > EXTRA_MAX_BYTES:
                    raise ValueError("extra 太大")
                clean["extra"] = v
            continue
        if k not in WRITABLE_FIELDS:
            continue                      # 静默丢弃（见上）
        val = _coerce(k, v)
        if k == "persona" and val is not None and len(val) > PERSONA_MAX_LEN:
            raise ValueError(f"persona 超长（上限 {PERSONA_MAX_LEN} 字符）")
        if k == "temperature" and val is not None and not (0.0 <= val <= 2.0):
            raise ValueError("temperature 必须在 0~2")
        if k == "top_p" and val is not None and not (0.0 <= val <= 1.0):
            raise ValueError("top_p 必须在 0~1")
        if k == "max_tokens" and val is not None and not (1 <= val <= 200_000):
            raise ValueError("max_tokens 必须在 1~200000")
        if k == "effort" and val is not None and val not in EFFORT_VALUES:
            raise ValueError(f"effort 只能是 {sorted(EFFORT_VALUES)}")
        if k in ("context_keep", "context_trigger") and val is not None and val < 0:
            raise ValueError(f"{k} 不能是负数")
        clean[k] = val
    return clean


def save_settings(relay, patch: dict, user_id: str = OWNER_ID) -> dict:
    """写设置。**只写 patch 里出现的字段**，其它字段原样保留（不是整行覆盖）。"""
    clean = validate_patch(patch)
    if not clean:
        return {"ok": False, "reason": "no_writable_fields", "changed": []}

    with _schema.connect(relay) as conn:
        exists = conn.execute(
            "SELECT 1 FROM settings WHERE user_id = ?", (user_id,)
        ).fetchone()

        if not exists:
            conn.execute(
                "INSERT INTO settings (user_id, updated) VALUES (?, ?)",
                (user_id, now_iso()),
            )

        sets, vals = [], []
        for k, v in clean.items():
            if k == "extra":
                sets.append("extra = ?")
                vals.append(json.dumps(v, ensure_ascii=False))
            else:
                sets.append(f"{k} = ?")
                vals.append(v)
        sets.append("updated = ?")
        vals.append(now_iso())
        vals.append(user_id)

        conn.execute(f"UPDATE settings SET {', '.join(sets)} WHERE user_id = ?", vals)
        conn.commit()

    return {"ok": True, "changed": sorted(clean.keys()),
            "settings": get_settings(relay, user_id)}


# ---------------------------------------------------------------------------
# 挂载
# ---------------------------------------------------------------------------

def install(relay, public_prefix: str = "/") -> None:
    """把身份/设置端点挂到 relay 上。

    路由全部在 `/app/ext/*` 这个**新命名空间**下 —— 不覆盖原版任何路径，
    也不与 `sessions_manage.py` 的 `/app/sessions/manage/*` 冲突。

    🔴 除 `login` 外，每个端点第一行都是 `relay.check_auth(request)`。
    """
    from fastapi.responses import JSONResponse

    prefix = "/" + (public_prefix or "").strip("/")

    def _norm(path: str) -> str:
        if prefix != "/" and path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    def _json(data: dict, status: int = 200):
        return JSONResponse(content=data, status_code=status)

    base = "/app/ext"

    @relay.app.get(base + "/me")
    async def _me(request: Request):
        relay.check_auth(request)
        return _json({"user": get_user(relay), "settings": get_settings(relay)})

    @relay.app.post(base + "/login")
    async def _login(request: Request):
        """用密钥换身份信息。

        ⚠️ 它**不调 check_auth** —— 它自己就是"证明你是谁"的动作。
           校验用的还是同一把 `RELAY_SECRET`（与 check_auth 同源），
           所以这里通过 ≠ 多了一把钥匙。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        given = str((body or {}).get("secret") or "")
        expected = getattr(relay, "SECRET", "") or ""
        if not given or not expected or not hmac.compare_digest(given, expected):
            return _json({"ok": False, "reason": "unauthorized"}, 401)
        user = get_user(relay)
        if not user:
            return _json({"ok": False, "reason": "no_user"}, 500)
        # 顺手把"密钥哈希"对齐一次（P1 真正用它时不会遇到旧哈希）
        with _schema.connect(relay) as conn:
            row = conn.execute(
                "SELECT secret_hash FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
            if row and not verify_secret(given, row["secret_hash"]):
                conn.execute(
                    "UPDATE users SET secret_hash = ? WHERE id = ?",
                    (hash_secret(given), user["id"]),
                )
                conn.commit()
        return _json({"ok": True, "user": user, "settings": get_settings(relay)})

    @relay.app.get(base + "/settings")
    async def _get_settings(request: Request):
        relay.check_auth(request)
        return _json(get_settings(relay))

    @relay.app.put(base + "/settings")
    async def _put_settings(request: Request):
        relay.check_auth(request)
        try:
            body = await request.json()
        except Exception:
            return _json({"ok": False, "reason": "bad_json"}, 400)
        try:
            res = save_settings(relay, body)
        except ValueError as e:
            return _json({"ok": False, "reason": "invalid", "detail": str(e)}, 400)
        return _json(res, 200 if res.get("ok") else 400)

    @relay.app.get(base + "/schema")
    async def _schema_ep(request: Request):
        """诊断用：四张表在不在、版本号、行数。前端设置页也能显示这个。"""
        relay.check_auth(request)
        rep = _schema.schema_report(relay)
        rep["users"] = list_users(relay)
        return _json(rep)

    _ = _norm   # 防御性冗余（与 sessions_manage.py 同款）
