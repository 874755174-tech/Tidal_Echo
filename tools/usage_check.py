#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
usage 记账验收 —— P2（token 账单落库 / 归一 / 命中率）
==========================================================================

## 这一套到底在守什么

usage 记账看起来只是"把上游的 usage 存进表"，但它最容易出的病**全都不显眼** ——
账面永远是"有数据"，坏的是**数据的意义**：

  · **账本说谎（缺口不可见）**：上游没给账单就干脆不写行 → `coverage` 永远是
    100%，而实际上一半调用没记账。⇒ 本套专验"没账单也写一行 + 理由必填"。
  · **`None` 被写成 `0`**：上游**没说**（未知）与上游**说了 0**（真没命中）
    混成同一个值 → 命中率再也算不准，而"看不出缓存有没有工作"正是本次要解的问题。
  · **命中率分母猜错**：Anthropic 的 `cache_read` 与 `input_tokens` 并列，
    OpenAI 的 `cached_tokens` 是 `prompt_tokens` 的子集 ——
    套同一个分母 → 一家偏高一家偏低，且**看不出来**。⇒ 记 `cache_in_prompt`，分行求和。
  · **空数据伪装成 0 分**：一行都没有 / 全没报 cache_read → 命中率必须是
    `null`（"还不知道"），**不是 `0`**（"完全没命中"）。这两个读起来天差地别。
  · **账单收不到**：OpenAI 兼容的**流式**默认不给 usage，要 `stream_options`
    才给 —— 不索要 = 记账是个永远空转的空壳。
  · **半个账单**：Anthropic 把账单拆两帧发（input / output 各一半），
    覆盖式赋值 → 静默丢一半。
  · **双命名账单**（2026-09-23 线上第一笔账实测）：中转站把 OpenAI 三件套和
    Anthropic 原始字段**一起**发回来，而 Anthropic 那一半在流式下**是坏的**
    （`output_tokens` 恒为 0）。"认出是哪一家"式的单值判断会在这时**挑中坏的那套**
    → **输出 token 全部记成 0**，而 `total_tokens` 又是对的 ⇒ 账面看上去很正常。
    ⇒ 判据改成"**哪一套自己算得平**"，形状如实写 `openai+anthropic`，口径留 `None`。
  · **掐断的调用凭空消失**：停止 / 上游报错 / 手机锁屏 —— 这些都**真的烧了 token**，
    却都走不到"正常结束"那条路。⇒ 四种收尾各自留痕，且**不重复记**。
  · **账本被外部伪造**：开一个 `POST /usage` = 谁能发请求谁就能改账单。
  · **记账把聊天搞挂**：记账是"更好用"，不是"能不能说话"的前提 → 必须 fail-open。

## 手法

  A 组 纯逻辑（归一化），不碰库
  B 组 真写库（临时 SQLite），**含"users 表空着也能记账"**
  C 组 聚合 / 命中率（**手算期望值**对账）
  D 组 端点（进程内 ASGI TestClient，**不起端口**）+ 红线 + 接线
  E 组 出站请求真跑 `providers.adapt`（纯函数，不发 HTTP）
  F 组 源码扫描（守"合并而非覆盖"与四处记账调用点这类**结构性**约束）
  G 组 **真样本回归** —— 线上账本抄回来的原文，钉死这个坑

用法：.venv\Scripts\python.exe tools\usage_check.py
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import warnings
from pathlib import Path

# 🔴 本机 PowerShell 5.1 管道下 sys.stdout.encoding = cp936(gbk)，而验收名里有
#    🔴/✅/⚠️ 这类 GBK 编不出的字符 -> print 到一半 UnicodeEncodeError，整套会
#    **半路死掉**（看起来像"验收挂了"，其实是没跑完）。
#    errors="replace" 只把编不出的字符降级成 "?"，中文和结论一个字不动。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

warnings.filterwarnings("ignore")     # fastapi TestClient 的 httpx 弃用警告

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(DEPLOY))

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

SECRET = "test-secret-usage-0123456789"

results: list = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sect(title: str) -> None:
    print("")
    print("-" * 66)
    print(title)
    print("-" * 66)


# ══════════════════════════════════════════════════════════════════════════
# 替身：最小 relay + 进程内 ASGI 客户端（不起端口）
# ══════════════════════════════════════════════════════════════════════════

MESSAGES_DDL = """
    CREATE TABLE IF NOT EXISTS messages (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        TEXT NOT NULL,
        direction TEXT NOT NULL,
        kind      TEXT NOT NULL,
        text      TEXT NOT NULL,
        meta      TEXT NOT NULL DEFAULT '{}'
    )
"""


class FakeRelay:
    def __init__(self, db_path: str):
        from fastapi import FastAPI
        self.DB_PATH = db_path
        self.SECRET = SECRET
        self.app = FastAPI()

    def check_auth(self, request):
        from fastapi import HTTPException
        import hmac
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else ""
        if not tok:
            tok = request.query_params.get("token", "")
        if not tok or not hmac.compare_digest(tok, SECRET):
            raise HTTPException(status_code=401, detail="unauthorized")


def install_usage(relay):
    """挂 usage 端点。**每次重置 `_INSTALLED`** —— 它是模块级单例守卫，
    而本套要往**多个** app 上分别挂（每个用例一个 app）。"""
    from app_ext import usage as U
    U._INSTALLED = False
    U.install(relay)
    return U


def fresh_db(tmp: str, name: str, seed_owner: bool = False) -> FakeRelay:
    """建一个新库（走到 `ensure_schema`）。

    `seed_owner=False` 时**故意不播种房主** —— 用来验"users 表空着，账照样记得进"。
    """
    from app_ext import schema as S, identity as I
    relay = FakeRelay(os.path.join(tmp, name + ".db"))
    S.ensure_schema(relay)
    if seed_owner:
        I.ensure_owner(relay)
    return relay


def make_old_v4_db(tmp: str) -> FakeRelay:
    """真造一个 **v4 老库**：五张表里**没有** `usage_log`，另带一条老记忆 + 一条消息。

    v4 的定义：settings 有 provider_id、sessions 有 summary_upto、
    memories 有 source、**没有 usage_log**。前四张表用现在的 DDL 就是 v4 形状。
    老库升级必须**只多一张表**，一行老数据都不许动 —— 这才是这条验收要守的。
    """
    from app_ext import schema as S
    db = os.path.join(tmp, "old_v4.db")
    conn = sqlite3.connect(db)
    tables = dict(S.DDL_TABLES)
    for name in ("users", "settings", "sessions", "memories"):
        conn.execute(tables[name])
    conn.execute(MESSAGES_DDL)
    conn.execute("INSERT INTO users (id, handle, display_name, secret_hash, created) "
                 "VALUES ('u_owner','owner','Lily','x','2026-09-01T00:00:00+00:00')")
    conn.execute("INSERT INTO memories "
                 "(id, user_id, kind, text, source, salience, created, last_used) "
                 "VALUES ('m_legacy','u_owner','fact','升级之前就存在的一条记忆',"
                 "'chat',0.88,'2026-09-10T00:00:00+00:00',NULL)")
    conn.execute("INSERT INTO messages (ts, direction, kind, text) VALUES "
                 "('2026-09-10T00:00:00+08:00','in','user','老消息')")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    return FakeRelay(db)


# ── 四家形状的样本（真实字段名）────────────────────────────────────────────
OPENAI_U = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 800}}
ANTHROPIC_U = {"input_tokens": 100, "output_tokens": 50,
               "cache_read_input_tokens": 900, "cache_creation_input_tokens": 30}
DEEPSEEK_U = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600,
              "prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 100}
GEMINI_U = {"promptTokenCount": 700, "candidatesTokenCount": 80,
            "totalTokenCount": 780, "cachedContentTokenCount": 600}

# ── 真样本（2026-09-23 从**线上账本** `recent?raw=1` 抄回来的原文，一个字符没改）──
#    🔴 这一份进验收的意义：它是**真实的、我们没想到过的**形状。
#       中转站（哈基米，relay 槽）把 OpenAI 三件套和 Anthropic 原始字段一起发回来，
#       而 Anthropic 那一半在流式下是坏的：
#         · `output_tokens: 0` —— 那是上游初值，真正的输出（450）在后面的
#           `message_delta` 里，被先到的 0 顶掉了
#         · `prompt_tokens` / `completion_tokens` / `total_tokens` 是站子自己归一的，
#           **自己算得平**（4479 + 450 = 4929）
#       当时 `shape_of()` 看见 `input_tokens` 就先返 `anthropic` → 读坏的那套
#       → **输出 token 全记 0**。把它钉在这里，同一个坑不踩第二次。
REAL_RELAY_U = {
    "prompt_tokens": 4479, "completion_tokens": 450, "total_tokens": 4929,
    "usage_semantic": "openai", "usage_source": "anthropic",
    "prompt_tokens_details": {"text_tokens": 0, "audio_tokens": 0, "image_tokens": 0},
    "completion_tokens_details": {"text_tokens": 0, "audio_tokens": 0,
                                  "image_tokens": 0, "reasoning_tokens": 0},
    "input_tokens": 4479, "output_tokens": 0, "input_tokens_details": None,
}
# 反向：双命名，但**平的是 Anthropic 那套** → 该读 Anthropic（判据是"算得平"）。
REAL_ANTHROPIC_WINS = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                       "prompt_tokens": 999, "completion_tokens": 999}
# 两套都算不平 → 仍读 OpenAI 那套（它自带 total_tokens）。
REAL_NEITHER = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 999,
                "input_tokens": 1, "output_tokens": 1}
# 双命名**且带缓存** → 口径不明时读数侧要保守取和（不按"子集"乐观处理）。
HYBRID_WITH_CACHE = {"prompt_tokens": 1000, "completion_tokens": 100,
                     "total_tokens": 1100, "input_tokens": 900,
                     "output_tokens": 100, "cache_read_input_tokens": 900,
                     "cache_creation_input_tokens": 30}


def run() -> str:
    from app_ext import schema as S, usage_store as U

    tmp = tempfile.mkdtemp(prefix="usage_check_")

    # ══════════════════════════════════════════════════════════════════════
    # A. 归一化（纯逻辑，不碰库）
    # ══════════════════════════════════════════════════════════════════════
    sect("A. 归一化：四家形状 → 统一 5 个数")

    n = U.normalize(OPENAI_U)
    chk("A1 OpenAI：prompt/completion 就位",
        n["prompt_tokens"] == 1000 and n["completion_tokens"] == 200, str(n))
    chk("A2 OpenAI：cache_read 从 prompt_tokens_details.cached_tokens 取到",
        n["cache_read_tokens"] == 800 and n["shape"] == "openai", str(n))
    chk("A3 🔴 OpenAI：cache_in_prompt=1（cached 是 prompt 的子集 → 分母用 prompt）",
        n["cache_in_prompt"] == 1, str(n))

    n = U.normalize(ANTHROPIC_U)
    chk("A4 Anthropic：input_tokens → prompt_tokens",
        n["prompt_tokens"] == 100 and n["shape"] == "anthropic", str(n))
    chk("A5 Anthropic：cache_creation_input_tokens → cache_write_tokens",
        n["cache_write_tokens"] == 30, str(n))
    chk("A6 🔴 Anthropic：cache_in_prompt=0（三者并列 → 分母要相加）",
        n["cache_in_prompt"] == 0 and n["cache_read_tokens"] == 900, str(n))

    n = U.normalize(DEEPSEEK_U)
    chk("A7 DeepSeek：prompt_cache_hit_tokens → cache_read_tokens",
        n["cache_read_tokens"] == 400 and n["shape"] == "deepseek", str(n))
    chk("A8 🔴 DeepSeek：prompt_cache_miss_tokens **不许**被当成 cache_write",
        n["cache_write_tokens"] is None, str(n))

    n = U.normalize(GEMINI_U)
    chk("A9 Gemini：promptTokenCount/candidatesTokenCount 就位",
        n["prompt_tokens"] == 700 and n["completion_tokens"] == 80, str(n))
    chk("A10 Gemini：cachedContentTokenCount → cache_read（分母含缓存）",
        n["cache_read_tokens"] == 600 and n["cache_in_prompt"] == 1, str(n))

    chk("A11 🔴 捡不到 = None，**不是 0**（0 与'没说'必须分开）",
        U.normalize({"prompt_tokens": 5})["cache_read_tokens"] is None
        and U.normalize({"prompt_tokens": 5})["completion_tokens"] is None, "")
    chk("A12 🔴 total_tokens **只在'上游给了'时才有**（我们不替它算）",
        U.normalize({"prompt_tokens": 10, "completion_tokens": 2})["total_tokens"] is None
        and U.normalize({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})
        ["total_tokens"] == 12, "")

    chk("A13 🔴 bool 不算数（`True` 是 int 子类，不能变成 1 个 token）",
        U.normalize({"prompt_tokens": True})["prompt_tokens"] is None, "")
    chk("A14 纯数字字符串收（'123' → 123）；带单位的不猜（'1.2k' → None）",
        U.normalize({"prompt_tokens": "123"})["prompt_tokens"] == 123
        and U.normalize({"prompt_tokens": "1.2k"})["prompt_tokens"] is None, "")

    chk("A15 空 / None / 垃圾 → shape=unknown 且一个数都没有",
        U.normalize(None)["shape"] == "unknown" and U.normalize({})["shape"] == "unknown"
        and U.normalize({"foo": 1})["shape"] == "unknown", "")
    chk("A16 只认出形状、一个数都没有 → usable=False（不许撑高 coverage）",
        not U.usable(U.normalize({"foo": 1}))
        and U.usable(U.normalize({"prompt_tokens": 1})), "")
    chk("A17 嵌在 message.usage 里的（部分中转站）也能归一",
        U.normalize({"input_tokens": 7, "output_tokens": 1})["prompt_tokens"] == 7, "")

    # ══════════════════════════════════════════════════════════════════════
    # B. 记账（真写库）
    # ══════════════════════════════════════════════════════════════════════
    sect("B. 记账：真升级老库 / 写一行 / fail-open / 缺口必带理由 / 无外键")

    # ── 先验老库升级（这件事必须在"新库"之前单独做）────────────────────────
    old = make_old_v4_db(tmp)
    before_msg = sqlite3.connect(old.DB_PATH).execute(
        "SELECT COUNT(*) FROM messages").fetchone()[0]
    rep = S.ensure_schema(old)
    chk("B0 🔴 老库（v4）升上来：**只多一张 usage_log**，版本推到 5",
        rep["created"] == ["usage_log"] and rep["version"] == 5, str(rep))

    conn = sqlite3.connect(old.DB_PATH)
    mem_n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    leg = conn.execute("SELECT text, source FROM memories WHERE id='m_legacy'").fetchone()
    msg_n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    msg_txt = conn.execute("SELECT text FROM messages").fetchone()[0]
    conn.close()
    chk("B0b 🔴 升级一行老数据都没动（记忆还在、正文未改、消息行数与内容不变）",
        mem_n == 1 and leg == ("升级之前就存在的一条记忆", "chat")
        and msg_n == before_msg == 1 and msg_txt == "老消息",
        f"mem={mem_n} leg={leg} msg={msg_n}/{before_msg} txt={msg_txt}")
    chk("B0c 幂等：再跑一次不新建任何表",
        S.ensure_schema(old)["created"] == [], "")

    relay = fresh_db(tmp, "main", seed_owner=True)
    out = S.ensure_schema(relay)
    chk("B0d 新库：版本 = 5，五张表齐（含 usage_log）",
        out["version"] == 5
        and all(S.schema_report(relay)["tables_present"].values()), "")

    r = U.record(relay, provider_id="relay", model="claude-opus-4-6", route="chat",
                 stream=True, usage=OPENAI_U, ms=1234, chars_out=321, session_id="s-1")
    chk("B1 🔴 record 真的写了一行（且判定为有账单）",
        r.get("ok") and r.get("billed") is True and r.get("shape") == "openai", str(r))

    rows = U.recent(relay, 5, with_raw=True)
    chk("B2 读回来的字段与写入一致（model / tokens / ms / chars_out / session）",
        rows[0]["model"] == "claude-opus-4-6" and rows[0]["prompt_tokens"] == 1000
        and rows[0]["ms"] == 1234 and rows[0]["chars_out"] == 321
        and rows[0]["session_id"] == "s-1", str(rows[0]))
    chk("B3 🔴 raw 存了上游原文（一个字段不丢：嵌套的 details 也在）",
        json.loads(rows[0]["raw"])["prompt_tokens_details"]["cached_tokens"] == 800, "")

    r = U.record(relay, provider_id="relay", model="m", route="chat", stream=True,
                 usage={}, session_id="s-2")
    chk("B4 🔴 空 usage → ok=0（不是不写行）+ 自动补理由",
        r.get("ok") and r.get("billed") is False, str(r))

    r = U.record(relay, provider_id="relay", model="m", route="chat", stream=True,
                 ok=False, note="stopped", session_id="s-3")
    chk("B5 掐断（stopped）也写一行 —— 缺口**看得见**", r.get("ok") is True, str(r))

    r = U.record(relay, provider_id="relay", model="m", route="chat", stream=True,
                 ok=False, note=None)
    chk("B6 🔴 ok=0 且没给理由 → 自动落 `no_usage_in_payload`（不留 NULL）",
        r.get("ok") is True, str(r))
    # 🔴 只看 **ok=0 的行** 的 note：ok=1 的行本来就没有"缺口理由"（它是 None，正确）。
    #    第一版断言没带这个过滤，于是把"正常有账单"也当成了"缺口没写理由"（假红）。
    gap_notes = {x["note"] for x in U.recent(relay, 50) if not x["ok"]}
    chk("B7 🔴 所有 ok=0 的行**都带理由**（没有 None —— gaps 分组才读得出来）",
        None not in gap_notes and {"stopped", "no_usage_in_payload"} <= gap_notes,
        str(sorted(gap_notes, key=str)))

    # fail-open：把 DB_PATH 指到一个不可能写的位置
    class Broken:
        DB_PATH = os.path.join(tmp, "no_such_dir", "deep", "x.db")
    rb = Broken()
    r = U.record(rb, provider_id="relay", model="m", usage=OPENAI_U)
    chk("B8 🔴 record 是 fail-open：写不进去也**不抛**，只回 ok=False",
        r.get("ok") is False and "recorded" in r, str(r))

    chk("B9 非法参数（ms 是字符串）不炸，落 NULL",
        U.record(relay, model="m", usage=OPENAI_U, ms="abc").get("ok") is True, "")

    # 🔴 无外键：users 表空着照样记
    relay_noowner = fresh_db(tmp, "noowner", seed_owner=False)
    r = U.record(relay_noowner, model="m", usage=OPENAI_U)
    chk("B10 🔴🔴 users 表**空着**也能记账（外键会让账整批静默丢失）",
        r.get("ok") and U.status(relay_noowner)["rows"] == 1, str(r))

    # recent 的投影：默认不外泄 raw
    chk("B11 🔴 recent 默认**不含 raw**（要看原文得显式 ?raw=1）",
        "raw" not in U.recent(relay, 1)[0]
        and "raw" in U.recent(relay, 1, with_raw=True)[0], "")

    # ══════════════════════════════════════════════════════════════════════
    # C. 聚合 / 命中率（手算对账）
    # ══════════════════════════════════════════════════════════════════════
    sect("C. 聚合与命中率：口径分行、缺口不进分母、空数据 = null")

    relayC = fresh_db(tmp, "agg", seed_owner=True)
    U.record(relayC, model="a", route="chat", stream=True, usage=OPENAI_U)
    U.record(relayC, model="a", route="chat", stream=True, usage=ANTHROPIC_U)
    U.record(relayC, model="b", route="complete", stream=False, usage=DEEPSEEK_U)
    U.record(relayC, model="b", route="chat", stream=True, ok=False, note="stopped")
    U.record(relayC, model="b", route="chat", stream=True, ok=False,
             note="client_disconnect")
    s = U.summary(relayC, days=0)

    chk("C1 calls / calls_with_usage / coverage 三项对",
        s["calls"] == 5 and s["calls_with_usage"] == 3
        and abs(s["coverage"] - 0.6) < 1e-9, str({k: s[k] for k in
                                                  ("calls", "calls_with_usage", "coverage")}))

    # 手算命中率：
    #   openai     : 800 / 1000                    (cache_in_prompt=1)
    #   anthropic  : 900 / (100 + 900 + 30)        (cache_in_prompt=0 → 三项相加)
    #   deepseek   : 400 / 500                     (cache_in_prompt=1)
    #   掐断那两行 cache_read 是 NULL → **不进分子也不进分母**
    exp_num = 800 + 900 + 400
    exp_den = 1000 + (100 + 900 + 30) + 500
    exp_rate = round(exp_num / exp_den, 4)
    chk("C2 🔴 命中率按口径分行求和（手算对账 2100/2530）",
        s["cache_hit_rate"] == exp_rate,
        f"实际 {s['cache_hit_rate']} 期望 {exp_rate}（{exp_num}/{exp_den}）")

    # 反向验"分口径"这件事**有没有区分力**：若把 Anthropic 也当 OpenAI 口径
    # （分母只用 prompt 那 100），命中率会算成 1.31 —— **超过 1**，一眼就知道错。
    # 这正是"两家的 cache_read 位置不同"必须分开处理的证明。
    wrong_rate = round(exp_num / (1000 + 100 + 500), 4)
    chk("C3 🔴 分口径有区分力：混用口径会算出 >1 的命中率（1.3125）",
        wrong_rate > 1.0 and wrong_rate != exp_rate, f"{wrong_rate} vs {exp_rate}")

    chk("C4 token 总量（prompt/completion/cache_read）按行相加",
        s["tokens"]["prompt"] == 1000 + 100 + 500
        and s["tokens"]["completion"] == 200 + 50 + 100
        and s["tokens"]["cache_read"] == 800 + 900 + 400, str(s["tokens"]))
    chk("C5 自白字段：多少行的 prompt / cache_read 上游压根没报",
        s["tokens"]["prompt_null_rows"] == 2
        and s["tokens"]["cache_read_rows"] == 3, str(s["tokens"]))

    chk("C6 gaps 按 note 分组（stopped / client_disconnect 各 1）",
        {(g["note"], g["n"]) for g in s["gaps"]} == {("stopped", 1),
                                                    ("client_disconnect", 1)}, str(s["gaps"]))
    chk("C7 by_model 分组：两个模型，调用多的（b=3）排前面",
        len(s["by_model"]) == 2 and s["by_model"][0]["calls"] == 3, str(s["by_model"]))
    chk("C8 by_day 按 UTC+8 切天（就是今天）",
        s["by_day"] and len(s["by_day"][0]["day"]) == 10 and s["tz"] == "UTC+8",
        str(s["by_day"]))
    chk("C9 🔴 cost 是 None + 说明（本层不算钱：单价会漂，不在这里猜）",
        s["cost"] is None and "单价" in (s["cost_note"] or ""), "")

    # 空库 → None（不是 0）
    empty = fresh_db(tmp, "empty", seed_owner=True)
    s0 = U.summary(empty, days=7)
    chk("C10 🔴🔴 空库：命中率 = None（**不是 0** —— 0 会被读成'缓存完全没命中'）",
        s0["cache_hit_rate"] is None and s0["coverage"] is None and s0["calls"] == 0,
        str(s0))

    # 有账单但上游一律没报 cache → None
    nocache = fresh_db(tmp, "nocache", seed_owner=True)
    U.record(nocache, model="m", usage={"prompt_tokens": 10, "completion_tokens": 2})
    s1 = U.summary(nocache, days=0)
    chk("C11 🔴 上游从没报过 cache_read → 命中率仍是 None（数据不足 ≠ 0 分）",
        s1["cache_hit_rate"] is None and s1["tokens"]["prompt"] == 10, str(s1))

    # 真·0 命中 → 0.0（与 None 严格区分）
    zero = fresh_db(tmp, "zero", seed_owner=True)
    U.record(zero, model="m", usage={"prompt_tokens": 100, "completion_tokens": 5,
                                     "prompt_tokens_details": {"cached_tokens": 0}})
    s2 = U.summary(zero, days=0)
    chk("C12 🔴🔴 上游明说 cache_read=0 → 命中率 = 0.0（**与 None 严格区分**）",
        s2["cache_hit_rate"] == 0.0, str(s2["cache_hit_rate"]))

    # days 窗口真的在筛
    chk("C13 `days=0` = 不限时间（全部都在）",
        U.summary(relayC, days=0)["calls"] == 5, "")

    # ══════════════════════════════════════════════════════════════════════
    # D. 端点（ASGI，不起端口）+ 红线 + 接线
    # ══════════════════════════════════════════════════════════════════════
    sect("D. 端点：全只读 / 鉴权 / 夹紧 / 红线")

    from fastapi.testclient import TestClient
    relayD = fresh_db(tmp, "api", seed_owner=True)
    U.record(relayD, provider_id="relay", model="claude-opus-4-6", route="chat",
             stream=True, usage=OPENAI_U, session_id="s-x")
    install_usage(relayD)
    c = TestClient(relayD.app)
    H = {"Authorization": f"Bearer {SECRET}"}

    chk("D1 无密钥 → 401（fail-closed）",
        c.get("/app/ext/usage/summary").status_code == 401, "")
    chk("D2 带密钥 → summary 200 且 ok=True",
        c.get("/app/ext/usage/summary", headers=H).status_code == 200
        and c.get("/app/ext/usage/summary", headers=H).json()["ok"] is True, "")
    chk("D3 recent 200 / status 200",
        c.get("/app/ext/usage/recent", headers=H).status_code == 200
        and c.get("/app/ext/usage/status", headers=H).status_code == 200, "")

    body = c.get("/app/ext/usage/recent", headers=H).json()
    chk("D4 🔴 响应里**没有 raw 字段**（默认不外泄上游原文）",
        "raw" not in body["items"][0], str(list(body["items"][0].keys())))
    body_raw = c.get("/app/ext/usage/recent?raw=1", headers=H).json()
    chk("D5 `?raw=1` 才给原文", "raw" in body_raw["items"][0], "")

    chk("D6 🔴🔴 **没有写入口**：POST /usage/summary → 405（账不能被外部伪造）",
        c.post("/app/ext/usage/summary", headers=H).status_code == 405, "")
    chk("D7 三个端点都没有 PUT / DELETE",
        c.put("/app/ext/usage/recent", headers=H).status_code == 405
        and c.delete("/app/ext/usage/recent", headers=H).status_code == 405, "")

    lim = c.get("/app/ext/usage/recent?limit=99999", headers=H).json()["filter"]["limit"]
    lim0 = c.get("/app/ext/usage/recent?limit=0", headers=H).json()["filter"]["limit"]
    chk("D8 `?limit=` 夹紧（99999 → 500；0 → 1）", lim == 500 and lim0 == 1, f"{lim}/{lim0}")
    d = c.get("/app/ext/usage/summary?days=99999", headers=H).json()["window_days"]
    chk("D9 `?days=` 夹紧（99999 → 3650）", d == 3650, str(d))
    chk("D10 `?days=abc` 不炸（回落 7）",
        c.get("/app/ext/usage/summary?days=abc", headers=H).json()["window_days"] == 7, "")

    st = c.get("/app/ext/usage/status", headers=H).json()
    chk("D11 status 回答'记账自己在不在工作'（行数 / 口径分布）",
        st["rows"] == 1 and st["by_shape"][0]["shape"] == "openai"
        and st["by_route"][0]["route"] == "chat", str({k: st[k] for k in
                                                       ("rows", "by_shape", "by_route")}))

    # ── 红线 / 接线 ────────────────────────────────────────────────────────
    import app_ext as AE
    chk("D12 三条路由都在 __init__._ROUTES 里",
        all(r in AE._ROUTES for r in ("/app/ext/usage/summary",
                                      "/app/ext/usage/recent",
                                      "/app/ext/usage/status")), "")

    init_src = (DEPLOY / "app_ext" / "__init__.py").read_text(encoding="utf-8")
    chk("D13 __init__ 里有第 ⑫ 步（usage 记账）与逃生开关",
        "⑫ usage 记账" in init_src and "APP_EXT_USAGE_DISABLED" in init_src
        and "usage.install" in init_src, "")

    usage_src = (DEPLOY / "app_ext" / "usage.py").read_text(encoding="utf-8")
    store_src = (DEPLOY / "app_ext" / "usage_store.py").read_text(encoding="utf-8")
    chk("D14 🔴 usage.py **不 import mcp**（它不挂 MCP 门 —— 那是我的账，不是给他的）",
        "import mcp" not in usage_src and "modules" not in usage_src, "")
    chk("D15 🔴 usage.py 里没有任何 POST / PUT / DELETE 装饰器",
        not re.search(r"\.(post|put|delete|patch)\(", usage_src), "")

    # 红线：这层不许碰别人的表
    bad = []
    for f in ("usage.py", "usage_store.py"):
        src = (DEPLOY / "app_ext" / f).read_text(encoding="utf-8")
        for kw in ("INSERT INTO messages", "UPDATE messages", "DELETE FROM messages",
                   "INSERT INTO memories", "DROP TABLE", "ALTER TABLE"):
            if kw in src:
                bad.append(f"{f}:{kw}")
    chk("D16 🔴 源码扫描：usage 这一层只碰 usage_log（不写 messages / 不 DROP / 不 ALTER）",
        not bad, str(bad))

    schema_src = (DEPLOY / "app_ext" / "schema.py").read_text(encoding="utf-8")
    ul = schema_src.split('"usage_log"')[-1][:1400]
    chk("D17 🔴 usage_log 的 user_id **故意不带外键**（外键 = 会静默吞账的开关）",
        "user_id            TEXT,              -- 标签，不是外键" in ul
        or re.search(r"user_id\s+TEXT,\s*\n", ul), "")

    chk("D18 verify_all 已接本套",
        "usage_check.py" in (HERE / "verify_all.py").read_text(encoding="utf-8"), "")

    # ── 逃生开关（真跑一次 register）────────────────────────────────────────
    os.environ["APP_EXT_USAGE_DISABLED"] = "1"
    os.environ["APP_EXT_ROOMS_DISABLED"] = "1"
    try:
        relayE = fresh_db(tmp, "off", seed_owner=True)
        summ = AE.register(relayE)
        paths = {getattr(r, "path", None) for r in relayE.app.routes}
        chk("D19 🔴 开关打开时：端点**不挂**（404），且 summary 里点名是开关关的",
            summ.get("usage") == "disabled by APP_EXT_USAGE_DISABLED"
            and "/app/ext/usage/summary" not in paths, str(summ.get("usage")))
        chk("D20 🔴 '能力没了 ≠ 数据没了'：表照建（usage_log 仍在 schema 报告里）",
            S.schema_report(relayE)["tables_present"]["usage_log"] is True, "")
    finally:
        os.environ.pop("APP_EXT_USAGE_DISABLED", None)
        os.environ.pop("APP_EXT_ROOMS_DISABLED", None)

    # ── 启动日志 GBK 安全 ──────────────────────────────────────────────────
    line = U.summary_line()
    try:
        line.encode("gbk")
        gbk_ok, gbk_err = True, ""
    except Exception as e:
        gbk_ok, gbk_err = False, f"{type(e).__name__}: {e}"
    chk("D21 🔴 summary_line() GBK 安全（Windows 启动日志会打它）",
        gbk_ok, gbk_err)
    chk("D22 🔴 usage.py / usage_store.py 的 print 里没有 GBK 编不出的符号",
        not [l for l in (usage_src + store_src).splitlines()
             if "print(" in l and re.search(r"[\U0001F300-\U0001FAFF\u2705\u26A0\u274C]", l)],
        "")

    # ══════════════════════════════════════════════════════════════════════
    # E. 出站索要账单（真跑 providers.adapt，纯函数不发 HTTP）
    # ══════════════════════════════════════════════════════════════════════
    sect("E. 出站请求：流式**必须开口要**，上游才给 usage")

    from app_ext import providers as P

    def adapt_body(pid, stream, **env):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
        try:
            req = {"system": "s", "messages": [{"role": "user", "content": "hi"}],
                   "params": {}, "stream": stream}
            return P.adapt(pid, "m", req)[2]
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    b = adapt_body("relay", True, PROVIDER_RELAY_KEY="k")
    chk("E1 🔴🔴 流式 openai 出站带 stream_options.include_usage（不带就永远收不到账单）",
        b.get("stream_options") == {"include_usage": True}, str(b.get("stream_options")))
    b2 = adapt_body("relay", False, PROVIDER_RELAY_KEY="k")
    chk("E2 🔴 非流式**不加** stream_options（它本来就带回 usage，加了反而给某些站找麻烦）",
        "stream_options" not in b2, str(b2))
    b3 = adapt_body("relay", True, PROVIDER_RELAY_KEY="k", LLM_STREAM_USAGE="0")
    chk("E3 🔴 `LLM_STREAM_USAGE=0` → 出站 body 回到改动前（无 stream_options，一键可退）",
        "stream_options" not in b3, str(b3))
    b4 = adapt_body("relay", True, PROVIDER_RELAY_KEY="k", LLM_STREAM_USAGE="1")
    chk("E4 `=1` / 空 / 其他值 = 要（默认开）",
        b4.get("stream_options") == {"include_usage": True}, "")
    b5 = adapt_body("anthropic", True, PROVIDER_ANTHROPIC_KEY="k")
    chk("E5 🔴 Anthropic 格式**不**加 stream_options（它原生就带账单，且不认这个字段）",
        "stream_options" not in b5, str(b5))

    # ══════════════════════════════════════════════════════════════════════
    # F. 源码扫描：流内合并 + 四处记账调用点
    # ══════════════════════════════════════════════════════════════════════
    sect("F. 接线守门：合并而非覆盖 / 四种收尾都留痕 / 防重记")

    gw_src = (DEPLOY / "app_ext" / "llm_gateway.py").read_text(encoding="utf-8")
    chk("F1 🔴🔴 流内 usage 是 **update 合并**（Anthropic 分两帧发，覆盖会丢一半）",
        "usage.update(u)" in gw_src and "usage = u" not in gw_src, "")

    # 两帧分开的 Anthropic 账单，`_pick_usage` 各捡到一半（互补）
    from app_ext.llm_gateway import _pick_usage
    p1 = json.dumps({"type": "message_start",
                     "message": {"usage": {"input_tokens": 100,
                                           "cache_read_input_tokens": 900}}})
    p2 = json.dumps({"type": "message_delta", "usage": {"output_tokens": 50}})
    u1, u2 = _pick_usage(p1), _pick_usage(p2)
    merged = {}
    merged.update(u1)
    merged.update(u2)
    chk("F2 两帧各捡到一半（message_start 给输入 / message_delta 给输出）",
        u1.get("input_tokens") == 100 and u1.get("cache_read_input_tokens") == 900
        and u2.get("output_tokens") == 50, f"{u1} | {u2}")
    chk("F3 合并之后两半都在（正是 `usage.update` 那条改动的意义）",
        U.normalize(merged)["prompt_tokens"] == 100
        and U.normalize(merged)["completion_tokens"] == 50
        and U.normalize(merged)["cache_read_tokens"] == 900, str(merged))

    rt_src = (DEPLOY / "app_ext" / "llm_routes.py").read_text(encoding="utf-8")
    chk("F4 🔴 四条收尾路径都记账：complete / end / stopped / upstream_error",
        rt_src.count("_bill(relay") >= 5
        and 'note="stopped"' in rt_src and 'note="upstream_error"' in rt_src
        and 'route="complete"' in rt_src and 'route="chat"' in rt_src, "")
    chk("F5 🔴 下游断开（手机锁屏）也有兜底记账 + 防重记标志",
        'note="client_disconnect"' in rt_src and "if not billed:" in rt_src
        and "billed = True" in rt_src, "")
    # `_bill` 的 fail-open：函数体里必须有兜住全部的 except，且注释写明理由。
    bill_body = rt_src.split("def _bill(")[-1].split("\ndef ")[0]
    chk("F6 🔴 `_bill` 是 fail-open（记账不许把说话搞挂）",
        "except Exception" in bill_body and "有意兜住全部" in bill_body, "")

    # ══════════════════════════════════════════════════════════════════════
    # G. 真样本回归：中转站的**双命名**账单（线上抄回来的原文）
    # ══════════════════════════════════════════════════════════════════════
    sect("G. 真样本：中转站的双命名账单（`output_tokens` 恒 0 那个坑）")

    n = U.normalize(REAL_RELAY_U)
    chk("G1 🔴 双命名 → shape = openai+anthropic（不是 anthropic）",
        n["shape"] == U.HYBRID_SHAPE, str(n["shape"]))
    chk("G2 🔴🔴 输出 token 读 **450** —— 不是 Anthropic 那半的 `output_tokens: 0`"
        "（线上第一笔账就踩了这个坑：total 是对的，所以从外面看不出来）",
        n["completion_tokens"] == 450, str(n["completion_tokens"]))
    chk("G3 输入 4479 / 总数 4929（两套在这两处一致，照旧读对）",
        n["prompt_tokens"] == 4479 and n["total_tokens"] == 4929, str(n))
    chk("G4 站子一个缓存字段都没报 → `cache_read` = None（'没说'，不是 0）",
        n["cache_read_tokens"] is None and n["cache_write_tokens"] is None, str(n))
    chk("G5 🔴 双命名时口径**不明** → `cache_in_prompt` = None（不猜）",
        n["cache_in_prompt"] is None, str(n["cache_in_prompt"]))

    n = U.normalize(REAL_ANTHROPIC_WINS)
    chk("G6 🔴 反着来：平的是 Anthropic 那套 → 就读 Anthropic"
        "（判据是'哪一套算得平'，不是名字先后）",
        n["shape"] == "anthropic" and n["prompt_tokens"] == 100
        and n["completion_tokens"] == 50 and n["cache_in_prompt"] == 0, str(n))

    n = U.normalize(REAL_NEITHER)
    chk("G7 两套都算不平 → 仍读 OpenAI 那套（它自带 `total_tokens`，至少总账是对的）",
        n["shape"] == U.HYBRID_SHAPE and n["prompt_tokens"] == 10
        and n["completion_tokens"] == 10, str(n))

    chk("G8 🔴 `unknown`（压根没认出来）**不许**声称口径 —— 认不出却写 1 = 凭空担保",
        U.normalize({"foo": 1})["cache_in_prompt"] is None
        and U.normalize({"prompt_tokens": 5})["cache_in_prompt"] == 1, "")

    # ── 真写库 + 读侧 ─────────────────────────────────────────────────────
    relayH = fresh_db(tmp, "hybrid", seed_owner=True)
    U.record(relayH, provider_id="relay", model="claude-opus-4-6-thinking",
             route="chat", stream=True, usage=REAL_RELAY_U, ms=17300,
             chars_out=298, session_id="api-1")
    row = U.recent(relayH, 1, with_raw=True)[0]
    chk("G9 真写库：读回来 shape / 输入 / 输出都对（450 不是 0）",
        row["shape"] == U.HYBRID_SHAPE and row["prompt_tokens"] == 4479
        and row["completion_tokens"] == 450 and row["total_tokens"] == 4929,
        str({k: row[k] for k in ("shape", "prompt_tokens", "completion_tokens")}))
    h1 = U.summary(relayH, days=0)
    chk("G10 形状怪 ≠ 没账单：覆盖率 1.0、不进缺口（也不该撑高别的行）",
        h1["coverage"] == 1.0 and h1["gaps"] == [], str(h1["gaps"]))
    chk("G11 🔴 站子不报缓存 → 命中率是 `null`（'还不知道'），**不是 0**（'完全没命中'）",
        h1["cache_hit_rate"] is None and h1["tokens"]["cache_read_rows"] == 0,
        str(h1["cache_hit_rate"]))

    U.record(relayH, provider_id="relay", model="m2", route="chat", stream=True,
             usage=HYBRID_WITH_CACHE, session_id="api-2")
    exp_num, exp_den = 900, 1000 + 900 + 30
    got = U.summary(relayH, days=0)["cache_hit_rate"]
    chk("G12 🔴 口径不明的行**保守取和**（分母 = prompt + cache_read + cache_write，"
        "不按'子集'乐观处理）",
        got == round(exp_num / exp_den, 4),
        f"期望 {round(exp_num / exp_den, 4)}（{exp_num}/{exp_den}）实际 {got}")

    shutil.rmtree(tmp, ignore_errors=True)
    return tmp


def main():
    t0 = time.time()
    try:
        run()
    except Exception:
        import traceback
        traceback.print_exc()
        chk("未捕获异常（整套没跑完）", False, "见上面的 traceback")

    npass = sum(1 for _n, ok, _d in results if ok)
    out = os.environ.get("USAGE_CHECK_OUT") or str(HERE / "usage_report.txt")
    try:
        Path(out).write_text(
            "\n".join(f"{'[PASS]' if ok else '[FAIL]'} {n}"
                      + (f"   {d}" if (d and not ok) else "")
                      for n, ok, d in results)
            + f"\n\n共 {len(results)} 项，通过 {npass}，失败 {len(results) - npass}\n",
            encoding="utf-8")
    except Exception:
        pass

    print("")
    print("-" * 66)
    for name, ok, detail in results:
        mark = "[OK]" if ok else "[!!]"
        line = f"{mark} {name}"
        if not ok and detail:
            line += f"   <<< {detail}"
        print(line)
    print("-" * 66)
    print(f"共 {len(results)} 项，通过 {npass}，失败 {len(results) - npass}   "
          f"（{time.time() - t0:.1f}s）")
    print(f"报告已存：{out}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
