#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2 · usage 记账 —— 把上游每次调用回来的 token 账单**收下、归一、存住**
==========================================================================

## 这一版解决什么（一句话）

> 网关早就把上游的 `usage` 从流里**捡出来**了 —— 然后**只塞进 SSE 帧就没了**。
> 于是我们**看不到 token 消耗，更看不到缓存命中**。

账单是**上游自己报的**，它一直躺在每次响应的最后一帧里，我们只是没接。
这一层就是把那个接住的动作补上。

## 🔴 四条纪律（每一条都对应一种"账本说谎"的坏法）

### ① 「捡不到」必须留痕 —— `ok=0` 那一行

如果"上游没给账单"就干脆不写行，那么**缺口永远查不出来**：
账本里全是干净数据，看起来一切正常，实际上一半调用没记账。
⇒ 所以：**没账单也写一行**，`ok=0` + `note` 写明为什么。
`summary()` 里 `coverage` 就是"多少比例真有账单"，`notes` 是缺口的原因分布。

### ② `None` ≠ `0`

- `0` = 上游明说"就是 0"（很硬的证据：缓存真的没命中）
- `NULL` = 上游**没说**（什么也证明不了）

这两件事一旦混起来，命中率就再也算不准了 —— 而"看不出缓存有没有工作"
正是这一次要解决的问题本身。⇒ 归一只挑认识的键，**捡不到就是 `None`**。

### ③ 原始 JSON 一个字段不丢（`raw` 列）

归一化只挑我们认识的 5 个数。但中转站随时可能多给一个计费维度
（推理 token / 音频 / 缓存分级 5m-1h…）——
只存挑出来的数 = 那个维度**永久丢失**（而且当时看不出丢了）。
⇒ **先原样收下**，解读留到读的时候做。

### ④ 命中率的**分母**不能猜

两家的口径**根本不同**：

| 形状 | `cache_read` 的位置 | 分母 |
|---|---|---|
| OpenAI / DeepSeek / Gemini | 是 `prompt_tokens` 的**子集**（已含在内） | `prompt_tokens` |
| Anthropic | 与 `input_tokens` **并列**（不含在内） | `input + cache_read + cache_write` |
| **`openai+anthropic`（双命名）** | **不知道**（见 ⑤） | 三个都加（保守） |

拿同一个分母套两家 → 一家偏高一家偏低。⇒ 落库时记 `cache_in_prompt`（口径开关），
命中率**按口径分行求和**，不靠猜、也不用"看起来差不多"。

### ⑤ 一份账单可能**同时**有两套字段名 —— 而且其中一套是坏的（2026-09-23 实测）

上线第一天，线上账本第一笔真实数据就长这样（`recent?raw=1` 抄回来的原文）：

```json
{"prompt_tokens": 4479, "completion_tokens": 450, "total_tokens": 4929,
 "usage_semantic": "openai", "usage_source": "anthropic",
 "input_tokens": 4479, "output_tokens": 0, "input_tokens_details": null}
```

中转站自己**归一过一遍**（它还给这两个字段起了名字：语义 openai / 来源 anthropic），
又把**原始那一套一起带了回来**。于是：

- OpenAI 三件套**自己算得平**：`4479 + 450 = 4929` ✓
- Anthropic 那一对**算不平**：`4479 + 0 = 4479 ≠ 4929` ✗
- 🔴 为什么坏：流式下 `message_start` 先到、那一帧的 `output_tokens` 还是**上游初值 0**，
  真正的输出在后面的 `message_delta` 里。站子把两帧并进同一个对象时，
  **先到的那个 0 把后来的 450 顶掉了**。

🔴 而当时的 `shape_of()` 是"**认出是哪一家**"的单值判断，看见 `input_tokens` 就先返
`"anthropic"` → 于是每次都去读**那套坏的** → **输出 token 全部记成 0**
（而 `total_tokens` 又是对的，所以账面看上去很正常 —— 典型的"账本说谎"）。

⇒ 判据从"**名字叫什么**"改成"**哪一套自己算得平**"（`_read_family()`），
   并在形状里如实点名 `openai+anthropic`；口径**不猜**，留 `None`。

## 🔴 这一版**不算钱**（有意不做）

只交 token 与命中率 —— 它们是**不会过期的事实**。
"花了多少钱"要靠一张单价表，而**单价表会漂**（站子改价、换模型、缓存价与普通价不同档）。
把会过期的东西写进代码 = 埋一颗将来必定骗人的真相。
⇒ 算钱属于"有了稳定单价来源之后"的下一站，本文件不猜单价、不写死单价。

## 🔴 不挂 MCP 门 / 没有 HTTP 写入口

- **不挂 MCP 门**：这是我的账本，不是给他的记忆。跟 `memories` 同一条边界。
- **没有写路由**：账只有**网关内联**这条唯一的写入口（`record()` 由 `llm_routes` 调）。
  开一个 `POST /usage` = 谁能发请求谁就能伪造账单 ⇒ **结构性地不允许**。

## 红线

- 只碰自己的表 `usage_log`（由 `schema.py` v5 建）。
- **一次都不碰 `messages`**，也不碰别的表。
- `record()` **fail-open**：记账出任何问题都吞掉 —— 记账是"更好用"，
  不是"能不能说话"的前提（跟 ⑧/⑪ 同一个原则）。
- 启动日志 GBK 安全（`summary_line()` 不许带 🔴/✅/⚠️）。
"""

import json
from datetime import datetime, timezone

from . import schema as _schema

#: 归一化之后的键（也是 `usage_log` 里那几列）。
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "cache_read_tokens",
               "cache_write_tokens", "total_tokens")

#: 一行账的默认形状（`normalize()` 的底盘）。
_EMPTY = {k: None for k in _TOKEN_KEYS}
_EMPTY.update({"shape": "unknown", "cache_in_prompt": None})

#: 🔴 中转站把**两套命名一起**发回来时的形状名（见文件头 ⑤）。
#: 它不是"第四家" —— 它是一份**混了两家字段名**的账单，所以口径单独处理。
HYBRID_SHAPE = "openai+anthropic"

#: 账本按哪个时区切"天"。Lily 在 UTC+8 —— 按 UTC 切会把晚上 8 点后的算进次日，
#: 那样"今天花了多少"永远对不上她看到的时间。写死一个偏移量就够（单人形态）。
_TZ_OFFSET = "+8 hours"
TZ_LABEL = "UTC+8"


def summary_line() -> str:
    return ("usage 记账就绪 · usage_log 表（每次真实上游调用一行，捡不到也留痕）"
            " · 三个只读端点：summary / recent / status · 无写入口、不挂 MCP 门")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
# 归一化：四家形状 → 统一 5 个数
# ══════════════════════════════════════════════════════════════════════════

def _dig(src, path: str):
    """按 `a.b.c` 取值。任一层不是 dict / 取不到 → None（不抛）。"""
    cur = src
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _pick_int(src, *paths):
    """按顺序找第一个**是数字**的值。找不到 → None。

    🔴 有意排除 `bool`（`True` 是 `int` 的子类，`int(True) == 1`）——
       上游要是哪天回了个 `"cached_tokens": false`，我们不该把它记成"1 个 token"。
    🔴 字符串数字只在**纯数字**时收（`"123"` 收，`"1.2k"` 不收 —— 那是猜）。
    """
    for p in paths:
        v = _dig(src, p)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return int(v)
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            if s.lstrip("-").isdigit():
                return int(s)
    return None


def _families(raw) -> set:
    """这份 usage 里**同时出现了哪几家的字段名**（可能不止一家）。

    🔴 之所以要"数几家"而不是"认出是哪家"：实测中转站会把 OpenAI 三件套和
       Anthropic 原始字段**一起**发回来（见文件头 ⑤）—— "认出一家就返回"
       的单值判断会在这时**挑错**，而且挑中的恰好是坏的那套。
    """
    fams = set()
    if ("prompt_tokens" in raw or "completion_tokens" in raw
            or "total_tokens" in raw or "prompt_tokens_details" in raw):
        fams.add("openai")
    if ("input_tokens" in raw or "output_tokens" in raw
            or "cache_creation_input_tokens" in raw
            or "cache_read_input_tokens" in raw):
        fams.add("anthropic")
    if ("promptTokenCount" in raw or "candidatesTokenCount" in raw
            or "totalTokenCount" in raw or "cachedContentTokenCount" in raw):
        fams.add("gemini")
    if "prompt_cache_hit_tokens" in raw or "prompt_cache_miss_tokens" in raw:
        fams.add("deepseek")
    return fams


def _family_sums_up(raw, fam: str) -> bool:
    """这一套字段**自己算得平**吗（输入 + 输出 == 总数）？

    🔴 这是双命名时唯一站得住的判据 —— **不看名字，只看算不算得平**。
       算不平的那一套不是"略有出入"，而是**明摆着是坏的**（实测里它差的是
       整整一半的输出，而总数是对的，从外面看不出来）。
    """
    t = _pick_int(raw, "total_tokens", "totalTokenCount")
    if t is None:
        return False
    if fam == "openai":
        p, c = _pick_int(raw, "prompt_tokens"), _pick_int(raw, "completion_tokens")
    elif fam == "anthropic":
        p, c = _pick_int(raw, "input_tokens"), _pick_int(raw, "output_tokens")
    else:
        return False
    return p is not None and c is not None and p + c == t


def _read_family(raw) -> str:
    """双命名时**该读哪一套**。返回 `"openai"` / `"anthropic"`；不适用 → `""`。"""
    fams = _families(raw)
    if not ("openai" in fams and "anthropic" in fams):
        return ""
    if _family_sums_up(raw, "openai"):
        return "openai"
    if _family_sums_up(raw, "anthropic"):
        return "anthropic"
    # 两套都算不平 → 仍读 OpenAI 那套：它自带 `total_tokens`，
    # 至少"这一轮总共花了多少"是对的（挑一套读，但不假装它有可信度）。
    return "openai"


def shape_of(raw) -> str:
    """判断这份 usage 是哪家的形状。认不出 → `"unknown"`。

    顺序有讲究：**先判特征键，再判通用键**。
    DeepSeek 也是 OpenAI 兼容形状，但它有独占的 `prompt_cache_hit_tokens` ——
    先认出它，才能把"缓存命中"映射对（这是本次的目标，不能含糊）。

    🆕 2026-09-23：多出一种 `"openai+anthropic"`（**双命名**，见文件头 ⑤）。
    """
    if not isinstance(raw, dict) or not raw:
        return "unknown"
    fams = _families(raw)
    if "openai" in fams and "anthropic" in fams:
        # 双命名：读哪一套由 `_read_family` 定（判据 = 哪一套算得平）。
        # 读的是 Anthropic 那套 → 形状就如实写 anthropic（字段名与读法一致）。
        return HYBRID_SHAPE if _read_family(raw) == "openai" else "anthropic"
    if "anthropic" in fams:
        return "anthropic"
    if "gemini" in fams:
        return "gemini"
    if "deepseek" in fams:
        return "deepseek"
    if "openai" in fams:
        return "openai"
    return "unknown"


def normalize(raw) -> dict:
    """把上游 `usage` 归一成统一形状。**捡不到 = None，不是 0**（见文件头 ②）。

    返回：`{"shape", "prompt_tokens", "completion_tokens", "cache_read_tokens",
            "cache_write_tokens", "total_tokens", "cache_in_prompt"}`

    `total_tokens` **只在"上游真的给了"时才有值** —— 我们**不替它算**。
    （`prompt + completion` 我们自己也会加，但那是我算的数，不是它的账；
      想加的时候在**读**的那一侧加，这样"是它说的还是我算的"永远分得清。）
    """
    out = dict(_EMPTY)
    if not isinstance(raw, dict) or not raw:
        return out

    sh = shape_of(raw)
    out["shape"] = sh

    if sh == "anthropic":
        out["prompt_tokens"] = _pick_int(raw, "input_tokens")
        out["completion_tokens"] = _pick_int(raw, "output_tokens")
        out["cache_read_tokens"] = _pick_int(raw, "cache_read_input_tokens")
        out["cache_write_tokens"] = _pick_int(raw, "cache_creation_input_tokens")
        out["total_tokens"] = _pick_int(raw, "total_tokens")
        # Anthropic：input / cache_read / cache_write 三者**并列相加**
        out["cache_in_prompt"] = 0

    elif sh == "gemini":
        out["prompt_tokens"] = _pick_int(raw, "promptTokenCount")
        out["completion_tokens"] = _pick_int(raw, "candidatesTokenCount")
        out["cache_read_tokens"] = _pick_int(raw, "cachedContentTokenCount")
        out["cache_write_tokens"] = None            # Gemini 无显式"缓存写入"计费项
        out["total_tokens"] = _pick_int(raw, "totalTokenCount")
        out["cache_in_prompt"] = 1                  # promptTokenCount 含 cached

    else:
        # openai / deepseek / 以及一切"长得像 OpenAI"的中转站
        out["prompt_tokens"] = _pick_int(raw, "prompt_tokens", "input_tokens")
        out["completion_tokens"] = _pick_int(raw, "completion_tokens", "output_tokens")
        out["cache_read_tokens"] = _pick_int(
            raw,
            "prompt_tokens_details.cached_tokens",   # OpenAI 标准位置
            "prompt_cache_hit_tokens",               # DeepSeek
            "cached_tokens",                         # 有些站摊平放
            "cache_read_input_tokens",               # 少数中转站混用 anthropic 命名
        )
        out["cache_write_tokens"] = _pick_int(
            raw, "cache_creation_tokens", "cache_write_tokens",
            "cache_creation_input_tokens",
        )
        out["total_tokens"] = _pick_int(raw, "total_tokens")
        # 🔴 双命名（`openai+anthropic`）时**口径不明** → `None`，不猜（见文件头 ④⑤）：
        #    站子两套名字都用了，我们**没有证据**判断它报的 cache_read（如果将来有）
        #    算不算在 prompt_tokens 里 —— Anthropic 语义下不算（并列），
        #    OpenAI 语义下算（子集）。猜一个 = 拿命中率骗自己。
        #    读数侧（`summary()`）对口径不明的行**保守取和**，不按子集处理。
        # 🔴 同一个道理，`unknown`（压根没认出来）也**不许**声称口径 ——
        #    这条之前漏了：认不出形状却写 `1`，等于凭空担保"cache 含在 prompt 里"。
        out["cache_in_prompt"] = 1 if sh in ("openai", "deepseek") else None

    # 🔴 `prompt_cache_miss_tokens`（DeepSeek）**故意不映射成 cache_write**：
    #    它说的是"没命中的那部分输入"，不是"写进缓存的量"。混起来会凭空
    #    造出一笔"缓存写入费" —— 那是伪造。它留在 `raw` 里，一个字段没丢。

    return out


def usable(norm: dict) -> bool:
    """归一之后**至少拿到了一个数**才算"这份账单有用"。

    只认出了形状但一个数都没有（比如 `{"shape":"openai"}`）→ 不算有账单：
    它进了库也回答不了任何问题，反而会把 `coverage` 撑高、把缺口盖住。
    """
    if not isinstance(norm, dict):
        return False
    return any(norm.get(k) is not None for k in _TOKEN_KEYS)


# ══════════════════════════════════════════════════════════════════════════
# 写：唯一入口
# ══════════════════════════════════════════════════════════════════════════

def record(relay, *, provider_id=None, model=None, session_id=None, route="chat",
           stream=True, usage=None, ok=None, note=None, ms=None, chars_out=None,
           user_id="u_owner") -> dict:
    """写一行账。**fail-open：出任何问题都吞掉，绝不向上抛。**

    参数：
        usage   上游回来的那份 dict（原样传进来，`raw` 会存原文）
        ok      显式指定"这次有没有账单"。默认 = `usage` 归一后是否可用。
                **被停止 / 上游报错**这类"掐断、肯定有消耗但拿不到账单"的情形，
                调用方应显式传 `ok=False` + `note="stopped"` → 缺口可见。
        note    `ok=False` 时的原因（`stopped` / `upstream_error` / `no_usage_in_payload`）

    返回：`{"ok": True/False, ...}` —— 给测试与诊断用，**调用方不需要看**。
    """
    try:
        norm = normalize(usage)
        have = usable(norm)
        if ok is None:
            ok = have
        # 🔴 `ok=0` 的行**必须带理由**：`summary().gaps` 是按 `note` 分组的，
        #    留一个 NULL 在里面，"缺口原因"就有一条读不出所以然。
        #    没给特定理由 = 上游正常结束但没给账单 —— 这本身就是最常见的那种。
        if not ok and not note:
            note = "no_usage_in_payload"
        row = (
            now_iso(),
            user_id,
            (provider_id or None),
            (model or None),
            (session_id or None),
            (route or None),
            1 if stream else 0,
            norm.get("shape") or "unknown",
            1 if ok else 0,
            (note or None),
            norm.get("prompt_tokens"),
            norm.get("completion_tokens"),
            norm.get("cache_read_tokens"),
            norm.get("cache_write_tokens"),
            norm.get("cache_in_prompt"),
            norm.get("total_tokens"),
            (int(ms) if isinstance(ms, (int, float)) else None),
            (int(chars_out) if isinstance(chars_out, (int, float)) else None),
            (json.dumps(usage, ensure_ascii=False) if isinstance(usage, dict) and usage else None),
        )
        with _schema.connect(relay) as conn:
            conn.execute(
                "INSERT INTO usage_log "
                "(ts, user_id, provider_id, model, session_id, route, stream, shape, ok, note, "
                " prompt_tokens, completion_tokens, cache_read_tokens, cache_write_tokens, "
                " cache_in_prompt, total_tokens, ms, chars_out, raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            conn.commit()
        return {"ok": True, "recorded": True, "billed": bool(ok),
                "shape": norm.get("shape"), "cache_read": norm.get("cache_read_tokens")}
    except Exception as e:                      # 🔴 有意兜住全部（见文件头）
        return {"ok": False, "recorded": False, "reason": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════════════════
# 读：明细 / 聚合 / 体检
# ══════════════════════════════════════════════════════════════════════════

#: 对外投影 —— 白名单式（新加列必须显式列出来，默认不外泄）。
def public(row: dict) -> dict:
    """一行账给界面看的形状。`raw` **默认不出去**（可能很长 / 含上游内部字段）。

    想看原文用 `?raw=1`（`recent()` 里显式开关）—— 默认不外泄，
    要看的时候得说出来，这跟 `salience` 那条是同一个手法。
    """
    return {k: row.get(k) for k in (
        "id", "ts", "provider_id", "model", "session_id", "route", "stream",
        "shape", "ok", "note", "prompt_tokens", "completion_tokens",
        "cache_read_tokens", "cache_write_tokens", "cache_in_prompt",
        "total_tokens", "ms", "chars_out",
    )}


def _rows(relay, sql: str, args: tuple = ()) -> list:
    with _schema.connect(relay) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def recent(relay, limit: int = 50, user_id: str = "u_owner",
           with_raw: bool = False) -> list:
    """最近 N 行（按 id 倒序 = 时间倒序；`id` 是递增主键，比 ts 稳）。"""
    rows = _rows(
        relay,
        "SELECT * FROM usage_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, int(limit)),
    )
    if with_raw:
        return rows
    return [public(r) for r in rows]


def _since_clause(days: int) -> tuple:
    """`days <= 0` = 不限时间（看全部）。"""
    if not days or int(days) <= 0:
        return "", ()
    return " AND ts >= datetime('now', ?)", (f"-{int(days)} days",)


def summary(relay, days: int = 7, user_id: str = "u_owner") -> dict:
    """窗口内的账：调用次数 / 覆盖率 / token 总量 / **缓存命中率** / 分模型 / 分天。

    🔴 命中率的分母**按 `cache_in_prompt` 分口径求和**（见文件头 ④），
       而且**只累计"真有 cache_read 值"的行** ——
       把"上游没报"的行算进分母 = 拿缺口把命中率压低 = 伪造一个更差的成绩。
    🔴 一行都没有 / 全没报 cache_read → `cache_hit_rate = None`（**不是 0**）。
       `0` 会被读成"缓存完全没命中"，而真相是"我们还不知道"。
    """
    where, args = _since_clause(days)

    head = _rows(
        relay,
        "SELECT COUNT(*) AS calls, SUM(ok) AS with_usage, "
        "       SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS without_usage "
        "FROM usage_log WHERE user_id = ?" + where,
        (user_id,) + args,
    )[0]
    calls = int(head["calls"] or 0)
    with_usage = int(head["with_usage"] or 0)

    tot = _rows(
        relay,
        "SELECT SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, "
        "       SUM(cache_read_tokens) AS cr, SUM(cache_write_tokens) AS cw, "
        "       SUM(total_tokens) AS t, "
        "       SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS p_null, "
        "       SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END) AS cr_rows "
        "FROM usage_log WHERE user_id = ?" + where,
        (user_id,) + args,
    )[0]

    # ── 命中率：只对"有 cache_read 值"的行，按口径分别累计 ──────────────────
    hit = _rows(
        relay,
        "SELECT cache_in_prompt AS cip, "
        "       SUM(cache_read_tokens) AS cr, "
        "       SUM(COALESCE(prompt_tokens, 0)) AS p, "
        "       SUM(COALESCE(cache_read_tokens, 0)) AS cr2, "
        "       SUM(COALESCE(cache_write_tokens, 0)) AS cw "
        "FROM usage_log WHERE user_id = ? AND cache_read_tokens IS NOT NULL" + where
        + " GROUP BY cache_in_prompt",
        (user_id,) + args,
    )
    num = 0.0
    den = 0.0
    for r in hit:
        num += float(r["cr"] or 0)
        if r["cip"] == 1:            # cache_read 已含在 prompt 里 → 分母就是 prompt
            den += float(r["p"] or 0)
        elif r["cip"] == 0:          # 三者并列 → 分母要把三块都加上
            den += float(r["p"] or 0) + float(r["cr2"] or 0) + float(r["cw"] or 0)
        else:                        # 口径不明（老行 / unknown 形状）→ 保守取和
            den += float(r["p"] or 0) + float(r["cr2"] or 0) + float(r["cw"] or 0)
    rate = round(num / den, 4) if den > 0 else None

    by_model = [
        {
            "model": r["model"], "calls": int(r["calls"] or 0),
            "prompt_tokens": r["p"], "completion_tokens": r["c"],
            "cache_read_tokens": r["cr"], "total_tokens": r["t"],
        }
        for r in _rows(
            relay,
            "SELECT model, COUNT(*) AS calls, SUM(prompt_tokens) AS p, "
            "       SUM(completion_tokens) AS c, SUM(cache_read_tokens) AS cr, "
            "       SUM(total_tokens) AS t "
            "FROM usage_log WHERE user_id = ?" + where
            + " GROUP BY model ORDER BY COUNT(*) DESC",
            (user_id,) + args,
        )
    ]

    by_day = [
        {
            "day": r["day"], "calls": int(r["calls"] or 0),
            "prompt_tokens": r["p"], "completion_tokens": r["c"],
            "cache_read_tokens": r["cr"],
        }
        for r in _rows(
            relay,
            "SELECT date(ts, ?) AS day, COUNT(*) AS calls, SUM(prompt_tokens) AS p, "
            "       SUM(completion_tokens) AS c, SUM(cache_read_tokens) AS cr "
            "FROM usage_log WHERE user_id = ?" + where
            + " GROUP BY day ORDER BY day DESC",
            (_TZ_OFFSET, user_id) + args,
        )
    ]

    notes = [
        {"note": r["note"], "n": int(r["n"] or 0)}
        for r in _rows(
            relay,
            "SELECT note, COUNT(*) AS n FROM usage_log "
            "WHERE user_id = ? AND ok = 0" + where + " GROUP BY note ORDER BY n DESC",
            (user_id,) + args,
        )
    ]

    return {
        "ok": True,
        "window_days": int(days or 0),
        "tz": TZ_LABEL,
        "calls": calls,
        "calls_with_usage": with_usage,
        "calls_without_usage": calls - with_usage,
        "coverage": (round(with_usage / calls, 4) if calls else None),
        "tokens": {
            "prompt": tot["p"], "completion": tot["c"],
            "cache_read": tot["cr"], "cache_write": tot["cw"],
            "total": tot["t"],
            # 🔴 这两行是"数据有多少缺口"的自白：多少行的这一列上游压根没报。
            "prompt_null_rows": int(tot["p_null"] or 0),
            "cache_read_rows": int(tot["cr_rows"] or 0),
        },
        "cache_hit_rate": rate,
        "cache_hit_basis": (
            "Σcache_read / Σ分母；分母按形状分口径 —— "
            "OpenAI/DeepSeek/Gemini 用 prompt_tokens（cache_read 是其子集），"
            "Anthropic 用 input+cache_read+cache_write（三者并列）；"
            "只累计「上游真的报了 cache_read」的行。null = 数据不足，不是 0。"
        ),
        "by_model": by_model,
        "by_day": by_day,
        "gaps": notes,
        # 🔴 有意不做：见文件头「这一版不算钱」。
        "cost": None,
        "cost_note": "本层只记 token（不会过期的事实）。算钱要一张单价表，"
                     "而单价会漂 —— 等有了稳定的单价来源再接，不在这里猜。",
    }


def status(relay, user_id: str = "u_owner") -> dict:
    """只读体检：账本有多少行、什么时候开始记的、覆盖率的两个极端。

    回答的是"**记账这件事自己在工作吗**" —— 而不是"花了多少"。
    """
    head = _rows(
        relay,
        "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
        "       SUM(ok) AS with_usage "
        "FROM usage_log WHERE user_id = ?",
        (user_id,),
    )[0]
    n = int(head["n"] or 0)
    with_usage = int(head["with_usage"] or 0)
    by_route = [
        {"route": r["route"], "n": int(r["n"] or 0), "with_usage": int(r["u"] or 0)}
        for r in _rows(
            relay,
            "SELECT route, COUNT(*) AS n, SUM(ok) AS u FROM usage_log "
            "WHERE user_id = ? GROUP BY route ORDER BY n DESC",
            (user_id,),
        )
    ]
    by_shape = [
        {"shape": r["shape"], "n": int(r["n"] or 0), "cache_read_rows":
            int(r["cr"] or 0)}
        for r in _rows(
            relay,
            "SELECT shape, COUNT(*) AS n, "
            "       SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END) AS cr "
            "FROM usage_log WHERE user_id = ? GROUP BY shape ORDER BY n DESC",
            (user_id,),
        )
    ]
    return {
        "ok": True,
        "rows": n,
        "calls_with_usage": with_usage,
        "coverage": (round(with_usage / n, 4) if n else None),
        "first_ts": head["first_ts"],
        "last_ts": head["last_ts"],
        "empty": n == 0,
        "by_route": by_route,
        "by_shape": by_shape,
        "tz": TZ_LABEL,
    }
