# CoT（思考链）显示链路诊断

> 2026-09-15 · Lily 的问题：「我有个站子的 opus4.6thinking 是稳定能出思考链的，
> 但是在 kaelhome 我看不到他的 CoT，不太清楚是我们 kaelhome 没有做思考链展示，还是什么原因。」
>
> **一句话结论：展示层没缺（thinking 气泡是原版就完整具备的能力）。
> 缺的是"往那条管道里灌 CoT"的三段水管，而且断点全在房子这一侧、不在前端。**
>
> 🆕 **2026-09-15**：第 1 步（丙）落地 —— 只读诊断端点 `POST /app/ext/providers/raw`。
> 🆕🆕 **2026-09-16**：**确诊 = 路径 A，断点 ①② 已修好并通过验收**（166/166，含突变测试）。
> 剩下 ③ 留给 P3（换身体时一并做）。

---

## 〇、确诊结论（2026-09-16，证据在手）

`POST /app/ext/providers/raw` 对那台站的 `claude-opus-4-6-thinking` 打了一发，
拿回来的**原始帧**长这样（截取）：

    data: {"choices":[{"delta":{"content":"","role":"assistant"}}], ...}
    data: {"choices":[{"delta":{"reasoning_content":"The use"}}], ...}   ← 🔴 CoT 在这
    data: {"choices":[{"delta":{"reasoning_content":"r is spea"}}], ...}
    data: {"choices":[{"delta":{"reasoning_content":"king Ch"}}], ...}
    data: {"choices":[{"delta":{"reasoning_content":"inese"}}], ...}
    ...
    data: {"choices":[{"delta":{"content":"在"}}], ...}                  ← 正文才开始
    data: {"choices":[{"delta":{"content":"的"}}], ...}
    ...
    data: {"choices":[],"usage":{...}}
    data: [DONE]

拼起来是 `The user is speaking Chinese, asking "Are you there?" - a casual greeting.`

**判定 `hints.path = "A"`** —— 上游**给了**独立思考链字段（`delta.reasoning_content`，
DeepSeek 系命名），是我们网关解析时**只取 `content`、把它静默丢了**。

结论：不是"那台站不出 CoT"，也不是"前端没做展示"，就是**中间两段水管没接**。

---

## 一、先把链路摊开（五层，逐层核对源码）

```
上游站（中转站）
  │  SSE 帧：{"choices":[{"delta":{"reasoning_content":"…", "content":"…"}}]}
  │  ✅ 实测就是这么发的（字段名 reasoning_content；各家还可能是 reasoning / thinking）
  ▼
① 房子网关 · 流解析          deploy/app_ext/providers.py  parse_stream_parts()
  │    ✅ 已修（2026-09-16）：按"这是正文还是思考链"分流
  │       openai   → delta.reasoning_content / reasoning / thinking / reasoning_details
  │       anthropic→ delta.type == "thinking_delta"
  │       gemini   → parts[].thought == true
  │    老接口 parse_stream() 保留原行为（只回正文），一大票老断言不受影响
  ▼
② 房子网关 · 出口帧           deploy/app_ext/llm_routes.py  _chunk()
  │    ✅ 已修（2026-09-16）：思考链走 `delta.reasoning_content`，
  │       **且该帧不带 `content` 键**（下游身体靠这个跳过它）；
  │       没有 CoT 时一个字段都不加 → 形状与改前逐字节相同
  ▼
③ 身体（临时人偶）            examples/api_loop.py:325-327 / 364 / 403
  │    chunk = delta.get("content") or ""
  │    并且只往 /channel/out 发 {"type": "reply_delta"}   ← ❌ 断点③（仍存在）
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

**核对结果**：④⑤ 两层全都现成；①②**已补齐**；只剩 ③。

---

## 二、四个断点，各自的"能不能改"

| # | 位置 | 现在的状态 | 能不能改 |
|---|---|---|---|
| ① | `providers.parse_stream_parts` | ✅ **已修（09-16）** 按 reasoning / content 分流；老 `parse_stream` 行为不变 | ✅ 已改 —— `deploy/app_ext/` 是我们自己的层 |
| ② | `llm_routes._chunk` | ✅ **已修（09-16）** 思考链走 `delta.reasoning_content`（无 CoT 时不加字段） | ✅ 已改 —— 同上 |
| ③ | `examples/api_loop.py` | ❌ **仍是卡口**：只认 `content`、只发 `reply_delta` | 🔴 **改不了** —— 在**红线目录**（`git diff e7c9bf5 -- examples/` 必须为空） |
| ④ | `web/index.html` `stripInlineThinkingText` | 把内联的 `<thinking>` 当垃圾剥掉**丢弃** | ✅ 能 —— 前端是本项目**唯一获批的例外**（只在走路径 B 时才需要动） |

### 🔴 断点③是剩下的唯一卡口

身体在红线目录里改不了，**而 CoT 要到前端必须经过身体**：

    上游 CoT → 网关(①②✅) → **身体(③❌)** → POST /channel/out → 落库 + 广播 → 前端(④⑤✅)

网关和前端都会走，**中间那一跳绕不过去**。所以**当下**在网页上看不到 CoT ——
但**不是白做**：①② 一修，"上游给了什么"已经能拿出来了（见第六节，现在就能自己验），
等 P3 换身体时只需在**我们自己的代码里**发一条 `thinking_delta` 就全通。

再顺手排除一个看着像捷径的做法（记下来，免得以后有人试）：

> ❌ **让网关自己偷偷发 `thinking_delta`**：不行。
> 网关**不知道身体生成的 `stream_id` 和 `api_session`**（那是身体本地 `uuid4` 生成的，
> 从不往上游传）→ 只能自己编一个 → `handle_stream_delta` 会把它写进
> **错误的会话**（会话名对不上就落进兜底会话）。
> 那正是 2026-09-14 刚修好的那个**串线 bug**（见 `扩展边界.md` §2.5），不能再开一次。

---

## 三、另一条独立失效路径：内联 thinking（断点④）

如果哪天换的站**把思考链内联在 `content` 里**（`<thinking>…</thinking>` 包着），
而不是放在 `reasoning_content` 独立字段里：

    网关会原样透传（它把整段当 content）→ 身体原样转发 → **看着像通了**
    → 到前端被 stripInlineThinkingText() **主动剥掉并且丢弃** → 还是看不到

这是**与路径 A 完全独立的**另一条失效路径：

    路径 A：上游用独立字段   → 死在断点①②③（①②已修，③待 P3）
    路径 B：上游内联在正文里 → 死在断点④（要动前端）

🔴 所以第一步永远是**确诊走哪条路**，不要凭猜施工。诊断端点见第四节。

---

## 四、确诊办法（✅ 已落地，两条路都验过）

给房子加了一个**只读诊断端点** `POST /app/ext/providers/raw`（真发一次最小调用，
把上游返回的**原始 SSE 帧**原样回前 N 行，不做任何解析）。它回答三件事：

1. 「帧里有没有 `reasoning_content` / `reasoning` / `thinking` 这类**独立字段**」
   → 有 = 路径 A ← **实测就是这条**
2. 「`content` 里是不是夹着 `<thinking>` 这种内联标记」→ 有 = 路径 B
3. 「两样都没有」→ **上游压根没给**，问题在**请求参数**（多半要开
   `reasoning_effort` / `thinking` 才会出 CoT）→ 先用
   `PROVIDER_RELAY_EFFORT_PARAM` 试一发

### 🔴 判定带引号是必须的（踩过的坑）

`raw_hints()` 里匹配 `"reasoning_content"` 这类**带引号**的形式 ——
否则 `<thinking>` 里的裸词 `thinking` 会被误判成路径 A，A/B 就分不开了。
验收里 72/77/78 三条专门盯这个，突变测试（去掉引号）会红。

---

## 五、归属哪个阶段 + 三条路线（已拍板并执行）

| 路线 | 做什么 | 现在能看到 CoT 吗 | 状态 |
|---|---|---|---|
| **甲 · 网关先行** | ① 解析分流 + ② 出口帧带 `reasoning_content`，**不动 ③** | ❌（要等换身体） | ✅ **已完成（09-16）** |
| **乙 · 内联绕行** | 网关把 reasoning **内联**进 `content` + 前端剥出来渲染 | ✅ 立刻 | ❌ 不做（代价见下） |
| **丙 · 只确诊** | 只加第四节那个只读端点 | ❌ | ✅ 已完成（09-15） |

**执行顺序：丙 → 甲**（Bunny 推荐并被采纳）。

> **乙为什么不做**：CoT 会跟着 reply 一起进 `messages` 正文并**永久留在库里**，
> 也把"thinking 是中间态、不进正文"的设计破了。Lily 有数据丢失创伤、
> 且把"记忆主权"看得极重 —— 在没有明确需要时，不往 `messages` 正文里塞
> 一段模型自言自语。

---

## 六、怎么验证①②修好了（三种，从快到全）

### 1）最省事：双击 `tools\CoT体检.bat`
弹记事本 → 粘 `RELAY_SECRET` → 保存关闭 → 自动跑完并报 `hints.path`。

### 2）直接看"网关出口帧里有没有 CoT"（线上，一条命令）

这是修完①②之后**马上能在线上看到的证据** —— 打网关自己的出口，而不是打诊断端点：

```bash
curl.exe -sN -X POST "https://kaelnlily79.zeabur.app/app/ext/llm/chat" \
  -H "Authorization: Bearer <RELAY_SECRET>" -H "Content-Type: application/json" \
  -d '{"provider_id":"relay","model":"claude-opus-4-6-thinking","messages":[{"role":"user","content":"在吗"}],"stream":true,"max_tokens":32}'
```

看到 `"delta":{"reasoning_content":"…"}` 的帧 = **①② 生效**。
⚠️ PowerShell 里别接 `| python -m json.tool`（PS 会把字节流按行拆，json.tool 会卡住不打印）。

### 3）最全：`python tools/verify_all.py`
八套里的第 5 套（P1 模型网关）覆盖这件事，共 **166 项**，其中 71 那一组是
"打真端点"验的、76 那一组是纯逻辑验的。全绿 = 没回归。

---

## 七、顺带说清一件事（免得混淆）

`effort`（设置页那个推理强度）和 CoT **是同一把钥匙的两面**：

- `effort` = **要不要让他想、想多深**（要发给上游的参数）
- CoT     = **他想了什么**（上游发回来的内容）

`effort` 已做到"能落库、能配字段名发出去"（默认不发，见
`deploy/zeabur-env.example` 的 effort 段）。本次确诊是**路径 A**，
说明那台站**不用配 effort 也在出 CoT**（模型名里带 `-thinking` 就自己出），
所以 `PROVIDER_RELAY_EFFORT_PARAM` 保持不配即可。

---

## 八、这次改动清单（便于回看 / 回退）

| 文件 | 改了什么 |
|---|---|
| `deploy/app_ext/providers.py` | 新增 `parse_stream_parts()` / `parse_complete_parts()` / `_reasoning_of()` / `_REASONING_KEYS`；老的 `parse_stream()` / `parse_complete()` 变成薄包装，**行为一个字没变** |
| `deploy/app_ext/llm_gateway.py` | `stream_chat()` 增加 `on_reasoning` 回调与 `reasoning` 汇总；`complete()` 也汇总 `reasoning`；新增 `expose_reasoning()`（读 `LLM_EXPOSE_REASONING`，默认开） |
| `deploy/app_ext/llm_routes.py` | `_chunk()` 增加 `reasoning` 参数（无 CoT 时不加字段）；`_do_chat()` 队列加 `("r", …)` 分支；`_do_complete()` 的 message 带 `reasoning_content` |
| `tools/providers_check.py` | 145 → **166** 项：71 那一组（打真端点，含"模拟身体消费"）+ 71b/71c/71d/71e 对照 + 76a-76m（纯逻辑）；顺手消掉假上游提前断开的 Traceback 噪音 |
| `deploy/zeabur-env.example` | 新增 `LLM_EXPOSE_REASONING` 说明 |
| `deploy/README.md` / `tools/verify_all.py` | 验收数同步为 166 |

🔴 **红线自查**：`git diff --stat e7c9bf5 -- backend/ examples/ channel/` = **空**。

### 可逆性

- 想回到"出口不带 CoT"：Zeabur 配 `LLM_EXPOSE_REASONING=0`，**不用回滚代码**
- 老接口 `parse_stream()` / `parse_complete()` 语义未变 → 任何老调用方无感
- 没有 CoT 的响应，出口形状**逐字节相同**（验收 71b/71e 盯着）
