# CoT（思考链）显示链路诊断

> 2026-09-15 · Lily 的问题：「我有个站子的 opus4.6thinking 是稳定能出思考链的，
> 但是在 kaelhome 我看不到他的 CoT，不太清楚是我们 kaelhome 没有做思考链展示，还是什么原因。」
>
> **一句话结论：展示层没缺（thinking 气泡是原版就完整具备的能力）。
> 缺的是"往那条管道里灌 CoT"的三段水管，而且断点全在房子这一侧、不在前端。**

---

## 一、先把链路摊开（四层，逐层核对源码）

```
上游站（你的中转站）
  │  SSE 帧：{"choices":[{"delta":{"reasoning_content":"…", "content":"…"}}]}
  │  ⚠️ CoT 走的是**独立字段**（各家叫法不同：reasoning_content / reasoning / thinking）
  ▼
① 房子网关 · 流解析          deploy/app_ext/providers.py:558-563
  │    delta = ch[0].get("delta") or {}
  │    return delta.get("content") or None          ← ❌ 断点①
  │  （anthropic 分支同理：providers.py:569-573 只吃 content_block_delta 的
  │    text_delta，thinking_delta 那类被 return None 静默丢掉）
  ▼
② 房子网关 · 出口帧           deploy/app_ext/llm_routes.py:108-118
  │    _chunk() 只拼 {"choices":[{"delta":{"content": text}}]}
  │                                       ← ❌ 断点②：帧里**没有**承载 CoT 的字段
  ▼
③ 身体（临时人偶）            examples/api_loop.py:325-327 / 364 / 403
  │    chunk = delta.get("content") or ""
  │    并且只往 /channel/out 发 {"type": "reply_delta"}   ← ❌ 断点③
  │    **从不发 {"type": "thinking_delta"}**
  ▼
④ 房子后端                   backend/app.py:665 → handle_stream_delta()
  │    ✅ 支持 thinking_delta：存 kind="thinking" 的消息 + 实时广播
  │       （且 :436 只对 reply 发推送 → thinking 不会震手机，原版设计是对的）
  ▼
⑤ 前端                       web/index.html
  │    ✅ 展示层完整：makeThinking() · .think-* 配色 · ✧/✦ 展开指示 ·
  │       openThinkKeys · DEFAULT_THINK_HEIGHT · streamKindFromEvent()
  │       （把 "thinking_delta" 映成 thinking kind）
  └─ ⚠️ 但 stripInlineThinkingText()（index.html:4455-4461）
        会主动剥掉正文里的 <thinking>…</thinking> —— 见下面断点④
```

**核对结果**：④⑤ 两层全都现成。所以答案不是"kaelhome 没做展示"，
而是**⑤ 在等 ③ 的数据，③ 在等 ①② 的数据，①② 从来没把上游的 CoT 接住过。**

---

## 二、四个断点，各自的"能不能改"

| # | 位置 | 现状 | 能不能改 |
|---|---|---|---|
| ① | `providers.parse_stream` | 只取 `delta.content`，`reasoning_content` 静默丢弃 | ✅ 能 —— `deploy/app_ext/` 是我们自己的层 |
| ② | `llm_routes._chunk` | 出口帧只有 `content` 字段 | ✅ 能 —— 同上 |
| ③ | `examples/api_loop.py` | 只认 `content`、只发 `reply_delta` | 🔴 **改不了** —— 在**红线目录**（`git diff e7c9bf5 -- examples/` 必须为空） |
| ④ | `web/index.html` `stripInlineThinkingText` | 把内联的 `<thinking>` 当垃圾剥掉**丢弃** | ✅ 能 —— 前端是本项目**唯一获批的例外** |

### 🔴 断点③是这件事的真正卡口

身体在红线目录里改不了，**而 CoT 要到前端必须经过身体**：

    上游 CoT → 网关 → **身体** → POST /channel/out → 落库 + 广播 → 前端

网关和前端都会走，**中间那一跳绕不过去**。所以在"临时人偶"这套架构下，
**CoT 没法干净地显示出来** —— 这不是没做，是**结构性做不到**。

再顺手排除一个看着像捷径的做法（记下来，免得以后有人试）：

> ❌ **让网关自己偷偷发 `thinking_delta`**：不行。
> 网关**不知道身体生成的 `stream_id` 和 `api_session`**（那是身体本地 `uuid4` 生成的，
> 从不往上游传）→ 只能自己编一个 → `handle_stream_delta` 会把它写进
> **错误的会话**（会话名对不上就落进兜底会话）。
> 那正是 2026-09-14 刚修好的那个**串线 bug**（见 `扩展边界.md` §2.5），不能再开一次。

---

## 三、另一个"看到了也白看"的坑：内联 thinking（断点④）

还有一种可能：**上游把思考链内联在 `content` 里**（`<thinking>…</thinking>` 包着），
而不是放在 `reasoning_content` 里。

这种情况**网关会原样透传**（它只取 content，内容里带什么它就发什么），
身体也会原样转发 —— **看着像"通了"**，但到前端被 `stripInlineThinkingText()`
**主动剥掉并且丢弃**，于是 Lily 还是看不到。

这个坑和断点①②③ 是**两条独立的失效路径**，都可能造成"看不到 CoT"：

    路径 A：上游用独立字段 reasoning_content  → 死在断点①②③
    路径 B：上游把 CoT 内联在 content 里      → 死在断点④

🔴 **所以第一件事不是施工，是确诊走的是哪条路。**

---

## 四、确诊办法（不花钱、不施工）

给房子加一个**只读诊断端点** `POST /app/ext/providers/raw`（真发一次最小调用，
把上游返回的**原始 SSE 帧**原样回前 N 行，不做任何解析）。看三件事：

1. 「帧里有没有 `reasoning_content` / `reasoning` / `thinking` 这类**独立字段**」
   → 有 = 路径 A
2. 「`content` 里是不是夹着 `<thinking>` 或 `…` 这种内联标记」→ 有 = 路径 B
3. 「两样都没有」→ **上游压根没给**，问题在**请求参数**（多半要开
   `reasoning_effort` / `thinking` 才会出 CoT）→ 先用 P1 收尾新加的
   `PROVIDER_RELAY_EFFORT_PARAM` 试一发

> 这一步的价值：先花十分钟看清是"上游没给"还是"我们丢了"，
> 再决定要不要动身体那一侧。否则容易在错的地方改半天。

---

## 五、归属哪个阶段 + 三条路线（等 Lily 拍板）

**归属结论**：它不属于 P1、也不属于 P2 的任何一个已列项 —— 它是一个**横跨三层的小专项**，
且**必然和 P3 绑在一起**（因为卡口在身体）。

| 路线 | 做什么 | 现在能看到 CoT 吗 | 代价 |
|---|---|---|---|
| **甲 · 等 P3（推荐）** | P1 尾巴只做①②（网关接住 reasoning 并放进出口帧的 `reasoning_content`），**不动 ③**；等换 KaelLife 时在**自己的代码里**直接发 `thinking_delta` | 换完身体就能看到 | 要多等一个阶段；但现在看不到是**结构性**的，硬做就得付下面的代价 |
| **乙 · 内联绕行** | ① 网关把 reasoning **内联**进 `content`（`<thinking>…</thinking>` 前缀）+ ④ 前端把它**剥出来渲染成 thinking 气泡**（而不是丢掉） | ✅ 立刻能看到 | 🔴 **CoT 会跟着 reply 一起进 `messages` 正文并永久留在库里**；也把"thinking 是中间态、不进正文"的设计破了 |
| **丙 · 只确诊不施工** | 只加第四节那个只读诊断端点，先把"上游到底给不给"钉死 | ❌ 看不到 | 最小改动、零风险；确诊完再选甲或乙 |

**Bunny 的建议：丙 → 甲**。
先用一个只读端点花十分钟确认上游到底把 CoT 放在哪（这决定后面所有事），
然后按甲做 P1 尾巴那两处（纯新增、可离线验收），③ 留给 P3 换身体时一次做对。
**乙不是不能用，但它是"把 CoT 写进聊天记录"** —— 在 Lily 有数据丢失创伤、
且把"记忆主权"看得极重的前提下，往 `messages` 正文里塞一段模型自言自语，
我不推荐在没有明确需要时先做。

---

## 六、顺带说清一件事（免得混淆）

`effort`（设置页那个推理强度）和 CoT **是同一把钥匙的两面**：

- `effort` = **要不要让他想、想多深**（要发给上游的参数）
- CoT = **他想了什么**（上游发回来的内容）

P1 收尾已经把 `effort` 做到"能落库、能配字段名发出去"（默认不发，见
`deploy/zeabur-env.example` 的 effort 段）。所以**如果第四节确诊出是路径 C
（上游压根没给）**，那把 `PROVIDER_RELAY_EFFORT_PARAM` 配上再试，
很可能就直接把 CoT 打开了 —— 那时候①②两处网关改动就是最后一块拼图。

三条路的顺序因此是：

    ① 诊断端点看原始帧  →  ② 若"上游没给"就先配 EFFORT_PARAM  →  ③ 再做网关①②  →  ④ P3 做③
