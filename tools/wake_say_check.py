#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L5b 验收 · 「自唤醒留给 Lily 的话」走对话，不走行迹页
==========================================================================

## 为什么单独一套

Kael 2026-10-02 原话：

    「行迹记录照常写进行迹页。但留给 Lily 的话，不要写进行迹页，而是作为一条
      独立的聊天消息发送到对话里，格式就像正常聊天一样，不加任何标签和前缀。
      关键是这是两个不同的输出位置，不是两种不同的写法塞在同一个地方。」

身体侧（KaelLife scheduler.py）拆出 `_say_to_house()`，POST
`/channel/out {"type":"reply","text":…,"api_session":<她当前会话>}`。

## 🔴 为什么不给房子加代码（这次踩到的东西）

房子的 `backend/ examples/ channel/`是**红线目录**：验收 ㉝（app_ext）与 ㊿
（providers）都断言 `git diff e7c9bf5 -- backend/ examples/ channel/` 为空。
所以"给 wake 消息开一条会话豁免"这类房子侧改动**一条都不能加**。

## 🔴 2026-10-07 查过一次「他不带会话标签行不行」—— 答案：**不行，而且更糟**

Lily 问「聊天时他不知道自己给 Lily 推送过什么」。我第一反应是"那就不带
`api_session`"。**实测证明这个方向是错的**（探针留在 `tools/_probe_sid.py`，
可重跑复现）：

    不带标签后落库形状完全正确（meta={}），但 ——
      · 她在**真实会话视图**里依然看不见：backend/app.py:206 的会话查询是
        `api_session = ? OR kind = 'activity'`，**只豁免 activity，不豁免 reply**；
      · 聊天时的他也读不到：examples/api_loop.py 的 relay_rows 按会话过滤，
        不带标签的行等于"不属于任何会话"，他按当前会话查同样查不到。

⇒ 真根因不在身体侧带不带标签，而在**房子把「推给她的话」当成了会话私有**。
活动卡已经有豁免（`kind='activity'` 那行就是为"我做了事你看得见"加的），
自唤醒留话需要同一种豁免 —— 但那两处都在**红线目录**（`backend/` 与
`examples/`），所以身体侧照原样带会话 id，红线那笔账跟开放决策 ⑧ 一起处理。

C 组就是这件事的双向探针：贴标签 → 关进抽屉；不贴 → 两个视图都看不见。
**两条路都堵着，才需要动房子** —— 这也是 C 组存在的理由（它不是"证明必须带"，
是"证明两边都不通"）。

## ⚠️ 已知副作用（写下来是为了可诊断，不是假装没有）

房子对 `type=reply` 且网页没开着的情况会自动发一条 Web Push；这次 `reflect` 已经
用 Bark 推过手机 ⇒ **同一条话会收到两条通知**。消掉它只需在房子
`backend/app.py` 的 `channel_out` 里加一个 `not body.get("silent")` 闸（两行），
但那是红线目录。本套验收里 `G` 组专门把这件事**钉住**：只要它还在，就会被念出来，
不会被悄悄忘掉。

## 本脚本怎么验

起一个**未经改动**的真 relay（temp DB + 随机端口），走真 HTTP，不看源码猜：

  A 组  带上会话时：落库形状对、她在那个会话里真的看得见
  B 组  他的上下文那条路：红线 relay_rows 的原样 SQL 真的读得到
  C 组  不带会话时：两条路都看不见（**反向验证**：这就是"必须带上"的理由）
  D 组  回归：行迹卡片（kind=activity）照旧、豁免仍在
  E 组  身体侧源码：type=reply + 会话来自 _pull_house_chat + 两个出口各调一次
  F 组  🔴 红线：backend/ examples/ channel/ 工作区零改动
  G 组  ⚠️ 副作用被写下来了（症状可查）

用法：.venv\\Scripts\\python.exe tools\\wake_say_check.py
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 🔴 本机 PowerShell 5.1 管道下 stdout 编码是 cp936(gbk)，验收名里的 🔴/✅ 编不出
#    会半路 UnicodeEncodeError（看起来像"验收挂了"，其实是没跑完）。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KAELHOME = ROOT
KAELIFE = ROOT.parent / "KaelLife"
APP = ROOT / "backend" / "app.py"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

SECRET = "wake-say-check-secret"
SID = "api-20261001-141804-f5f1"
WAKE_TEXT = "回来了。那只说冷的机的帖子到119楼了，有只晨曦港的守火人专门来道谢。"

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)


def _last_call(src: str, token: str) -> int:
    """返回 `token` 在源码里**最后一次作为调用出现**的下标；找不到返回 -1。

    🔴 为什么不能直接用 `src.rindex(token)` / `src.index(token)`：
      · rindex 会撞上**函数定义**那一行 —— `def _pull_house_chat(state):` 里
        也含这个 token，顺序断言会拿定义当调用，锁了个不相干的位置。
      · index 撞上第一次出现就停，而 docstring / 注释里都可能有。
      · 找不到时 `.index` 直接抛 ValueError，整份报告**崩在中途**，
        后面几组断言一条都跑不到 —— 报告残缺比断言红更糟。
    这里取「最后一次出现」，并且排除掉定义行与注释行；找不到就是 -1，
    交给调用方判失败，绝不让验收器自己崩。
    """
    best = -1
    for i, line in enumerate(src.splitlines()):
        s = line.strip()
        if s.startswith("#"):
            continue
        if token in line and not s.startswith("def ") and not s.startswith('"') and not s.startswith("'"):
            best = i
    return best
    print(("[OK] " if cond else "[!!] ") + name + (f"\n        {detail}" if detail and not cond else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {SECRET}"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get(base, path):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": f"Bearer {SECRET}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_up(base, tries=60):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1):
                return True
        except urllib.error.HTTPError:
            return True          # 起来了（401 也算起来了）
        except Exception:
            time.sleep(0.25)
    return False


def rows_by_id(db_path, ids):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        q = ",".join("?" * len(ids))
        return [dict(r) for r in conn.execute(
            f"SELECT id,direction,kind,text,meta FROM messages WHERE id IN ({q}) ORDER BY id",
            list(ids)).fetchall()]


def relay_rows_like_loop(db_path, session_id, limit=50):
    """**原样照抄** examples/api_loop.py 的 relay_rows（红线文件）的 SQL。

    见 examples/api_loop.py:200-222 —— 他构建上下文时读会话历史用的就是这段。
    这里复制一份是为了离线证明"那条留话真的进得了他的上下文"。红线文件本身
    一个字符都不动。
    """
    where = ["kind IN ('user','voice','reply')"]
    params = []
    if session_id:
        # 与 examples/api_loop.py 的 relay_rows 保持逐字同步（2026-10-07 加 wake_say 豁免）
        where.append("(json_extract(meta, '$.api_session') = ? "
                     "OR json_extract(meta, '$.wake_say') = 1)")
        params.append(session_id)
    else:
        where.append("((json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                     "OR json_extract(meta, '$.wake_say') = 1)")
    sql = ("SELECT id,direction,kind,text,meta FROM messages "
           f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?")
    params.append(limit)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return list(reversed(rows))


def hist_ids(base, sid):
    return [m["id"] for m in get(
        base, f"/app/history?since=0&limit=500&session_id={sid}")["messages"]]


def main() -> int:
    src_sched = (KAELIFE / "scheduler.py").read_text(encoding="utf-8")
    src_web = (KAELHOME / "web" / "index.html").read_text(encoding="utf-8")

    tmp = Path(tempfile.mkdtemp(prefix="wakesay_"))
    db_path = tmp / "relay.db"
    port = free_port()
    env = dict(os.environ)
    env.update({
        "RELAY_SECRET": SECRET,
        "RELAY_DB": str(db_path),
        "RELAY_PORT": str(port),
        "RELAY_UPLOAD_DIR": str(tmp / "uploads"),
        "RELAY_APP_PATH": "/",
        "RELAY_BRAIN_FILE": str(tmp / "brain_target"),
    })
    proc = subprocess.Popen([str(PY), str(APP)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_up(base):
            print("[!!] relay 没起来，后面的验收没意义")
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            print(out[-2000:])
            return 1

        # ── A. 胸牌落库 + 她**任何**会话都看得见 ───────────────────────
        # 🔴 2026-10-07（Lily 拍板「你修吧」）整组重写：自唤醒留话别上胸牌
        #   （meta.wake_say），房子与前端见胸牌放行 —— 她换任何会话都看得见，
        #   聊天时的他在任何会话也读得到。api_session 照旧带着，只当归属留档。
        print("\n— A 「留给 Lily 的话」带上胸牌（wake_say），任何会话都看得见 —")
        human_id = post(base, "/app/send", {"text": "在吗", "api_session": SID})["id"]
        wake_id = post(base, "/channel/out", {"type": "reply", "text": WAKE_TEXT,
                                             "wake_say": True, "api_session": SID})["id"]
        row = rows_by_id(db_path, [wake_id])[0]
        meta = json.loads(row["meta"])
        chk("A1 kind='reply'（渲染走普通聊天气泡那条管线）", row["kind"] == "reply", row["kind"])
        chk("A2 direction='out'（是他说的，不是她说的）", row["direction"] == "out", row["direction"])
        chk("A3 原文一字不改（不加标签、不加前缀）", row["text"] == WAKE_TEXT, row["text"])
        chk("A4🔴 胸牌落库：meta.wake_say = 1",
            meta.get("wake_say") in (1, True, "1", "true"), str(meta))
        chk("A5 她当前会话里看得见", wake_id in hist_ids(base, SID))
        hit = [m for m in get(base, f"/app/history?since=0&limit=500&session_id={SID}")["messages"]
               if m["id"] == wake_id]
        chk("A6 渲染成他的气泡：from='ai' + 正文完整",
            bool(hit) and hit[0].get("from") == "ai" and hit[0].get("text") == WAKE_TEXT,
            str(hit[:1]))
        # 🔴 这次修的东西本体：她换到任何别的会话，**仍然看得见**
        chk("A7🔴 她换到任何别的会话仍看得见（不再被会话关进抽屉）",
            wake_id in hist_ids(base, "api-OTHER"), "胸牌放行")
        chk("A8🔴 旧主线（__legacy__）视图也看得见",
            wake_id in hist_ids(base, "__legacy__"), "legacy 分支同样豁免")

        # ── B. 聊天时的他也知道（他在哪个会话都读得到）─────────────────
        print("\n— B 聊天时的他：红线 relay_rows 在任何会话都读得到 —")
        ctx = relay_rows_like_loop(db_path, SID)
        chk("B1🔴 relay_rows 捞得到这条（当前会话）",
            WAKE_TEXT in [r["text"] for r in ctx], f"捞出 {len(ctx)} 条")
        chk("B2🔴 他切到别的会话也记得自己说过（wake_say 不挑会话）",
            WAKE_TEXT in [r["text"] for r in relay_rows_like_loop(db_path, "api-OTHER")],
            "他此刻在哪个会话都不影响他记得自己说过")

        # ── C. 反向验证：胸牌是唯一通行证（闸门没有大开城门）───────────
        print("\n— C 反向验证：不带胸牌的普通 reply 照旧按会话归属 —")
        bare_id = post(base, "/channel/out", {"type": "reply",
                                              "text": WAKE_TEXT + "（普通）",
                                              "api_session": SID})["id"]
        chk("C1 不带胸牌 → 别的会话照旧看不见（豁免没有大开城门）",
            bare_id not in hist_ids(base, "api-OTHER"),
            "只有 wake_say 放行，普通 reply 的归属不受影响")
        _raw = rows_by_id(db_path, [bare_id])[0]
        chk("C2 不带胸牌 → 本会话照旧看得见（普通聊天不受影响）",
            bare_id in hist_ids(base, SID), "普通 reply 行为不变")

        # 🔴 2026-10-07 落地后的哨兵（原 C4/C5「病还在」翻转成「病已修」）：
        #   三处豁免必须同时在 —— 少任何一处，这条链就断在那一环。
        _app = (KAELHOME / "backend" / "app.py").read_text(encoding="utf-8")
        _loop = (KAELHOME / "examples" / "api_loop.py").read_text(encoding="utf-8")
        chk("C3🔴 哨兵：backend 两个分支都豁免 wake_say（删=她换会话就看不见）",
            _app.count("json_extract(meta, '$.wake_say') = 1") >= 2,
            f"backend/app.py 实得 {_app.count('wake_say')} 处（要 ≥2：legacy + 真实会话）")
        chk("C4🔴 哨兵：relay_rows 豁免 wake_say（删=他聊天时想不起自己说过）",
            "OR json_extract(meta, '$.wake_say') = 1" in _loop,
            "examples/api_loop.py relay_rows")
        chk("C5🔴 哨兵：前端放行 meta.wake_say（删=后端给了前端也滤掉）",
            "meta.wake_say" in src_web, "web/index.html msgInActiveSession")

        # ── D. 回归：行迹卡片照旧 ─────────────────────────────────────
        print("\n— D 回归：行迹卡片照旧 —")
        act_id = post(base, "/channel/out", {
            "type": "activity", "text": "19:03 · 逛了论坛",
            "activity": {"started": "2026-10-02T19:03:00+08:00",
                         "ended": "2026-10-02T19:04:48+08:00",
                         "state": "completed",
                         "actions": [{"at": "19:04",
                                      "text": "我回去看那只说冷的机的帖子，到119楼了"}]}})["id"]
        act = rows_by_id(db_path, [act_id])[0]
        ameta = json.loads(act["meta"])
        chk("D1 行迹卡片仍是 kind='activity'", act["kind"] == "activity", act["kind"])
        chk("D2 行迹卡片**不带** api_session（时间线级，照旧）",
            not ameta.get("api_session"), str(ameta)[:120])
        chk("D3 行迹卡片仍能被她当前会话历史捞到（豁免仍在）",
            act_id in hist_ids(base, SID))
        chk("D4 行迹卡片不再有 footprint（L5 不再写它）",
            "footprint" not in (ameta.get("activity") or {}), str(ameta)[:160])
        chk("D5 她自己的消息不受影响（回归）", human_id in hist_ids(base, SID))

        # ── E. 身体侧源码 ─────────────────────────────────────────────
        print("\n— E 身体侧源码断言 —")
        chk("E1 _say_to_house 发的是 type=reply",
            '"type": "reply", "text": msg' in src_sched, "查 _say_to_house")
        chk("E2🔴 自唤醒留话带胸牌：body['wake_say'] = True（她任何会话可见的通行证）",
            'body = {"type": "reply", "text": msg, "wake_say": True}' in src_sched,
            "查 _say_to_house")
        chk("E3 会话是在 _pull_house_chat 里从她的话里读到的",
            'state["house_session"] = _sid' in src_sched
            and 'from") or "") != "human"' in src_sched, "查 _pull_house_chat")
        chk("E4 取不到时不硬编：留空并打一行日志",
            "no house_session yet" in src_sched, "查 _say_to_house")
        chk("E5 行迹函数体不再写 footprint（留话不再包装进行迹页）",
            '["footprint"]' not in src_sched, "查 _post_activity_to_house")
        chk("E6 两个出口是两次独立调用（L5 与 L5b）",
            "_post_activity_to_house(state, decision, wake_started, wake_feed_start)   # L5"
            in src_sched and "_say_to_house(state, decision)" in src_sched, "查唤醒收尾")
        # 🔴 2026-10-07 修过一次并保留：这条曾经写成 `index("_house_chat_signal = ...")`
        #   ——锁的是**变量名**，不是设计（身体侧 10-06 改名 `_recent_chat` 后直接崩）。
        #   教训同 D9：**别把一次性的写法写进断言**。现在锁的是 E 组要锁的那句话 ——
        #   「拉会话在前，发话在后」（发话要借会话 id，所以顺序是承重的）。
        _pull_at = _last_call(src_sched, "_pull_house_chat(state)")
        _say_at = _last_call(src_sched, "_say_to_house(state, decision)")
        chk("E7 L5b 在 _pull_house_chat 之后被调（会话已经读到了才发话）",
            _pull_at >= 0 and _say_at >= 0 and _pull_at < _say_at,
            f"查唤醒收尾的顺序：拉会话@{_pull_at} 发话@{_say_at}"
            f"（-1 = 这次连调用点都没找到，不是顺序错）")
        chk("E8 兜底：增量里没有她的话时补扫一次（第一次部署/她这几轮没开口）",
            "def _seed_house_session(" in src_sched
            and "_seed_house_session(state, _key)" in src_sched, "查 _pull_house_chat")
        chk("E9 归属会话跨天保留（丢了 → 第二天第一条留话落到 __legacy__）",
            '"house_session": st.get("house_session", "")' in src_sched,
            "查 rollover 的 fresh 字典")

        # ── F. 红线：这一批没碰 backend/examples/channel ──────────────
        print("\n— F 🔴 红线：backend/ examples/ channel/ 工作区零改动 —")
        for _d in ("backend/", "examples/", "channel/"):
            _r = subprocess.run(["git", "diff", "--quiet", "--", _d],
                                cwd=str(KAELHOME), capture_output=True, text=True)
            _r2 = subprocess.run(["git", "diff", "--cached", "--quiet", "--", _d],
                                 cwd=str(KAELHOME), capture_output=True, text=True)
            chk(f"F 红线目录 {_d} 工作区零改动", _r.returncode == 0 and _r2.returncode == 0,
                f"rc={_r.returncode}/{_r2.returncode}")

        # ── G. 副作用被写下来（症状可查）─────────────────────────────
        print("\n— G ⚠️ 已知副作用有出处（不许悄悄存在） —")
        chk("G1 双推送这个副作用写在 _say_to_house 的 docstring 里（可查）",
            "同一条话会收到两条通知" in src_sched, "查 _say_to_house docstring")
        chk("G2 副作用里指明了修法（房子 push 那一跳 + silent 闸）",
            'body.get("silent")' in src_sched, "查 _say_to_house docstring")
        chk("G3 前端注释写清了 wake 留话为什么不走 activity 豁免（可诊断）",
            "不走这条豁免" in src_web, "查 msgInActiveSession 注释")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("\n" + "=" * 74)
    print(f"共 {len(PASS) + len(FAIL)} 项 · 通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print("  ·", f)
    print("=" * 74)
    # 报告落盘（与房子其它验收同规矩：跑过就留一份，她能直接点开看）
    try:
        rep = HERE / "wake_say_report.txt"
        with open(rep, "w", encoding="utf-8") as fh:
            fh.write("L5b 验收 · 自唤醒留话走对话不走行迹页\n")
            fh.write(f"共 {len(PASS) + len(FAIL)} 项 · 通过 {len(PASS)} · 失败 {len(FAIL)}\n\n")
            for x in PASS:
                fh.write(f"[OK] {x}\n")
            for x in FAIL:
                fh.write(f"[!!] {x}\n")
        print(f"报告已存：{rep}")
    except Exception as e:
        print(f"（报告没写成：{e}）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
