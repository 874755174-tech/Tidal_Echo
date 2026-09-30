# -*- coding: utf-8 -*-
"""
验收器：行迹卡片跨会话显示（补丁① history_for_session 豁免 kind='activity'）。

治 2026-09-30 实拍病：
  KaelLife 的 activity POST 到 /channel/out 落库时 meta 无 api_session，
  history_for_session 按 api_session 严格过滤 → 卡片在真实会话里被滤没、
  只剩 __legacy__（「旧主线 / Desktop 记录」）能看到；前端 index.html 的
  msgInActiveSession 本来就写了"activity 每个会话都显示"，被后端先滤。

钉死：
  A/B  真实会话能看到自己的消息 + activity，且不串别的会话；
  C    __legacy__ 仍含 activity、不混入带会话的消息；
  D    旧 SQL（无豁免）确实漏掉 activity —— 证明本测试真在测"豁免"这处差异。

用法：
  python _check_history.py
"""
import os
import sys
import tempfile

# 必须在 import app 之前设好（SECRET 为空会 SystemExit；DB 用临时文件不污染真实库）
os.environ["RELAY_SECRET"] = "test-secret"
os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(), "test-relay.db")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "backend"))

import app  # backend/app.py

_results = []


def chk(name, cond, detail=""):
    _results.append(bool(cond))
    print(f"[{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if not cond else ""))


def kinds(rows):
    return [m["kind"] for m in rows]


# ---- 造数 ----
app.init_db()
app.save_message("in", "user", "会话1的消息", {"api_session": "api-test-1"})
app.save_message("out", "activity", "我回去看了那个帖", {"activity": {"state": "done"}})
app.save_message("in", "user", "会话2的消息", {"api_session": "api-test-2"})
app.save_message("out", "reply", "会话1的回复", {"api_session": "api-test-1"})

# ---- A/B：真实会话 ----
r1 = app.history_for_session("api-test-1", 0, 100)
chk("A1 会话1能看到自己的 user", "user" in kinds(r1), str(kinds(r1)))
chk("A2 会话1能看到 activity 行迹卡片", "activity" in kinds(r1), str(kinds(r1)))
chk("A3 会话1能看到自己的 reply", "reply" in kinds(r1), str(kinds(r1)))
nonact1 = [m["text"] for m in r1 if m["kind"] != "activity"]
chk("A4 会话1不串会话2的 user", "会话2的消息" not in nonact1, str(nonact1))

r2 = app.history_for_session("api-test-2", 0, 100)
chk("B1 会话2能看到自己的 user", "user" in kinds(r2), str(kinds(r2)))
chk("B2 会话2也能看到 activity（跨会话显示）", "activity" in kinds(r2), str(kinds(r2)))

# ---- C：legacy 桶 ----
rl = app.history_for_session("__legacy__", 0, 100)
chk("C1 __legacy__ 仍能看到 activity", "activity" in kinds(rl), str(kinds(rl)))
chk("C2 __legacy__ 不混入带会话的消息", all(m["kind"] == "activity" for m in rl),
    str([m["text"] for m in rl]))

# ---- D：有牙对照（旧 SQL 无豁免 → 漏 activity）----
conn = app.db()
old = conn.execute(
    "SELECT * FROM messages WHERE id > 0 "
    "AND json_extract(meta, '$.api_session') = ? ORDER BY id ASC",
    ("api-test-1",)).fetchall()
old_kinds = [r["kind"] for r in old]
chk("D1 旧 SQL 会漏掉 activity（有牙：豁免是真实差异）",
    "activity" not in old_kinds, str(old_kinds))

n = len(_results)
ok = sum(_results)
print(f"\n{'PASS' if ok == n else 'FAIL'}  {ok}/{n} 项通过")
sys.exit(0 if ok == n else 1)
