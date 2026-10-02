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

房子的 `backend/ examples/ channel/` 是**红线目录**：验收 ㉝（app_ext）与 ㊿
（providers）都断言 `git diff e7c9bf5 -- backend/ examples/ channel/` 为空。
所以"给 wake 消息开一条会话豁免"这类房子侧改动**一条都不能加**。

房子不改，这件事仍然能做对，靠的是**身体把会话带上**：

  · 前端 `history_for_session` 按 `json_extract(meta,'$.api_session') = ?` 严格过滤
  · 红线文件 `examples/api_loop.py` 的 `relay_rows()` 同样按 api_session 过滤
    （他构建上下文时读的就是这段，改不了）
  ⇒ 一条 `type=reply` 只要**带上她当前会话的 id**，两条路就都通了 —— 她看得见，
    他下次说话也想得起。不带的话两条路都会把它滤掉，等于没说。

会话 id 从哪来：身体每次醒来本来就会 `GET /app/history` 把房子聊天拉回来写 OB
（`_pull_house_chat`），顺手从她的消息里取 `meta.api_session` 即可，不多发请求。

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
        where.append("json_extract(meta, '$.api_session') = ?")
        params.append(session_id)
    else:
        where.append("(json_extract(meta, '$.api_session') IS NULL "
                     "OR json_extract(meta, '$.api_session') = '')")
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

        # ── A. 带上会话：落库形状 + 她看得见 ───────────────────────────
        print("\n— A 「留给 Lily 的话」落成一条普通聊天消息（带会话） —")
        human_id = post(base, "/app/send", {"text": "在吗", "api_session": SID})["id"]
        wake_id = post(base, "/channel/out", {"type": "reply", "text": WAKE_TEXT,
                                             "api_session": SID})["id"]
        row = rows_by_id(db_path, [wake_id])[0]
        meta = json.loads(row["meta"])
        chk("A1 kind='reply'（渲染走普通聊天气泡那条管线）", row["kind"] == "reply", row["kind"])
        chk("A2 direction='out'（是他说的，不是她说的）", row["direction"] == "out", row["direction"])
        chk("A3 原文一字不改（不加标签、不加前缀）", row["text"] == WAKE_TEXT, row["text"])
        chk("A4 带上 api_session = 她当前那个会话",
            meta.get("api_session") == SID, str(meta))
        chk("A5 会话历史里有这条（前端那条路）", wake_id in hist_ids(base, SID))
        hit = [m for m in get(base, f"/app/history?since=0&limit=500&session_id={SID}")["messages"]
               if m["id"] == wake_id]
        chk("A6 拉到的 from='ai' + 正文完整（渲染成他的气泡）",
            bool(hit) and hit[0].get("from") == "ai" and hit[0].get("text") == WAKE_TEXT,
            str(hit[:1]))
        chk("A7 别的会话看不见它（归属明确，不是到处广播）",
            wake_id not in hist_ids(base, "api-OTHER"), "api-OTHER")

        # ── B. 他的上下文那条路：红线 relay_rows ───────────────────────
        print("\n— B 他的上下文那条路：红线 relay_rows 原样 SQL 读得到 —")
        ctx = relay_rows_like_loop(db_path, SID)
        chk("B1 relay_rows 能捞到这条（kind=reply + api_session 齐了）",
            WAKE_TEXT in [r["text"] for r in ctx], f"捞出 {len(ctx)} 条")
        chk("B2 位置正确：它是她上一条之后的最新一条",
            ctx and ctx[-1]["text"] == WAKE_TEXT, str(ctx[-1:]))

        # ── C. 反向验证：不带会话 → 两条路都看不见 ────────────────────
        print("\n— C 反向验证：不带 api_session 就没戏（这就是「必须带上」的理由） —")
        bare_id = post(base, "/channel/out", {"type": "reply",
                                              "text": WAKE_TEXT + "（裸）"})["id"]
        chk("C1 不带 api_session → 她的会话里看不见",
            bare_id not in hist_ids(base, SID), "不带就是看不见")
        _raw = rows_by_id(db_path, [bare_id])[0]
        chk("C2 不带 api_session → 行照样落库（不是丢了，是归属不明）",
            _raw["kind"] == "reply" and _raw["text"].endswith("（裸）"), str(_raw)[:120])
        chk("C3 不带 api_session → 红线 relay_rows 也读不到（他上下文里没有）",
            (WAKE_TEXT + "（裸）") not in [r["text"] for r in relay_rows_like_loop(db_path, SID)],
            "这正是要在身体侧补上会话的原因")

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
        chk("E2 会话取自 state['house_session']（不是硬编一个）",
            'state.get("house_session")' in src_sched and 'body["api_session"] = _sid' in src_sched,
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
        chk("E7 L5b 在 _pull_house_chat 之后被调（会话已经读到了才发话）",
            src_sched.rindex("_say_to_house(state, decision)")
            > src_sched.index("_house_chat_signal = _pull_house_chat(state)"),
            "查唤醒收尾的顺序（rindex = 调用点，不是函数定义）")
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
