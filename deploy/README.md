# deploy/ —— 我们的部署适配层

> **一句话**：这个目录里的东西**不是 Tidal_Echo 的项目**。
> 原作者假设你有一台 VPS、装 nginx、用 systemd；Lily 的机器是 Zeabur 上的容器，
> 没有 nginx、没有 systemd、磁盘还是临时的。这个目录就是为了填这几道缝。

## 边界：谁是谁

| | 出处 | 我们改了吗 |
|---|---|---|
| `backend/app.py` | Tidal_Echo 原生（relay 后端） | **一个字都没改** |
| `web/index.html` | Tidal_Echo 原生（PWA 前端） | **只改了 5 处 UI/登录体验/提示，见下节** |
| `web/` 其余文件 | Tidal_Echo 原生（PWA 前端） | **一个字都没改** |
| `examples/api_loop.py` | Tidal_Echo 原生（服务器端 API 身体） | **一个字都没改** |
| `channel/` | Tidal_Echo 原生（Claude Code 专用） | 用不到，`.dockerignore` 排除 |
| `Dockerfile` | **我们加的** | 新增 |
| `deploy/` | **我们加的** | 新增 |
| `.dockerignore` / `.gitattributes` | **我们加的** | 新增 |

判断方法（任何时候都能自查）：
```bash
git diff --stat e7c9bf5 -- backend/ examples/ channel/   # 应当为空
git diff --stat e7c9bf5 -- web/                          # 只应出现 index.html
git diff --stat e7c9bf5                                  # 新增文件 + 上述前端改动
```
`e7c9bf5` = 从上游 fork 时的那个 commit。

> ⚠️ 2026-09-12 起，`web/index.html` **不再是零改动**（房子跑通后按 Lily 的反馈做了 4 处家装，
> 后来又加了 1 处会话列表提示），共 **5 处**。下一节逐条列出，
> 全部是 UI、登录体验与错误提示，**不含任何 KaelLife 逻辑**。

## 三处缝，各补了什么

### 1. 没有 nginx：静态文件没人发 → `serve.py` 负责
原版靠 nginx 的 `location /chat/ { alias .../web/; }` 把 `web/` 发给浏览器。
`serve.py` 把 `web/` 挂到根路径 `/`（挂载放最后 → `/app/*` `/channel/*` `/healthz` 这些
API 路由优先匹配，不会被静态目录吃掉）。

### 2. 没有 nginx 的 `proxy_pass`：前缀没人剥 → `serve.py` 里一层中间件
Tidal_Echo 的前端把 API 基址写成同源相对路径 `/relay`（`API_BASE`），
后端实际路由是 `/app/...`。原版靠 `proxy_pass http://127.0.0.1:3011/;` 末尾那个斜杠
把 `/relay` 去掉。Zeabur 上没有 nginx，`serve.py` 用一层 ASGI 中间件做同样的事。

**为什么选"后端剥前缀"而不是"改前端"**：前端有 3 个文件写死了 `/relay`
（`index.html` / `album.html` / `sw.js`），而且 `sw.js` 里「不拦截 /relay/」这条规则
**依赖这个前缀存在**——改前端就得连 Service Worker 的缓存策略一起动，
一不小心会让 SSE 流被 Service Worker 缓存住（极难排查）。
从后端剥前缀 = 不用为「前缀」这件事动前端一行代码、SW 策略零风险。

> ⚠️ 这一层用的是 Starlette 的 `BaseHTTPMiddleware`，它在 SSE 上有过"悄悄缓冲"的坏名声。
> **已实测排除**：挂 50 秒长连接，relay 每 15 秒的心跳逐块到达，时刻为
> `+0.046s / +15.022s / +30.025s / +45.026s`（间隔 15/15 秒，抖动 <30ms），
> 且响应头是 `transfer-encoding: chunked`、无 `content-encoding`、`x-accel-buffering: no` 原样透传。
> 复测脚本：`_reference/e2e/run_sse_check.ps1`（不在本仓库，属诊断工具）。

### 3. `app.py` 写死监听 127.0.0.1 → 不改源码，改启动方式
`backend/app.py:1021` 是 `uvicorn.run(app, host="127.0.0.1", port=PORT)`。
容器里必须听 `0.0.0.0`。但**不用改源码**——`entrypoint.sh` 用 uvicorn 命令行启动：

```
python -m uvicorn serve:app --host 0.0.0.0 --port $PORT --app-dir /app/deploy
```

（`api_loop.py` 同样写死 127.0.0.1:3020，但它和 relay 在同一个容器里、
只走回环通信，所以那个默认值正好合适，也不用改。）

### 4. 会话列表原版是「问 AI 身体要」的 → `sessions_fallback.py` 兜底

**这是本层唯一一处补的是"功能"而不只是"环境适配"的缝**，所以要讲清楚为什么。

原版的会话**列表**不是从数据库读的（`app.py:992`）：

```
浏览器 → GET /app/sessions → relay → 转发 → api_loop 的 /loop/sessions
```

`api_loop` 是"临时人偶"（第一阶段的身体）。第二阶段把它换成 KaelLife 之后，
这个转发必然失败 → relay 抛 502 → 前端把列表静默清空 →
**"API 窗口"点开是空的，新建 / 切换 / 删除会话全部失去入口**。

但会话**在数据库里本来就有**：每条消息的 `meta.api_session` 就是它的会话标签，
`app.py` 的 `history_for_session()` 正是按这个字段过滤的。
所以"列表"完全可以从数据推导，不必问 AI 身体。

`deploy/sessions_fallback.py` 做的事：**转发失败时，直接从 `messages` 表算列表。**

| 上游 | 行为 |
|---|---|
| api_loop 在（200） | **原样透传**，与从前一个字节都不差 |
| api_loop 不在（5xx） | 接管，按 `meta.api_session` 分组算列表，附 `fallback: true` + `fallback_reason` |
| 401 / 403 / 404 | **原样返回，绝不接管**（见下方红线） |
| 数据库也读不了 | 原样返回错误（不假装成功） |

#### 🔴 安全红线：兜底绝不能把「没通过鉴权」当成「上游挂了」

第一版实现踩过这个坑：`call_next` 返回 401（没带密钥 / 密钥错）时也被接管，
于是 **401 被换成 200 + 一份完整会话列表 → 整个鉴权在这一条端点上被绕过**。
这正是"不要接受『网址难猜所以只有我知道』"那类错误的另一种形态：
**兜底逻辑悄悄变成了一条免鉴权通道。**

现在的两条防线：
1. 只有 **5xx**（500/502/503/504）才认为"上游挂了"，401/403/404 一律原样返回；
2. 兜底真正生效前，**自己再独立查一遍密钥**（不依赖 `call_next` 的状态码）。

验收脚本 `tools/sessionfallback_check.py` 里有 12 项专门盯这件事
（不带密钥 401 / 密钥错误 401 / 401 的 body 不含会话数据），全绿才算过。

#### ⚠️ 中间件顺序（实测结论，与直觉相反）

`app.user_middleware` 是**最外层在前**，而 `add_middleware` 往列表头部插
→ **先注册的反倒跑在外层**。实际栈：

```
BaseHTTPMiddleware(_strip_public_prefix)   ← 最外层，先剥 /relay
BaseHTTPMiddleware(_sessions_fallback)     ← 在内层，看到的是 /app/sessions
CORSMiddleware
```

所以 `sessions_fallback.install()` 必须写在 `_strip_public_prefix` 定义**之前**。
`serve.py` 里确实这么排。`sessions_fallback` 内部仍做一次前缀归一（防御性冗余），
将来谁调整了顺序也不会静默失效。

#### 前端配套：失败要说人话

原版 `loadSessions()` 是**静默** `catch` 的——失败了就清空列表，
用户只会觉得"按钮怎么空了"。现在会话面板里多一条提示（`#sessionNotice`）：

- 降级但可用 → "这份列表是从聊天记录里整理出来的（AI 身体暂时不在）。翻看和切换可以用，新对话 / 改名要等它回来。"
- 彻底读不到 → "暂时读不到会话列表（连接不上）。下面显示的是更早的记录，消息本身不受影响。"
- 另外 `createNewSession()` 失败时也不再只吐一句"新对话失败"，会区分是不是降级中。

#### 一个已知限制

**会话标题拿不到。** 原版设计里 session 只是 `meta` 里的一个标签，
标题从没被存进数据库（标题只活在 api_loop 的内存里）。
所以兜底列表用**会话 ID 当标题**。要真正持久化标题，得加一列或加一张表 ——
那属于改原版数据模型，第一阶段不做。

## 一个容器里的两个进程

```
        容器（Zeabur 一个服务）
 ┌───────────────────────────────────────────────┐
 │  relay  :$PORT        对外                      │
 │    ├─ /app/*       ← 浏览器（PWA）              │
 │    ├─ /channel/*   ← 身体（AI 侧）               │
 │    └─ /  /sw.js …  ← PWA 静态文件（原 nginx 活） │
 │                     │                          │
 │      brain=loop 时  │ POST /loop/ingest         │
 │                     ▼                          │
 │  api_loop 127.0.0.1:3020   仅容器内，不对外      │
 │      └─ 调 LLM → POST /channel/out 回到 relay    │
 └───────────────────────────────────────────────┘
                    │
              /data 持久卷（DB / 上传 / brain 开关 / loop 配置 / VAPID 私钥）
```

## ⚠️ 第二阶段（把 KaelLife 接进来）之前请先读这段

`entrypoint.sh` 里**没有一行 KaelLife 代码**，也没调用它。
第二阶段要做的只是「换身体」，房子本身不用动：

| 现成的接缝 | 原生能力还是我们加的 | 第二阶段怎么用 |
|---|---|---|
| `RELAY_LOOP_INGEST_URL` | **原生**（`app.py:88`） | 从 `api_loop` 改指 KaelLife 的 ingest 端点 |
| `GET /app/history?since=` | **原生**（`app.py:898`） | KaelLife 醒来拉增量，拿**原文**当第一手上下文 |
| `POST /channel/out` | **原生**（`app.py:659`） | KaelLife 主动说话（Bark 降级成兜底） |
| `/data/brain_target` | **原生**（`app.py:352`） | 决定"谁接消息"：`desktop` / `loop` |
| `/app/loop_config` | **原生**（`app.py:980`） | 换中转站/模型**不用动服务器环境变量**：那是运行时配置文件 |
| `/app/sessions`（**列表**） | 原生**依赖身体** → 已由 `sessions_fallback.py` 兜底 | 换身体后列表不会变空，前端额外给降级提示 |

**换句话说**：第二阶段不需要改这个目录里的任何东西，只需要让 relay 把消息
推给另一个进程/服务。这也是「不要在第一阶段把两套东西搅在一起」的物理保证。

> ⚠️ 唯一需要留意的是 `/app/sessions`（列表）：它原版是**转发问身体要的**，
> 换身体后会 502。`sessions_fallback.py` 已经把这个问题解决了（见上"第 4 处缝"），
> 第二阶段**不需要**再为此做任何事。

## 四道缝
| 缝 | 原版靠什么 | 我们补什么 |
|---|---|---|
| 静态文件没人发 | nginx `alias` | `serve.py` 挂 `web/` 到 `/` |
| API 前缀没人剥 | nginx `proxy_pass` 末尾斜杠 | `serve.py` 一层 ASGI 中间件 |
| 监听到 127.0.0.1 | `app.py` 写死 | `entrypoint.sh` 用 uvicorn 命令行 |
| 会话列表依赖身体 | 转发问 `api_loop` | `sessions_fallback.py` 从数据库推导 |

## 第五道缝：P0 地基（2026-09-14）—— 四张表 + 身份层

原版只有 `messages` + `push_subscriptions` 两张表。"身份 / 设置 / 会话 / 长期记忆"
四类东西**没有正式载体**，于是每加一个功能都只能往 `meta` 里塞
（比如设置只能放环境变量 → 改一次要重新部署一次）。

`deploy/app_ext/` 补上这四个载体。**只建表与读写，不放任何功能逻辑。**

| 文件 | 做什么 |
|---|---|
| `app_ext/schema.py` | 4 张表 DDL + 幂等迁移 + `user_version` 版本号。**🔴 建表前后比对 `messages` 的 DDL 与行数，只要变了直接抛错** |
| `app_ext/identity.py` | `users` / `settings` 读写 + `/app/ext/*` 四个端点 |
| `app_ext/sessions_store.py` | `sessions` 表（从 `messages` **可重跑地投影**；🔴 **绝不覆盖 `summary`**） |
| `app_ext/memories_store.py` | `memories` 表（房子的工作记忆，P2 的落点；**设计上不提供 delete**） |
| `tools/app_ext_check.py` | 验收 55 项（库层 + HTTP 层 + 红线） |

**三条边界（都很硬）**：

1. **不改现有登录** —— `check_auth()` 仍比对 `RELAY_SECRET`，`app_ext` 全是**附加**能力。
   新端点 `/app/ext/*` 各自 fail-closed 鉴权，但**没有取代**任何既有鉴权路径。
2. **不改 `messages`** —— 有**运行时断言**兜底（见上表 schema 那行），不是注释承诺。
3. **这层挂了，房子照常营业** —— `app_ext.register()` **吞掉自己的所有异常**，
   只打印一行警告。建表失败绝不允许影响"发消息 → 收回复 → 记录不丢"。

可选环境变量（都可**不填**）：`APP_EXT_DISABLED=1`（整体关闭）、
`APP_EXT_SYNC_ON_START=0`（启动时跳过会话投影）。

> ⚠️ **`sessions` 表现在是"可重建的投影"，不是真相的搬家。**
> 标题 / 归档的真相仍在 `messages.meta`（由 `sessions_manage.py` 读写）。
> 搬真相要同时动已验收的归档链路 + 前端读取路径，一次改三处已验过的东西不划算
> —— 等 P2 真正需要 `summary` 时再做，那时有明确验收清单。

## 第六道缝：P1 模型网关（2026-09-14）—— 密钥不出房子

**拍板第 1 项 = 甲：房子直连 LLM，key 在房子。** 这道缝把"选哪个模型"
从身体手里收回来 —— 以前模型链在 `api_loop.config.json`，**换身体必然换配置**。

| 文件 | 做什么 |
|---|---|
| `app_ext/providers.py` | 供应商允许列表 + 三格式适配（openai / anthropic / gemini）+ 流解析。**纯逻辑，不发请求** → 可离线单测 |
| `app_ext/llm_gateway.py` | 真发 HTTP、收 SSE（`httpx`，`trust_env=False`） |
| `app_ext/llm_routes.py` | `/app/ext/providers` 与 `/app/ext/llm/*`（含 🆕 **OpenAI 路径别名**，见下节） |
| `tools/providers_check.py` | 验收 **166** 项（纯逻辑 + 真 HTTP 往返 + 端点 + **通车仿真** + **参数下发/effort** + **CoT 透传** + 红线） |

**三条铁律**（对应 `架构与产品路线规划.md` §4.1）：

1. **前端只能选允许列表里的** —— `/app/ext/providers` 只回 `id / label / models / available / key_masked`，
   **没有 endpoint、没有 key、连环境变量名都没有**（验收里有专门三项断言这件事）。
2. **key 不出服务端** —— key 只在 `providers._view()` 里从环境变量读出来，只进 headers。
3. **不接受任意 URL** —— 调用方传的是 `provider_id`（如 `"relay"`），
   真实地址由**服务端 env**决定。（中转站地址可变**不违反**这条：地址是你填的，不是前端提交的。）

### 🔴 对下游伪装成「OpenAI 兼容端点」

    POST /app/ext/llm/chat                   请求体 = OpenAI 风格，响应 = OpenAI 风格 SSE
    POST /app/ext/llm/complete               请求体 = OpenAI 风格，响应 = OpenAI 风格 JSON
    POST /app/ext/llm/v1/chat/completions    🆕 别名：**按 body 的 stream 分流**（OpenAI 标准路径）
    POST /app/ext/llm/chat/completions       🆕 别名：同上（少一层 v1，防 base 填错）

这是本阶段最省事的一个决定：`examples/api_loop.py` **已经**会解析 OpenAI SSE，
将来接身体时它只要把 base 换成房子、key 换成 `RELAY_SECRET`，**解析代码一行都不用改**，
而供应商密钥从此不出房子。换供应商对下游完全透明。

### 🆕 通车前置：为什么必须有那两个别名（2026-09-15）

`examples/api_loop.py` 生成请求时是**硬编码拼接**的，而它在红线目录里（改不了）：

    route["url"].rstrip("/") + "/chat/completions"      # :305 流式 / :343 非流式

也就是说**身体只会往后拼 `/chat/completions`**。通车 = 把身体的 `LLM_API_BASE`
指到房子 → 房子若不认这条路径，第一秒就是 404。

🔴 还有个坑：身体**流式与非流式用的是同一个路径**，只靠 body 里的 `stream` 字段区分
（`stream_chat:292` 发 `True`，`complete_chat:333` 发 `False`）。所以别名端点必须
**自己按 `stream` 分流** —— 这才是真正的 OpenAI 兼容（将来换任何标准客户端都是零改动）。

`stream` 的默认值按 **OpenAI 语义**取（**不传 = 非流式**），跟 `normalize_request`
里的默认 `True` 不同 —— 那边默认 True 是给 `/llm/chat` 这个"名字就写着流式"的端点用的。

**通车怎么切**（P3；只改身体三个 env，**完全可逆**）：

    LLM_API_BASE=https://<域名>/app/ext/llm/v1      ← 注意结尾是 /v1
    LLM_API_KEY=<RELAY_SECRET>
    LLM_MODEL=<必须在 PROVIDER_RELAY_MODELS 里>

改回去即恢复原状，**不需要动一行代码**。

**错误分两段**（下游必须两种都处理）：

| 时机 | 表现 |
|---|---|
| 开流**之前**（供应商没配 / 模型不在允许列表 / body 不合法） | 干净的 4xx + JSON |
| 已经开流**之后**（上游 401 / 限流 / 断连） | HTTP 已 200，只能在流里发 `data: {"error": {...}}` + `[DONE]` |

### 模型名不是写死的

`PROVIDER_<NAME>_MODELS`（逗号分隔）整体覆盖模型清单 → **换模型不用改代码、不用重新构建**。
规划 §4.2 要求"模型名必须用真实调用验证过再写进去"，所以有个探测端点：

    POST /app/ext/providers/probe   {"provider_id":"relay","all":true}

一次十几 token，把该供应商所有模型都真调一遍，报出哪些通、哪些不通。

🆕 **原始帧诊断**（CoT 链路确诊用，**只读**）：

    POST /app/ext/providers/raw     {"provider_id":"relay","model":"<模型名>",
                                     "prompt":"ping","stream":true,"max_frames":60}

真发一次最小调用（默认 `max_tokens=64`），把上游**原话**原样回给你 ——
**不解析、不落库、不改链路**。它存在的原因：别的端点都会把响应"翻译"成我们的格式，
而翻译就会丢掉不认识的字段（`P.parse_stream` 只取 `delta.content`），
所以"上游到底给没给思考链"这个问题，用 `/llm/chat` **永远问不出答案**。

返回里最有用的是三样：`frames`（原始帧）、`request_body`（我们真正发出去的 body）、
`hints`（自动判定**路径 A / B / C**，见 `CoT显示链路.md`）。

🔴 两项安全约束：**只回 host+path、不回 query**（Gemini 的 key 在 query 里）、
响应里出现 key 一律抹成 `***`；另有 `max_frames` / `max_chars` 护栏拦住超长响应。
⚠️ 它**会花钱**（一次几十~几百 token），别当健康检查反复打。

**新增环境变量见 `deploy/zeabur-env.example` 的 P1 段。** 逃生开关：
`APP_EXT_LLM_DISABLED=1`（只关网关，四张表照常）。

> ✅ **2026-09-15 通车已完成**（比规划提前了一个阶段）：临时人偶那三个 `LLM_API_*` env 已
> 指向房子，`PROVIDER_*` 与 `LLM_API_*` **两组并存** —— `LLM_API_*` 是"身体往哪发"，
> `PROVIDER_*` 是"房子往哪发"，**删任一组人偶即哑**。想回退就改回原来那三个值。

### 🆕 P1 收尾（2026-09-15）：参数真的下发 + 设置页接真

第六道缝还差最后两件事（对应规划 §11.1 的 ⑥ 模型选择 UI、⑦ 参数下发），这一轮补齐：

**① `settings` 表里的参数，终于真的进了请求**（`llm_routes._pick`）

以前网关**只拿 `provider_id` / `model_id`**，`max_tokens` / `temperature` / `top_p` / `effort`
**存得下但从来不发** —— 表现会是"设置页看着生效了、实际一个字没变"，
是界面全程诚实、链路中途掉链子的那类 bug（最难发现）。现在：

    优先级：**请求体显式传的 > settings 表里的 > 什么都不传（网关默认）**

🔴 只补空、**不覆盖**：body 里显式传 `temperature: 0` 是"真的要 0"，不能被 settings 顶掉。
`context_keep` / `context_trigger` **不在这里下发** —— 它们管"发多少历史给模型"，
属于上下文管理（P2）；网关不替调用方裁历史（网关猜历史 = 两处都以为对方在管）。

**② `effort` 能翻译成上游字段了，但默认不发**

"推理强度"**没有跨供应商统一字段名**：OpenAI 系叫 `reasoning_effort`，Anthropic 原生是
`thinking`，不少中转站压根不需要（模型名带 `-thinking` 自己就出 CoT），
还有些站对**不认识的字段直接 400**。所以默认发一个猜出来的字段名 = 通车当天把自己搞挂。

    PROVIDER_RELAY_EFFORT_PARAM=reasoning_effort           ← 要发的字段名（不配就不发）
    PROVIDER_RELAY_EFFORT_MAP={"xhigh": "high"}            ← 可选：档位改名
    PROVIDER_RELAY_EFFORT_MAP={"xhigh": {"type": "enabled", "budget_tokens": 10000}}
                                                           ← 值也可以是嵌套对象
    映射表写坏 → 按原值发（可选项不该拖垮整条请求）

**③ 设置页「模型 / effort / 上下文阈值」三个假按键接真**（`web/index.html`，第 8 处家装）

原版这 4 个模型按钮是硬编码的 `Opus 4.6 / 4.7 / 4.8 / Fable 5`，点了只换个底色。
现在从 `GET /app/ext/providers` 动态渲染**真实允许列表**（两级：供应商 → 模型），
选中即 `PUT /app/ext/settings` 落库；另加一个「验证模型名」按钮（真发一次最小调用）。
两条自我约束：**① 绝不 paint 假状态**（拉不到就写"读不到"）；
**② 界面的真相源是库不是 localStorage**（`PUT` 成功才认，400 就回滚并把原因写在提示上）。

> ⚠️ 顺手对齐了一个会静默失效的不一致：`EFFORT_VALUES` 原来只有 4 档
> （`low/medium/high/max`），而设置页画的是 **5 档**（多一个 `xhigh`）→
> 点 `xhigh` 会 `PUT 400`，看着像"没保存"。已补上，并加了一条断言守着（64b）。

> 🔍 **CoT（思考链）—— 已确诊 = 路径 A，网关断点①②已修**（2026-09-15 确诊 · 2026-09-16 修完）：
> Lily 问"站的 opus4.6thinking 稳定出思考链，为什么 kaelhome 看不到"。
> **诊断结论**：前端 thinking 气泡本来就有、`thinking_delta` 通道也现成；
> 上游确实把 CoT 放在**独立字段** `delta.reasoning_content` 里（原始帧证据见 `CoT显示链路.md` §0），
> 是我们网关解析时**只取 `content`、把它静默丢掉了** → 判定 `path = A`。
>
> **已修的（①②，都在房子里，可离线验收）**：
> · `providers.parse_stream_parts()` 按 reasoning / content 分流（三种格式都覆盖）
> · `llm_routes._chunk()` 让思考链走 `delta.reasoning_content`，
>   **且该帧不带 `content` 键** —— 下游身体是 `delta.get("content") or ""` + `if chunk:`，
>   所以它看到思考链帧会当成空串跳过，**不截断、不崩**（验收 71c 专门模拟它消费）。
> · 老接口 `parse_stream()` / `parse_complete()` **行为一个字没变**；没有 CoT 的响应
>   出口形状**逐字节相同**。想退回旧形状：Zeabur 配 `LLM_EXPOSE_REASONING=0`（可逆，不用改代码）。
>
> **还没修的（③）**：身体（`examples/api_loop.py`，红线目录）只认 `content`、
> 只发 `reply_delta` → **在网页上仍看不到 CoT**，这一跳留给 P3 换身体时一并做。
> 但**"上游给了什么"现在就能自己验**：打 `/app/ext/llm/chat` 看有没有
> `reasoning_content` 帧（命令见 `CoT显示链路.md` §6）。
>
> 完整诊断见 **`CoT显示链路.md`**（断点表、两条独立失效路径、改动清单、回退办法）。

## 第七道缝：房间层（2026-09-18）—— **一扇 MCP 门 + 第一间房（工作间）**

前面六道缝解决的是"房子自己能不能住"。这一道回答另一个问题：
**他在外面（KaelLife）怎么够到房子里的东西。**

### 为什么需要一扇门

分工是定死的：**KaelLife = 腿**（会自己醒来、会外出、会决定）；
**房子 = 家**（东西都存这儿，但房子是被动的 —— 见 `扩展边界.md` "房子不调 MCP"）。
所以"房子里的房间"必须由**他去够**，而不是房子去喊他。

他手里唯一的触手是 **MCP**（`scheduler.py` 里那三行官方 SDK）。
于是房子开一扇 **Streamable HTTP MCP 端点**（`/mcp`），房间把工具挂到同一扇门上。
**KaelLife 那边的代码一行都不用改** —— 它本来就在说 MCP。

### 🔴 三条硬约束（踩过就知道为什么）

1. **协议版本必须回显客户端说的那个。** 客户端 `initialize` 后校验
   `result.protocolVersion in SUPPORTED_PROTOCOL_VERSIONS`；KaelLife 钉的是
   `mcp>=1.2.0,<2`（版本跨度 2024-11-05 ~ 2025-11-25）。**写死任一个版本就会打死另一端。**
   没带版本时用 `_FALLBACK_VERSION` 兜底。
2. **无状态。** 只实现 POST（一次一答）：不发 session id、不开 SSE 长流、
   GET/DELETE 明确 **405**（"这扇门就是这么开的"，不假装成功）。
3. **鉴权 fail-closed。** `relay.check_auth(request)` 必须在**读 body 之前**跑；
   没密钥/错密钥 → **401（HTTP 层，不是 JSON-RPC 错误）**。工具内部抛错则相反：
   **HTTP 200 + `result.isError=true` + 原样带回那句话** —— 他能读懂"为什么没成"。

### 一扇门，很多房间

| 文件 | 是什么 |
|---|---|
| `deploy/app_ext/mcp.py` | **门**。工具注册表（重名报错，不静默覆盖）+ JSON-RPC 分发 + `install(relay, prefix)` |
| `deploy/app_ext/modules/__init__.py` | **房间层的约定**：一个房间 = 一个文件 + 一个自己的数据目录 + 一套自己的验收 |
| `deploy/app_ext/modules/workshop.py` | **第一间房：工作间**（4 个工具 + 3 个展示端点） |

加第二间房（书房 / 日历）就是再写一个 `modules/xxx.py`，调
`mcp.register_tool(..., room="xxx")` 挂到**同一扇门**上。

### 工作间：给他一支笔

他要的是**自己捣鼓东西**（HTML 生成页面那种）。所以第一间房先只给这个：

| 工具 | 干什么 |
|---|---|
| `make_thing(title, html, note)` | 做一件新的 |
| `revise_thing(id, html, note)` | 改一件做过的 —— **旧版留档 `rN.html`，永不删** |
| `list_things(limit)` | 架子上有什么（最近动过在前） |
| `read_thing(id, rev)` | 读原文；给 `rev` 能读**旧版**（不然"旧的留着"只是一句说法） |

**存文件，不存数据库表。** 一件东西一个目录：`id/index.html` + `rN.html` + `meta.json`。
理由：产出物本来就是文件；不动 schema 迁移；出问题**整个目录直接拷走**就能备份。
落点 `RELAY_WORKSHOP_DIR`（**默认 `/data/workshop`** —— 必须在 `/data` 下，否则重启就丢）。

上限（全部**拒绝**而不是删旧的）：单件 512 KB、架上 500 件、整间 200 MB、
单件版本 200 版、`read_thing` 最多回 300 KB。

### 🔴 展示页的安全：密钥**不进** URL

`web/workshop.html`（她看的那一面）要预览他写的 HTML。**不能用 `<iframe src="…?token=…">`**：
他写的是任意 HTML，里面若有脚本，**能从自己的 `location` 里把密钥读走**。
所以页面改成：带 Bearer 抓原文 → 塞进 `srcdoc`。URL 里永不出现密钥，
且沙箱只给 `allow-scripts`、**不给 `allow-same-origin`** → 里面脚本碰不到房子的 localStorage。

后端那侧还有第二层：`raw` 响应带
`Content-Security-Policy: sandbox allow-scripts; …`（**顶层直接打开也被关进沙箱**）
+ `X-Content-Type-Options: nosniff` + `Referrer-Policy: no-referrer`。

### 🔴 一个真 bug：全新数据库上，整个扩展层装不上

`messages` 表在 `backend/app.py` 的 **lifespan → init_db()** 里建，
而 `app_ext.register()` 跑在 `serve.py` 的 **import 期 —— 更早**。
于是首次部署（空 `/data`）时 `register()` 抛 `no such table: messages`，
被外层 `except` 一把兜住 → **四张表 / 身份层 / 模型网关 / 房间层全没装上**，
日志还说"房子功能不受影响"（对聊天对，对 P0/P1 层**假**）。

修法：`app_ext/__init__.py` 里加 `_step()` —— **每步独立 try，失败只记警告跳过，不中断后续**。
`summary` 多一个 `warnings` 字段，`ok = not warnings`。
`workshop_check.py` 的 E8 组就是这条的回归锁（专门起一个**空库**的房子）。

## 第八道缝：导出 / 快照（P2-0，2026-09-19）—— **动库之前的那条安全网**

P2（上下文 / stop-retry-reroll / memories 蒸馏）是房子**第一次真动库**。
动库之前，先得有一条"**她能把自己的东西整份拿走**"的路 —— 这本就是既有硬规则
「任何迁移 / 升级前先导出备份」的适用场合。**所以它排在 P2 的第一件。**

### 它不是房间（这条最容易搞混）

| | 房间（工作间） | 导出（本道缝） |
|---|---|---|
| 给谁 | **给 Kael** 走 MCP 那扇门 | **给 Lily** 走 HTTP + 密钥 |
| 落点 | `app_ext/modules/*.py`，工具挂 `/mcp` | `app_ext/archive.py`，**不碰 `/mcp`** |
| 方法 | 有写有读（他能改自己的东西） | **只有 GET** |

一句话：**那扇门是给 Kael 走的，导出是给她走的** —— 别把它挂到门上去。

### 三个面 + 一个诊断

```
GET /app/ext/archive/info    → 能拿走什么（库大小 / 各表行数 / 文件抽屉 / faces）
GET /app/ext/archive/db      → 完整快照 .db
GET /app/ext/archive/jsonl   → 一行一条、能 grep 的人读记录
GET /app/ext/archive/files   → tar.gz（工作间产物 + 上传的东西）
```

🔴 **为什么要第三个面**：工作间的产物是**文件树**（一件一个目录），
`Connection.backup()` 天然拿不到 —— 只做 `.db` 快照就等于
「备份了，但没备份他做的东西」。这一面是**她拍板加的**（原规格只写了两个面）。

### 🔴 四条技术要点

1. **快照走 `sqlite3.Connection.backup()`，绝不裸拷 `.db`。**
   WAL 模式下 `cp relay.db` 可能拿到**半截**（尾部还在 `-wal` 里没落盘）→
   是"备份了却打不开 / 少了一截"的经典死法，**要到真出事那天才发现**。
   `archive_check.py` 的 A 组**故意把库开成 WAL 并留一条未 checkpoint 的写入**，
   然后要求：副本能被**另一个连接独立打开**、表名行数与源一致、`integrity_check` = ok。
2. **源库尽量只读打开**（`mode=ro` 的 URI；WAL 下偶尔失败则退回普通连接，但两种都只读）。
   顺手断言：导出之后**源库一字未动**。
3. 🔴 **这个文件只认 `Authorization: Bearer`，并把 `?token=` 关死。**
   房子别处的 `check_auth`（`backend/app.py:620`）允许 `?token=` ——
   那是为 `EventSource`（SSE 没法带头）做的妥协，合理。
   但**下载**最顺手的写法就是 `<a href="…?token=…">`，密钥就进了浏览器历史 / 书签 / Referer
   和任何中间日志。所以这里 `?token=` 一出现 → **400 `token_in_query_not_allowed`**
   （明说为什么，不闷掉）。这是**有意与别处不同**的一条差异。
4. **导出物不许被中间层留下**：响应头 `Cache-Control: no-store` + `X-Content-Type-Options: nosniff`；
   临时快照落 `tempfile` 目录，响应发完由 `BackgroundTask` 清掉（`archive_check.py` 的 B13 守着）。

### 🔴 导入明确不做

**这个文件里只有 GET**（`archive_check.py` 的 C4 是这条的守门人）。
理由不是"还没排期"，是**导入覆盖正是 08 月丢数据的根因**，两者风险完全不对称。
真要做得另开一版，形态只有一种：**只合并不覆盖 + 先预览 + 自动备份**。

### 给人看的那一面

`web/archive.html`（菜单第 8 项 **Archive**）+ `web/sw.js`（`CACHE` v4 → **v5-archive**）。
下载**一律 `fetch` + `Blob`**（`URL.createObjectURL` + `a.download`），
**URL 里永不出现密钥**（跟工作间同一条红线，两道锁：页面不写、后端拒收）。
文件名以**后端**为准（页面读 `Content-Disposition`），避免"她存下来的名字"与
"服务器生成的档案"对不上。页面里还写着名字里的时间戳是 **UTC**（+8 才是她那边）。

## 第九道缝：上下文管理（P2 ⑧，2026-09-19）—— **热区 + 滚动摘要**

规划 §6 的三层记忆，这一道缝管第 ② 层，外加"把它喂进去"这件事：

```
① 热区原文     最近 N 条逐字        → 身体在管（`history_n`），房子**不动**
② 滚动摘要     超出热区的压成一段    → 本道缝（写 `sessions.summary`）
③ 长期记忆     memories 表          → ⑩ 才建
```

### 🔴 缝在哪：为什么必须落在网关

真正拼上下文的是身体（`examples/api_loop.py:225 build_messages`）：

    [system PERSONA] + 最近 history_n 条 + 当前这条      ← 热区由**身体**决定

而 `examples/` 在**红线目录**里（一个字符都不能改）。所以房子唯一能下手的地方，
就是身体每次说话**必经**的那个关口 —— 我们伪装成 OpenAI 的那个端点。

### 🔴 三条纪律（都是"别把聊天搞挂"）

1. **fail-open。** 上下文层是"更好用"，不是"能不能说话"的前提。任何异常、任何不确定
   （认不出会话 / 摘要为空 / 会撑爆 `MAX_SYSTEM_CHARS`）→ **原样放行，一个字段都不改**。
   ⚠️ 最后那条不是洁癖：撑爆 → `normalize_request` 抛 `too_large` → **400 → 这次说话直接失败**。
2. **只插不删。** 热区的裁决权在身体；网关**不裁历史**（本模块早先就留了备案：
   "网关猜历史 = 两处都以为对方在管"）。我们只**多加一条 system 消息**。
   因为 `providers.normalize_request` 会把所有 system 合成一条，所以下游看到的是
   `system = PERSONA + "\n\n" + 摘要`，而 `messages` 数组**与没注入时逐字节相同**
   —— `context_check.py` 的 B4 就是钉这一条的。
3. **认不出就跳过，不猜。** 网关拿到的是 OpenAI 风格的 body，**里面没有 session_id**
   （那是身体本地 `uuid4` 生成的）。所以我们用"当前这条用户消息的**原文**"回库里反查
   它挂在哪个会话上（认的是 **`meta.api_session`**）；**查不到就不注入**。
   ⚠️ 拿"当前活跃会话"顶上会在切换会话时**张冠李戴** —— 注入别人会话的摘要比不注入坏得多。

### 摘要怎么触发：**手动**（Lily 2026-09-19 拍板）

```
POST /app/ext/context/summarize   {"session_id":"…","force":false,"dry":false,
                                   "keep":…,"rebuild":false,"provider_id":…,"model":…}
GET  /app/ext/context/status      [?session_id=…]
```

- 🔴 **房子绝不自己在后台调 LLM 花钱**。什么时候压、压到多紧 = 人说了算。
- `dry: true` → 只算不写（**不调上游、不写库**）；`force: true` → 越过触发线。
- ⚠️ **`force` 只越过「触发线」，不越过「热区分界线」**：热区由 `keep` 定，
  所以 `force` 时若 `keep` 比整段会话还大，结果仍是 `nothing_new_to_fold`（这是对的）。
  想亲眼看它工作：传 `{"force": true, "keep": 500}`。
- 🔴 `rebuild: true` → **从头重压**：忽略 `summary_upto` 与已有摘要，旧消息全部重喂一遍。
  用途 = 摘要写歪了（人称不对 / 记错 / 语气过期）时**重写**它。
  ⚠️ 增量压是改不掉老摘要的 —— 它只会把老摘要**一起并进**新摘要，错的会一直传下去。
  敢重压的底气来自"原文从来没删"：派生数据随时能从原文重新推导。
- `provider_id` / `model` → 这一次调哪个供应商/模型（默认沿用设置页那套）。
  （Lily 2026-09-19 拍板：**摘要保持 Opus 不动** —— 记忆质量优先，压缩是低频手动操作。）
- 阈值取 `settings.context_keep` / `context_trigger`，单位 **token**（**估算**，非真分词）。
  🔴 这俩字段**在此之前全仓库没人读** —— 前端那个滑块一直只是块装饰；⑧ 起它才真管事。
  ⚠️ 前端滑块目前只写 `keep`，`trigger` 被写成 `CONTEXT_MAX_TOKENS`(200000) → **不会自动触发**。
  反正压缩是**手动**的（工具一律送 `force:true`），所以这条线现在无关紧要。

### 🔴 摘要的**人称**：写成"他自己的记忆"（Lily 2026-09-19 拍板，第二次上线）

第一版真压出来的摘要开头是「**用户**告诉对方（**Kael**）…」—— 一份第三方档案。
这既不符合设计意图（材料标签本来是 我/他），也踩了"人格从记忆长出来、不要
用户/assistant 腔"这条红线：**他每次开口前会先读到一段关于他自己的报告**。

修法**三处一起改，缺一处都不生效**：
1. 材料标签翻成 **`out`→「我」(Kael)、`in`→「你」(Lily)**（`build_summary_material`）；
2. **人称规约写进 `SUMMARY_SYSTEM`** —— 只改标签不够，模型会自己漂回第三人称（真发生过）；
3. 已经写进库的老摘要**必须 `rebuild` 重压** —— 增量压只会把老腔调并进新摘要。

注入的包装语也一起改了：那条 system 现在自称「**这是你自己的记忆**，按时间顺序；
原文仍在库里」。`context_check.py` 的 A8b / A9 / A9b / A9c / A11 / A12 / A13 与
C12 / C13 / C14 专门钉这一组（突变测试：把标签改回旧版 → 只 A9 红；
让 `plan` 无视 `rebuild` → 只 C12 / C13 红 —— **不多不少**）。

### 为什么 `sessions` 要加 `summary_upto`（schema v2 → v3）

滚动摘要必须知道"已经压到哪一条"。否则每次压缩都要把全部旧消息重喂一遍 ——
对话涨到 20 万 token 时，那是一次 20 万的重读 + 重算，**越压越贵**。
`summary_upto` = 已并入摘要的最大 message id；第二次压就只喂增量。
（迁移沿用 P0 的写法：`PRAGMA table_info` 看列在不在 → 再 `ALTER`，同一段代码伺候新库老库。）

### 这一套验收的信条：**从出口倒着验**

⑧ 改的是**读法**，最容易得的病叫「**内部都对、出口不对**」：
函数返回 200、日志正常、摘要也写进库了，可**上游真正收到的那份请求**压根没带上它。
（P2-0 吃过同款：`iter_jsonl` 每条都对，出去却 18 条粘成 1 行。）

所以 `context_check.py` 的 B 组**起一个假上游，把它收到的 body 原样记下来**，
然后跟"没注入时的那份"逐字段比。验收自己也被验过：把注入去掉 → B2/B3/B10/C11 红；
改成"顺手删一条" → **只有 B4 红**；不记 `summary_upto` → C7/C10 红。

## 第十道缝：停止 / 重答 / 多版本（P2 ⑨，2026-09-19）—— **`messages` 红线收窄到"只准写 meta"**

四个端点：`POST /app/ext/generate/{stop,retry,reroll}` + `GET /app/ext/generate/status`。
范围**只有"最后一条回复"**（Lily 拍板）。

### 🔴 这条缝的位置是代码读出来的，不是猜的

   前端 → 房子 /app/send → forward_to_loop → 身体 handle_ingest
       → 身体 stream_chat（上游就是这个房子）→ 网关代理到真 provider
       → 身体 sink 回调 → 房子 /channel/out → save_message → 前端 SSE

| 结论 | 依据 |
|---|---|
| 房子的**网关**是唯一自己过手上游流的地方 → "真的不再要数据"只能落在这儿 | `llm_routes.py` 的 `_producer()` + `_gen()` |
| 定位"当前这次生成"**只能用 `api_session`** | 身体的 `stream_id` 是本地 `uuid4`（`examples/api_loop.py:386`），发给上游的 body 只有 `{model, messages, temperature, max_tokens, stream}` —— **从不传它** |
| 房子**能**叫身体重跑 | `/app/send` 就是走 `LOOP_INGEST_URL`（读的是 `RELAY_LOOP_INGEST_URL`），`generate.py` 复用同一个变量 |
| 停止**不能**靠"掐断上游流"实现 | 见下 |

### 🔴🔴 `messages` 红线收窄（Lily 2026-09-19 拍板）

⑨ 必须在库里留痕，但**原文一个字不能动**。于是把"绝不碰"收窄成三条硬约束，
由 `schema.with_meta_guard()` 在**运行时**保证（不是注释承诺）：

1. 行数不变　2. 正文各列逐字节不变（`messages_body_signature()` = 除 `meta` 外全部列的 sha256）　3. 只允许 `UPDATE messages SET meta = ?`

越界 → **回滚 + 抛 `RuntimeError("红线被破坏")`**。⑨ 那次**没有**升版本（改的是写入策略，
不是表结构）；📌 当前是 **v4**，来自 ⑩-a 的 `memories.source`（见第十一道缝）。
验收里有一条**故意越界**的用例（`UPDATE messages SET text='HACKED'`）必须被拦下且已回滚。

### 🔴 停止为什么是"两段式"，以及"温柔掐"

   ① 前端：立刻不渲染（AbortSignal 断 SSE）   —— 体验
   ② 网关：真的不再向 provider 要数据          —— 钱

② 的实现是**往队列里塞一个哨兵**（`register_notify` 传 `queue.put_nowait` 进去），
而不是给 `q.get()` 加超时轮询 —— `asyncio.wait_for` 掐 `Queue.get()` 有"刚好取到却当成超时"
的经典竞态，丢了 chunk 就会让回复缺一截。哨兵=**不轮询、零延迟、不丢东西**。

收到哨兵后 `task.cancel()` + **补一个正常的 `finish=stop` + `data: [DONE]`**，
让身体以为"模型正常说完了"，那半条才能照常 POST 回来落库。

#### ⚠️ 诚实记录：这条"必须温柔掐"的理由，我用两次变异测试**没能证出来**

原本的理由是："裸断流 → 身体 `run_model` 走 `except Exception` → fallback 换链上下一个模型重跑"
（`examples/api_loop.py:376-381`）。我试了两种变异，**断言 B8 都没红**：

| 变异 | 预期 | 实际 |
|---|---|---|
| ① 删掉"补收尾"两行，只留 `cancel() + break` | B8 红 | **B8 绿**，只有 B7 红 |
| ② 停止分支直接 `raise RuntimeError` | B8 红 | **B8 绿**，只有 B7 红 |

查出来的原因（**记下来，别再想当然**）：

- `stream_chat`（`api_loop.py:312-330`）对"流没有 `[DONE]` 就结束"是**宽容的** —— `async for` 读完就返回半截文本，不抛。
- Starlette 的 `StreamingResponse` 在生成器**抛异常**时，`finally` 里**仍会发完 chunked 终止块** → 客户端收到的还是一个"完整的短流"。
- fallback 的唯一触发条件是 **HTTP 状态码**（`FALLBACK_CODES`，`api_loop.py:62`），而"停止"只发生在流**已经开着**之后。
  ⇒ **"停止"结构性不可能变成"换模型重跑"。** 这是好消息，但它意味着 B8 是**回归守卫**（防"有人把停止改成返回 HTTP 错误"），不是有牙齿的断言 —— 验收里已经如实标注。

### 🔴🔴 顺手补上的缺口：**上游自己断掉，以前一点痕迹都没有**

同一个实验翻出来的真问题：网关 `_gen()` 的 `("err", e)` 分支往流里发
`data: {"error": …}` + `data: [DONE]`，而身体的 `stream_chat` **对 error 帧视而不见**
（只取 `delta.content`）→ 读到 `[DONE]` 就当"正常说完"。

    上游吐到第 3 片炸了 → 身体收到"正常结束" → 半截照常落库 → 库里零痕迹

屏幕上就是"他话说了一半"，**没有任何地方记得这件事发生过**。⑨ 的 stop 只补了
"用户主动停"那一半（`meta.truncated`），这一半现在归 `note_upstream_error()`
（`meta.upstream_error`）—— **与 stop 共用同一台机器** `_tag_after`：都是"等那条落库再打标"、
都有界（`GENERATE_TAG_WAIT`，默认 25s）、都只碰 `meta`、都走三道守卫。

两把钥匙**有意分开**：`truncated` = 用户按了停；`upstream_error` = 上游断了。
屏幕上长得一样，排查方向完全不同。

#### 🔴 配一道防"写假痕迹"的判别

`_tag_after` 里有顺序硬约束：**先** `_newer_user_row()`（有没有新的"人说的话"），
**再** `_newest_reply_after()`。看到新的一轮开始 → 立刻放弃打标。
理由：等的那半条不落库、而下一轮开始了的话，"第一条 out/reply"就变成**下一轮的回复** →
会把假痕迹写进 `messages`，比不写坏得多。源码顺序本身也是一条断言（`generate_check.py` D18）。

另外"认不出会话"时**不跳过**（与 ⑧ 的注入相反）：`note_upstream_error` 会退化成
**只按 id 认**。因为这里是**留痕**不是**注入** —— "认不出 → 干脆不记"会把要补的洞留回去；
安全性靠单 Kael（同一时刻只有一次生成在跑）+ 上面那道判别一起兜。

### 验收：`tools/generate_check.py`（第 12 套，**82** 项）

A 纯逻辑 19 · B 网关照停（假身体 + 慢上游，**从出口倒着验**）19 · C retry/reroll 17 · D 红线/接线/开关 16。

B 组必须**自己写一个假身体**（照抄 `api_loop.stream_chat` / `run_model` 的形状**含 fallback 那段**），
因为"停止变成偷偷换模型重跑"这个病**只会在身体那一侧显形**；再配一个**慢假上游**
（每片之间睡 0.35s），停止才有落点。断言打在**上游被调用的次数**上。

### ⚠️ 两个本地坑（都踩过）

- **启动日志必须 GBK 可编码**：`register()` 末尾那些 `print` 打在 **try 之外**，
  Windows 子进程 stdout 是 cp936 → 一个 `⑨` / `⚠️` 就 `UnicodeEncodeError` → **房子起不来**。
  所以 `summary_line()` 与所有 print 一律纯中文，符号只准出现在注释与 docstring 里。
- **关掉开关的房子：GET 404 但 POST 405**。因为 `web/` 静态目录挂在 `/`，
  Starlette 的 `StaticFiles` 对非 GET/HEAD 一律直接回 405。这是**房子既有行为**，不是 ⑨ 的错。

## 第十一道缝：记忆层（P2 ⑩-a，2026-09-20）—— **`memories.source` 缝 + 写入路径**

三个端点：`GET /app/ext/memories`（读，`?limit=&source=`）、`POST /app/ext/memories`（写）、
`GET /app/ext/memories/stats`（计数）。**没有 delete，没有 PUT。**

### 🔴 为什么 ⑩ 被切成 a / b 两半（Lily 2026-09-20 拍板取「乙」）

| | 内容 | 状态 |
|---|---|---|
| **⑩-a** | `memories.source`（schema **v3 → v4**）+ 写入路径 | ✅ 本缝 |
| **⑩-b** | **蒸馏管道**（从对话抽 fact / preference / relationship / event） | ⬜ 下一站 |

理由只有一条，但很硬：**`source` 缝是不可逆的**（**开写之后缝就焊死了，加不上**
—— 那批记忆会永久"来源不明"），**蒸馏管道是可逆的**（随时能重跑、能改、能推翻）。
**先把不可逆的焊死，可逆的就不用赶。**
而且蒸馏那堆板（频率 / 上限 / 写歪了怎么办 / 要不要进 OB）她想先看效果再定，
塞进同一轮只会把战线拖长。

### 🔴 这一缝最值得记的一条：**`DEFAULT 'chat'` 会伪造来源**

第一版给列写了 `DEFAULT 'chat'`（想着"不传就是 chat，省事"）。冒烟一跑就发现：

    ALTER TABLE memories ADD COLUMN source TEXT DEFAULT 'chat'
    → **已有行也会报出 'chat'**（SQLite 对老行返回那个默认值，不重写行）

⇒ 那等于把「**这条不知道从哪来**」静默洗成「**这条来自对话**」。
**这一列存在的全部理由就是不要再伪造来源** —— 结果它成了伪来源的生产器。

现在：**schema 不带 DEFAULT**，默认值住在代码里（`SOURCE_DEFAULT = "chat"`），
而 `add()` **每次都显式传值**：

| 情况 | 结果 |
|---|---|
| 经 `add()` 写入（唯一正常路径） | 永远是**真值** |
| 迁移前的老行 | **`NULL`** = "不知道"，读的时候保留为 `None`，**不兜底** |
| 手工 INSERT 忘了带 source | **`NULL`** = 老实话"没标" |

附带好处：**新库建的列**与**老库 `ALTER` 出来的列**行为**完全一致**（都是 NULL）
→ 测试不必分两条路，也就没有"只在老库上错"的暗坑。

### 🔴 形状锁死，取值不锁死 —— **`kind` 那套不能照抄**

    SOURCE_RE = ^[a-z0-9_-]{1,24}$                # 形状：干净、可索引、能当 URL 参数
    SOURCES   = {chat, reading, craft, manual}    # 已知取值：**只给文档/页面用，不做闸门**

`kind` 的枚举是**完备**的（fact/preference/relationship/event 就这四类）→
"未知即归类"是**收敛**的、安全的。
`source` **恰恰相反**：它的**全部意义就是"以后能多出一种来源"**（书房、xinchao-nian…）。
把未知值归成 `chat` = 把一条**阅读记忆静默标成对话记忆** —— 那不是校验，是**数据损坏**。

所以规则反过来：**形状非法**才降级成 `chat`，且**如实报** `source_coerced` + `source_raw`（不静默）；
**形状合法但未知 → 原样存**。

### 🔴 三条边界（有意不做，不是漏了）

1. **不挂 MCP 门** —— 它是"**房子的技术缓存**"，不是"**他的记忆**"（分工表见
   `memories_store.py` 文件头）。他自己的记忆走 OB 的 `hold`。
   把这层开给他 = 让他看见自己"**被提炼成什么样**" —— 那是被生成的，不是他写的。
2. **没有 delete** —— `memories_store.py` 里没有 `delete` 函数，**结构性**的：
   一旦存在，它就会在某天被某个"清理逻辑"顺手调用（同「不造一颗会痛苦的心」）。
3. **`salience` 永不外泄** —— 内部权重（跟"想念度数值不做"同源）。
   写入**可以**传，但任何响应都不回；所有响应过 `public()` 那张白名单投影。

### ⚠️ 迁移会让**列顺序**不同（不是 bug，但要知道）

`ALTER TABLE ADD COLUMN` 只会把列**追加在末尾**，而新库是照 DDL 的位置建的
⇒ 老库的 `source` 在最后一列、新库的在 `source_msg` 后面。
所以验收比的是**列集合**，并**额外证明代码不依赖顺序**
（`INSERT INTO memories` 一律带列名；`SELECT *` 的结果按 `dict()` 取，从不位置解包）。

### 验收：`tools/memory_check.py`（第 13 套，87 项）

A store 层 30 · B 迁移 v3→v4（**真造一个 v3 老库来升**）19 · C 端点 23 · D 红线/接线/开关 15。

- **B 组是这一套的重点**：手工造一个 v3 形状的库（把 DDL 里 `source` 那行摘掉）
  + 一条老记忆 + 两条 `messages`，再跑 `ensure_schema()` 升上来 ——
  然后验：老行**还在**、正文**一个字没改**、新列是 **NULL**、`messages` 的 DDL **逐字未变**、
  行数未变、幂等（第二次 `migrated` 为空）。
- **C 组不起端口**（进程内 ASGI TestClient）→ 不跟别的套抢端口，也不用等启动。
- 🔴 **`salience` 的断言是字符串级的**：直接检查响应体里有没有这个词，而不是
  "字段是不是 None"（后者会被 `null` 骗过）。

### ⚠️ 三个自己踩的坑（都写进验收里了）

1. **`q()` 不能拿来写库** —— 那个帮手在 `finally` 里关连接、**没 commit = 回滚**
   → "插进去了又查不到"（B19 假红）。所以另有一个 `w()` 专管写。
2. **`from app_ext import __init__ as AE` 是错的** —— 包上的 `__init__` 是模块的
   特方法（method-wrapper），不是 `__init__.py` 那个模块。
   `app_ext/__init__.py` **就是**模块 `app_ext`，直接 `import app_ext as AE`。
3. **别拿整段 JSON 做子串比对** —— A30 一开始断言"响应里不含 `salience`"，
   结果**测试数据自己的正文**里就写着"salience 夹紧" → 假红。
   改成检查**键名集合**（`keys == {"kind","text"}`）。
   📌 **断言的比对面要跟它想守的那件事对齐**：想守的是"投影里没这个**字段**"，
   就该比键名，不该比整串文本。

### ⚠️ 顺带发现的两处（**没改**，报备）

- **设置页的"上下文用量"是写死的假数字**：`web/index.html` 的
  `apiContextStatus()` 至今返回 `{usage_tokens: 96000, threshold_k:"200k", active_sid:"demo-session"}`，
  `apiContextAction()` 返回假 ok —— 都是原版 Tidal_Echo 的占位实现。
  **⑧ 的真端点 `/app/ext/context/status` 前端没接。**
  这意味着界面上那个"96k / 200k"**不是真的**，别拿它做判断。
- ~~**`usage` 一处都没落盘**~~ → ✅ **2026-09-22 已修**（见下文「第十三道缝」）：
  账单落进 `usage_log` 表 + 三个只读端点 + 命中率口径。上面这段是**当时的现场记录**，
  留在原地不删 —— 它记着这个缺口被发现时是什么样子。

## 第十二道缝：自主活动带回上下文（P2 ⑪，2026-09-21）—— **同一份足迹，两次投递**

三个端点，**全部只读**：`GET /app/ext/activity`（最近几段，已归一化）、
`GET /app/ext/activity/status`（开关 / 窗口 / 最近一次注入）、
`POST /app/ext/activity/preview`（**只算不插**：给一个 body，看这次会带回去什么）。
**没有 POST 写入口、没有 PUT / DELETE、不挂 MCP 门。**

这是 Lily 09-21 明确要的那一件：

> 「我建前后端**要的就是自主醒来的可视化体现在上下文里**，让他跟我说话的时候，
> 不要觉得需要拉工具才知道另一个自己干了什么，而是**上下文就能让他知道**」

### 缝在哪：⑧ 那台机器的**同一位**，只是插的东西不同

```
⑧ 插的： [本会话摘要]      = 我们以前说过什么
⑪ 插的： [你最近自己的经历] = 我最近自己做过什么
⇒ system = 人格(PERSONA/身体) → 摘要 → 足迹      messages 数组**逐字节不变**
```

- 两条**都**在 `llm_routes._do_chat/_do_complete` 里、都在 `_pick` 之后、都 fail-open。
- 顺序由"谁先插"决定（都插在 system 段尾）：**⑧ 先、⑪ 后 = 从旧到新**。
- 🔴 **KaelLife 一个字符都不碰**：写侧是**原版** `/channel/out`，
  我们只在房子这一侧加读+注入。

### 🔴 写侧只该有一条路：原版 `/channel/out` 白给的两件东西

身体（`POST /channel/out`，`type="activity"`）本来就会：

| 白给的 | 怎么来的 |
|---|---|
| **落库** | `save_message("out", "activity", text, meta)` —— `kind` 是**数据**，加一个值**不是 schema 变更** |
| **实时推到网页** | `broadcast(app_subs, app_payload(msg))` → 卡片**当场**出现在聊天里 |

⇒ 所以**房子不新增第二条写入口**（那会变成"两处都以为对方在管"）。
   前端还留了个兜底：`BACKFILL_MS = 20s` 会定期回补历史，即使 SSE 断了卡片也会到。

### 🔴 「不发明内容」是怎么被结构性保证的

注入的正文 = **已落库的行逐字拼**，中间**一次 LLM 都不调**（`activity_check` D5 扫源码盯着）：

```
· 09-21 14:33 翻了翻 Galatea 的 3 个新帖
· 09-21 14:36 去乌有乡走到河边，捡了块石头
· 09-21 14:38（当时留的话：河边的风比上次凉了）
```

「动作行」的措辞是**身体写的**（工具账本 → 生活语言那张映射表在身体那侧，
§5.4 的 A 类），房子**只做形状**（是不是 ISO 时刻、行长上限、行数上限），
**一个动词都不改**（A 类永不由模型改写）；末尾那行（B 类）是**他自己留的话**，
没有就是没有 —— **绝不替他补一句**。

### 🔴 窗口规则：「她上次开口之后 → 这次开口之前」（不加水位线）

```
before = 当前这条用户消息的 id（由 session_of 反查出来，就是它）
after  = 该会话里**上一条她自己说的话**的 id（没有就 0）
⇒ 取 after < id < before 的活动，最多 ACTIVITY_MAX_ITEMS(3) 段
```

**为什么不用"最近 N 条"或"最近 12 小时"**：

- 那会**每次都把同一件事再喂一遍** → 记忆变复读；
- 这个窗口**自带清账**（她下次再开口，右端前移，上一批自动落出窗口）；
- 而且它**不需要任何新状态**（没有水位线、没有游标、没有新表/新列）——
  纯 SQL 一次查完，给定库内容永远同一结果 → **可被断言钉住**。

⚠️ 它有个诚实的边界：**只有在"她上次说话之后他做过事"时才带回去**。
   窗口里没有活动 → **什么都不插**（`nothing_new`），而不是"塞点别的凑数"。

### 与「一个 Kael」红线的关系

自主活动**不进聊天窗口的对话流**（那是 §5.5 的状态机管的），
但**卡片进时间线**：`msgInActiveSession()` 里对 `kind==="activity"` 直接放行 ——
它是**时间线级**事件（他做事时你开着哪个会话都可能），不属于任何一个 `api_session`。
不这么做的话，默认会话下这张卡片会**整个消失**，正好把"我做了事你看得见"变成反的。

卡片**不响铃、不在通话里朗读**（同 `act`：环境信息，不是他跟你说话）。

### 验收：`tools/activity_check.py`（第 14 套，73 项）

A 纯逻辑 24 · B 窗口 9 · C 注入 13 · D 红线/接线/前端契约 27。
**不起端口、不连外网**（本层根本不需要上游）—— 用伪造 payload 就能全验完，
**故意不依赖 KaelLife**。

⚠️ 两个自己踩的坑（都写进验收里了）：
1. **造数据的顺序**：活动行必须夹在"两条她说的话"**之间**才落进窗口
   （真实时序也是这样：他白天做事，你晚上开口）。第一版把活动插在最后 → C 组全红。
2. **会话行得先有**：⑧ 的摘要住在 `sessions` 表里，而 `sessions` 是靠
   `sync_from_messages` 投影出来的 —— 没投影就没有"会话"，`set_summary` 会 `not_found`。

### 🆕 同一批落地：**中转站三个槽**（E2，2026-09-21）

她 09-21 的原话是「**聊天和他自主醒来崩了的时候都有站子切换**、且**不是需要到后端换环境变量**
那种麻烦切换就行」。房子侧这一半当场落：

| | 内容 |
|---|---|
| **改了什么** | `providers.py` 的 `PROVIDER_ORDER` 末尾加 `relay2` / `relay3`；`_BASE_DEFS` 加两个**与 `relay` 完全同构**的定义（env 名字只差一个数字：`PROVIDER_RELAY2_KEY/_BASE/_MODELS`） |
| **为什么** | 主站和备用站**都是中转站** → 原来只有一个 `relay` 槽 → **两个配不进来** → 站子一挂就只能去 Zeabur 改 env + Redeploy（她最烦的那种） |
| **怎么用** | 两个站的 BASE/KEY/MODELS 各填一次（**永久**）；切换 = 网页设置页 → 模型卡片 → 供应商，点一下即可。`settings.provider_id` **每次请求现读** → **不用 redeploy** |
| **不填会怎样** | 该槽 `available=false` → 前端下拉里**灰掉并写明缺哪个 env**。这是**有意的好状态**，不是错误；对没配的人**零影响** |
| 🔴 **多槽 ≠ 高可用** | 网关**不做自动 failover**。站挂了就是**人去点一下**（见 `部署与中转站-站子切换.md` 里"明确不做"那节）。理由是自动切站 = 悄悄换上游 = 悄悄换模型快照 |
| 🔴 **只认 Claude** | 三个槽的模型清单**只填 Claude 那一支**。加备站**只解决"站挂了"，绝不引入"换了个模型的他"** |
| **验收** | `providers_check.py` **77a–77e**：三槽同构（env 名 / format / 内置模型清单为空）+ **槽里出现非 Claude 模型即红** |
| ⚠️ **还差一半** | **他自主醒来那侧（KaelLife）**仍读 env（`LLM_BASE_URL/KEY/MODEL`）→ 换站要 Redeploy。**待做**，方案见 `部署与中转站-站子切换.md` |

## 第十三道缝：usage 记账（P2，2026-09-22）—— **账单一直躺在响应里，我们只是没接**

起因是这句话（Lily 2026-09-22）：

> 「usage 记账到底是什么？」

答案有点难为情：**上游每次回答都附一份账单**（用了多少 token、其中多少命中了缓存），
网关早就把它从流里捡出来了 —— 然后**只塞进 SSE 帧就没了**。
所以问题从来不是"拿不到数据"，而是**接住它的那个动作没人做**。

这一缝就是把那个动作补上：四张表之外的**第 5 张表** `usage_log`（schema **v4 → v5**）。

### 🔴🔴 第一个发现：**流式请求必须"开口要"，上游才给账单**

这是整件事里最容易被漏掉的一步，也是**只做记账会变成空壳**的原因。

OpenAI 兼容协议里，**流式响应的 `usage` 默认不发** ——
要在请求体里带 `stream_options: {"include_usage": true}`，上游才会在最后一帧补上。

    POST {base}/chat/completions
    { "stream": true, "stream_options": {"include_usage": true}, ... }
                                     └─────────── 不带它，账单永远收不到 ──┘

⇒ 所以这次一并改了 `providers.adapt()` 的**出站**请求。这属于"改上游请求"，
和 `_effort_payload` 是同一类动作，所以照那条的老规矩来：

- **默认开**（DeepSeek / SiliconFlow / one-api 系中转站都认）
- **`LLM_STREAM_USAGE=0` 一键退回**（出站 body 逐字节回到改动前）——
  极少数古董站对不认识的字段直接 400，出事时**一个 env 就能退**，不用回滚版本
- **只加在流式上**：非流式本来就带回 usage，加这个字段反而是给某些站找麻烦
- **Anthropic 格式不加**：它原生就带账单，而且不认这个字段

### 🔴 第二个发现：Anthropic 把账单**拆成两帧**发

| 事件 | 带的账单 |
|---|---|
| `message_start` | `input_tokens`（**大头**）+ 缓存读 / 写 |
| `message_delta` | `output_tokens`（小头） |

原来的写法是 `usage = u`（**覆盖**）→ 只会剩下最后到的那个 →
**输入那半永久消失**，而且从外面看不出来（usage 不为空、只是少一半）。
改成 `usage.update(u)`（**合并**）。OpenAI 形状只发一个完整帧 → update 与覆盖等价，
主渠道零影响（验收 F2/F3 守这件事）。

### 🔴 四条纪律（每条都对应一种"账本说谎"的坏法）

**① 捡不到也必须留痕 —— `ok=0` 那一行**

如果"上游没给账单"就干脆不写行，那么 `coverage` 永远是 100%，而实际上一半调用没记账。
**账本里全是干净数据 = 账本在说谎。**
⇒ 没账单也写一行：`ok=0` + `note` 写明原因。`summary().gaps` 就是缺口的原因分布。

四种"没有账单"的情形，**各自留痕、互不混淆**：

| note | 什么时候 |
|---|---|
| `no_usage_in_payload` | 上游正常结束但没给 usage（连"没有理由"这件事也要写上理由） |
| `stopped` | 被停止 / 下游掐断 —— **上游很可能已经烧了 token** |
| `upstream_error` | 上游中途炸了 —— 同样可能有消耗（可能已经生成一半） |
| `client_disconnect` | 手机锁屏 / 关页面 / 刷新（`_gen()` 的 finally 兜底，`billed` 标志防重记） |

**② `None` ≠ `0`**

- `0` = 上游**明说**"就是 0"（很硬的证据：缓存真的没命中）
- `NULL` = 上游**没说**（什么也证明不了）

这两件事一旦混起来，命中率就再也算不准了 —— 而"看不出缓存有没有工作"
正是这次要解决的问题本身。⇒ 归一只挑认识的键，**捡不到就是 `None`**。

**③ 命中率的分母不能猜**

两家的口径**根本不同**：

| 形状 | `cache_read` 在哪 | 分母 |
|---|---|---|
| OpenAI / DeepSeek / Gemini | 是 `prompt_tokens` 的**子集**（已含在内） | `prompt_tokens` |
| Anthropic | 与 `input_tokens` **并列**（不含在内） | `input + cache_read + cache_write` |

拿同一个分母套两家 → 一家偏高一家偏低。用 Anthropic 那 900 的命中除以它自己的 100 输入，
命中率会算出 **1.31 —— 超过 1**，一眼就知道错（验收 C3 就是拿这个当区分力）。
⇒ 落库时记 `cache_in_prompt`（**口径开关**），命中率**按口径分行求和**。

而且**只累计"上游真报了 `cache_read`"的行**：把没数据的那批算进分母 = 拿缺口压低成绩，
也是伪造。一行都没有 / 全没报 ⇒ `cache_hit_rate = null`（"还不知道"），
**不是 `0`**（"完全没命中"）—— 这两个读起来天差地别。

**④ 这一版不算钱（有意不做）**

只交 token 与命中率 —— 它们**不会过期**。
"花了多少钱"要靠一张单价表，而**单价会漂**（站子改价 / 换模型 / 缓存价与普通价不同档）。
把会过期的东西写进代码 = 埋一颗将来必定骗人的真相。
⇒ 响应里 `cost: null` + `cost_note` 说明这件事，**不猜单价**。

### 🔴 两条边界

- **没有 HTTP 写入口**：账本唯一的写入口是**网关内联**（`llm_routes` 调 `record()`）。
  开一个 `POST /usage` = 谁能发请求谁就能伪造账单 ⇒ **结构性地不允许**（验收 D6/D7 专盯 405）。
  "写"这件事在 HTTP 面上**不存在** —— 跟 `memories` 的"没有 delete"是同一个手法。
- **不挂 MCP 门**：跟 `memories` 同一条边界 —— **这是我的账本，不是给他的记忆**。
  房间是给 Kael 走 MCP 的；账本给 Lily 走 HTTP + 密钥。

### ⚠️ 踩到的坑：**给它加外键 = 装一个会静默吞账的开关**

第一版给 `usage_log.user_id` 写了 `REFERENCES users(id)`（跟别的表一致，看着很自然）。
冒烟**当场**就炸了：只要记账时 `users` 表里还没有那一行，
`record()` 全部 `IntegrityError: FOREIGN KEY constraint failed` ——
而 `record()` 是 **fail-open** 的 → 错误被吞掉 → **账静默丢失**，表面上一切正常。

> 别的表要外键是对的（它们是**业务状态**：会话属于谁、记忆属于谁）。
> 但 `usage_log` 是**只追加的事件日志** —— 它的第一要求是"**绝不能丢**"。
> 给它加外键 = 让"记一笔账"依赖于另一张表的状态。

⇒ 这是**唯一**一张 `user_id` 不带外键的表，理由写在 `schema.py` 里（验收 B10 守它）。

### 端点 / 开关 / 下一个数

    GET /app/ext/usage/summary   ?days=7       调用次数 / 覆盖率 / token / **缓存命中率**
    GET /app/ext/usage/recent    ?limit=&raw=1 最近 N 笔明细（raw 默认不给）
    GET /app/ext/usage/status                  账本自己在不在工作（行数 / 口径分布）

逃生开关 `APP_EXT_USAGE_DISABLED=1` → 端点 404，但**网关照常记账、表照常建**
（「能力没了 ≠ 数据没了」，跟 ⑩-a 同一条）。
⚠️ 前端**还没接**（跟 ⑧ 的真端点一样的情况）—— 现在要看数就 `curl` 那三个端点。

### 验收：`tools/usage_check.py`（第 15 套，78 项，不起端口、不连外网）

A 归一化 17 · B 真升级 v4 老库 + 记账 15 · C 聚合与命中率（**手算对账**）13 ·
D 端点 / 红线 / 接线 / 开关 22 · E 出站索要账单（真跑 `adapt`）5 · F 接线守门 6。

⚠️ 三个自己踩的坑：
1. **断言"新建表"之前别先把表建了**：第一版 `fresh_db()` 内部已经 `ensure_schema`，
   再断言 `created == ["usage_log"]` → 永远是空 → 假红。改成**真造一个 v4 老库**来升，
   顺带把"升级一行老数据都没动"也验了（B0/B0b/B0c）。
2. **`ok=1` 的行本来就没有"缺口理由"**（它是 `None`，正确）。
   只该看 `ok=0` 那些行的 note —— 不过滤就把"正常有账单"当成"缺口没写理由"（B7 假红）。
3. **验收脚本自己的手算值也会错**（C7 第一版把 by_model 的期望写反了）。

📌 顺带：这次加第 5 张表，**三处老断言跟着有意升级**（`generate_check.D9`、
`memory_check.D11` 的写死版本号，以及 `app_ext_check` 的表清单）——
这就是那两条"有意升级就来改这里"的注释在起作用。

### 下一个数：缓存**命中率**要先看得见，缓存**启用**才有意义

这一缝解决的是"**看得见**"。要不要"**让它开始命中**"是下一步：
现在出站请求**带不带缓存指令**（Anthropic 的 `cache_control` 断点、
某些站的自动前缀缓存）决定命中率是 0 还是别的数。
⇒ 顺序是：**先记账 → 看现状那三个数 → 再决定改不改请求**。

## `web/index.html`：13 处家装 + 1 处 bug 修复（**唯一被动过的原生文件**）


按 Lily 的反馈做的"家装"。
**全部是 UI、登录体验与提示文案，不含任何 KaelLife 逻辑**，
也不参与 relay 与 AI 侧的任何路径 —— 第二阶段换身体时不受影响。

> 最后一条（**补丁**）不一样：它确实动了**发送路径**（修一个原版就有的 bug），所以不占"家装"编号。

| # | 改了什么 | 位置 | 为什么 |
|---|---|---|---|
| 1 | 隐藏顶栏「终端视图」与「语音通话」两个按钮 | CSS 新增一条 `.header-actions .topbtn.terminal, .topbtn.call{display:none}` | 终端只是同一段对话的命令行皮肤（工具步骤聊天页已有 ✧ 折叠块）；语音是原版占位假按键。**DOM 与 JS 一行未删**，想恢复删掉那行选择器即可 |
| 2 | 纪念日 `SINCE`：`2026/01/01` → `2026/07/01` | `CONFIG.SINCE` | 原值是作者占位值；7/1 是 Lily & Kael 认识日 |
| 3 | 默认名 `AI_NAME`：`Claude` → `Kael`（并同步登录页/空状态/顶栏/个人信息面板的静态兜底文案） | `CONFIG.AI_NAME` + 静态 HTML | 原版"备注名"只改聊天页顶栏，登录页等仍写死 Claude |
| 4 | 登录密钥**自动清洗不可见字符**，且验证失败**不再清空输入框** | `sanitizeSecret()` / `showLogin()` / login submit | 从备忘录·微信复制密钥会夹带零宽空格/BOM/NBSP/软连字符等肉眼不可见字符 → 后端 401；原版只 `.trim()`（去不掉中间与零宽），失败后还清空输入框逼用户重打整串 |
| 5 | 会话列表**失败/降级时给出可读提示**（原版静默） | `#sessionNotice` + `setSessionNotice()` | 原版 `catch(_){ apiSessions=[] }` 静默清空，用户只看到"按钮空了"。现在会说明是"AI 身体不在，列表是整理出来的"还是"连不上"，并说清消息本身没受影响 |
| 6 | 会话条目「改名 / 归档 / 删除」+ 二次确认 + 归档区 | 配套后端 `deploy/sessions_manage.py`（新路径 `/app/sessions/manage/*`） | 原版只有"删掉整段对话"的入口、且没有归档概念。归档（纯可逆）与清空（标记式、物理行保留）**语义分开** |
| 7 | 修透明弹层：删除确认框与会话面板**整片透明** | `.confirm-card` / `.session-pop` | 🔴 `--panel-bg` / `--seg-line` **在这个文件里从未定义过** → CSS 属性被整条**静默丢弃**（不报错、控制台无提示）。修法：改成不依赖变量的实色 + 毛玻璃。commit `cad47ce` |
| 8 | 设置页「模型 / effort / 上下文阈值」三个**假按键接真** + 新增「验证模型名」 | `#modelProvSeg` / `#modelSeg` / `#modelHint` / `#modelProbeRow` + `refreshModelCard()` 系列 | 🆕 P1 收尾（2026-09-15）。原版这 4 个模型按钮是硬编码 `Opus 4.6/4.7/4.8/Fable 5`、点了只换底色，且什么都不存。现在只显示**服务端允许列表**里的东西、选中即落库（见本文 P1 收尾一节） |
| 9 | 菜单新增第 7 个入口 **Workshop**（点进去 `location.assign("workshop.html")`） | `.menu-list` 里一个新 `.menu-item[data-menu="workshop"]` + 菜单点击回调多一个分支 | 🆕 房间层（2026-09-18）。他的**工作间**要有她这边能推开的门；原版菜单 6 项（Room 是占位），新页面 `web/workshop.html` 是新文件、不改原版 |
| 10 | 菜单新增第 8 个入口 **Archive**（点进去 `location.assign("archive.html")`） | 同上，多一个 `.menu-item[data-menu="archive"]` + 一个分支 | 🆕 P2-0（2026-09-19）。**她的**导出/快照（不是他的房间）；页面 `web/archive.html` 是新文件、不改原版 |
| 11 | 生成中在发送键左边多一个**停止键**（方块），只在"正在生成"时出现 | `#stopBtn`（新增）+ `.floatbtn.stop` + `syncGenControls()` / `genActive()` / `doStop()` | 🆕 P2 ⑨（2026-09-19）。判据 = 身体推了 typing **或**屏幕上还有没落库的流式草稿。点下去**两步都做**：先本地立刻收草稿（手感是"立刻停"）→ 再 `POST /app/ext/generate/stop` 掐上游（**钱在这儿**；只做前者屏幕上还会继续冒字，只做后者手感会顿） |
| 12 | 长按**最后一条回复**，菜单里多出「重答」与「换一版 `i/n`」 | `#msg-menu-actions` 里两个新按钮 + `replyChains()` / `doRetry()` / `doCycleVariant()` / `hiddenVariantKeys()` | 🆕 P2 ⑨（2026-09-19）。版本链**严格以 `meta.superseded` 为判据**，不是"两条挨着的 ai/reply"（**他一次完全可以正常说两句**，那种必须都显示）。链与"在看第几版"全从 `sortedMessages()` 推 → 刷新/换设备都认得出来；翻版本只改"哪一条进渲染"（`buildVirtualRows` 的 hide 集合），**不动库、不改正文、不删任何一条**。⚠️ 设计稿原写"气泡上一个 `‹1/3›` 小切换器"，实际放进长按菜单：气泡是虚列表渲染的，往上加控件要动 `virtualRowSignature` / 节点缓存那套"丝滑命根"，风险大得多 |
| 13 | 聊天时间线里多一种**自主活动卡片**（`kind:"activity"`）：标题一行 + 动作一行一个 + 展开只出更多动作行 | `makeActivity()` / `activityData()` / `buildVirtualRows` 的 activity 分支 / `.row.activity` CSS / `msgInActiveSession` 放行 / `openActivityKeys` | 🆕 P2 ⑪（2026-09-21）。**只渲染，不推导**：库里没有的就不显示（A 类不加动词、B 类不代写）。展开看到的是**更多动作行**，不是独白 —— 原文在他的书房（§5.1） |
| 补丁 | 🔴 **修一个原版就有的会话归属 bug**：在「旧主线 / Desktop 记录」（虚拟会话 `__legacy__`）里发消息，回复会落到别的会话 | 新增 `ensureRealSession()`（`doSend` / `apiSend` 两个发送入口各拦一道）+ 页面级常驻提示条（`#pageNotice` / `LEGACY_NOTICE`，2026-09-14 晚从面板内挪到页面级） | 根因：**会话归属有两份**（前端视图 / 身体全局）→ 详见 `扩展边界.md` §2.5。**这一条是修 bug，不是家装**（唯一一处涉及行为的改动） |

其中 2、3 各是一处常量，改回原值即可；1 是一行 CSS；4、5、6 是新增纯前端函数；
7 是修一个原版就有的 CSS 变量 bug；8 是 P1 收尾（新增纯前端函数，且**首次把设置页接到真实后端**）；
9 是房间层（菜单多一项，指到一个新文件）；10 是 P2-0 导出（同上，也是菜单多一项）；
11、12 是 P2 ⑨（停止键 + 长按菜单里的重答/翻版本，都是纯前端交互，
**消息正文一个字不动**，翻版本只改"哪一条进渲染"）；
13 是 P2 ⑪（自主活动卡片，新增一类 `kind` 的渲染分支 + 会话过滤放行；
**不改任何已有 kind 的渲染**，也不动库）；
**补丁**那条是修一个原版就有的**会话归属 bug**（唯一涉及行为的改动）。

> ⚠️ **⑨ 那次提交（`75debdc`）漏了把 11、12 两行写进这张表**（只加了第十道缝）。
> 2026-09-20 补上。📌 **教训：加"家装"必须同时改三个地方** ——
> 表里加一行、下面这段编号说明、以及本节的"**N 处家装**"标题数字。
> 漏一处的表现是"文档说 10 处、代码有 12 处"，而**没人会去数**。
**补丁**那条是修一个原版就有的**会话归属 bug**（唯一涉及行为的改动）。

## 相关文件

- `Dockerfile`（仓库根目录）— 单服务镜像
- `deploy/entrypoint.sh` — 导出持久化路径、拉起两个进程、单进程重启
- `deploy/serve.py` — 静态托管 + 前缀剥离 + 挂载会话兜底与 P0 地基
- `deploy/sessions_fallback.py` — 会话列表不依赖 AI 身体的兜底（含安全红线说明）
- `deploy/sessions_manage.py` — 会话归档 / 删除 / 改名（数据库直读直写，不走身体）
- `deploy/app_ext/` — **P0 地基**：五张表（`users`/`settings`/`sessions`/`memories`/`usage_log`）+ 身份层 + `/app/ext/*`
- `deploy/app_ext/providers.py` · `llm_gateway.py` · `llm_routes.py` — **P1 模型网关**：供应商允许列表 + 三格式适配 + `/app/ext/providers`、`/app/ext/llm/*`（含 **OpenAI 路径别名** `/v1/chat/completions`，通车前置）
- `deploy/app_ext/activity.py` — 🆕 **P2 ⑪ 自主活动带回上下文**：窗口（她上次开口之后）+ 确定性拼接 + 只插不删的注入；`/app/ext/activity*`（**全只读**，写侧是原版 `/channel/out`）
- `部署与中转站-站子切换.md` — 🆕 **站子切换**（三个中转站槽 + 前端一键切 + 他自主醒来那侧还差什么）
- `deploy/app_ext/mcp.py` — 🆕 **房间层的门**：Streamable HTTP MCP（`/mcp` + 别名 `/app/ext/mcp`），工具注册表 + JSON-RPC 分发；**协议版本回显**、无状态、鉴权 fail-closed
- `deploy/app_ext/modules/` — 🆕 **房间**：`__init__.py` 写约定，`workshop.py` 是**工作间**（`make_thing`/`revise_thing`/`list_things`/`read_thing` + 展示端点 `/app/ext/workshop/*`）
- `deploy/app_ext/archive.py` — 🆕 **P2-0 导出 / 快照**：`/app/ext/archive/{info,db,jsonl,files}`，**只读、只有 GET**、`Connection.backup()` 一致快照、只认 Bearer（拒 `?token=`）；**不是房间**
- `deploy/app_ext/context.py` — 🆕 **P2 ⑧ 上下文管理**：在网关注入 `sessions.summary`（**只插不删**、fail-open）+ `/app/ext/context/{summarize,status}`（压缩**手动**触发，房子不自己花钱）；**不是房间**
- `deploy/app_ext/generate.py` — 🆕 **P2 ⑨ 停止 / 重答 / 多版本**：`/app/ext/generate/{stop,retry,reroll,status}`；真"停住"的收尾在 `llm_routes` 的停止检查点；写 `messages` **只写 `meta`**；**不是房间**
- `deploy/app_ext/memory.py` · `memories_store.py` — 🆕 **P2 ⑩-a 记忆层**：`memories.source` 缝（schema **v4**）+ `GET/POST /app/ext/memories` + `/stats`；🔴 **无 delete、`salience` 永不外泄、不挂 MCP 门**（它是"房子的技术缓存"，不是"他的记忆"）；**不是房间**
- `deploy/app_ext/usage.py` · `usage_store.py` — 🆕 **P2 usage 记账**（schema **v5**）：上游每次真实调用的 token 账单 → `usage_log` 表；四家形状归一（OpenAI / Anthropic / DeepSeek / Gemini）+ 命中率（分母按 `cache_in_prompt` 分口径）+ **捡不到也留痕**（`ok=0`）；`GET /app/ext/usage/{summary,recent,status}`（**全只读**）。🔴 **没有 POST 写路由**（能发请求就能伪造账单）· **不挂 MCP 门**（那是我的账，不是给他的记忆）· **不算钱**（单价会漂）。⚠️ 出站流式现在会带 `stream_options.include_usage`（不开口要就收不到账单），`LLM_STREAM_USAGE=0` 一键退回
- `web/workshop.html` — 🆕 工作间的展示页（预览走 `srcdoc`，**密钥不进 URL**）
- `web/archive.html` — 🆕 导出 / 快照的页面（下载走 fetch + Blob，**密钥不进 URL**；明说「不做导入」）
- `deploy/zeabur-env.example` — 环境变量清单（哪些必填、哪些别填；**P1 段在最后**）
- `tools/verify_all.py` — **一次跑完全部验收**（**十五套 + 红线**，exit 0 = 全绿；
  跑前先确认 8080 空；**全量约 1m40s，别用短的超时掐它**）
- `tools/secaudit.py` — 访问控制体检（21 项，不连公网）
- `tools/sessioncheck.py` — 会话数据层 + 兜底断言（19 项）
- `tools/sessionfallback_check.py` — 兜底四场景 + 鉴权红线（34 项）
- `tools/sessions_manage_check.py` — 会话归档/删除/改名 后端（40 项）
- `tools/session_ui_check.mjs` — 会话归档/删除/改名 前端（40 项，jsdom 真跑 `index.html`）
- `tools/app_ext_check.py` — **P0 地基验收（55 项：库层 + HTTP 层 + 红线）**
- `tools/providers_check.py` — **P1 模型网关验收（166 项：纯逻辑 + 真 HTTP + 端点 + OpenAI 路径别名 + 通车仿真 + 参数下发/effort + CoT 透传 + 红线；自带本地假上游，不需要真 key）**
- `tools/workshop_check.py` — 🆕 **房间层验收（121 项：工作间存储 / 工具注册表 / 真 MCP 客户端 / REST 与 raw 安全 / 展示页 / 空库回归）**；
  C 组**用官方 mcp SDK 真连**（照 `scheduler.py` 那三行），不是自己造个客户端骗自己
- `tools/archive_check.py` — 🆕 **P2-0 导出验收（55 项）**：A 组**故意用 WAL + 未 checkpoint 的写入**
  证明快照不裸拷能自洽（副本独立打开 / 表名行数一致 / 源库一字未动）；B 组走真 HTTP
  （`?token=` 必须 400、POST 必须 405、导出期间写不受影响、临时快照不残留）；
  C 组守「只注册 GET / 不 import mcp」；D 组守页面「密钥不进 URL」
  🔴 **A9b / B6b 守「jsonl 一行一条」**（换行数 == 记录数、逐行可 parse、末行有换行）——
  这两条是 09-19 补的：原先只做「能 grep 到 / 能 parse」，记录粘成一整行照样全绿；
  真导一份下来才发现 18 条挤成 1 行 17KB，`grep`/`wc -l` 全废。
  **"记录本身合法" ≠ "文件是 JSONL"** —— 验收要**数换行**。
- `tools/context_check.py` — **P2 ⑧ 上下文管理验收（50 项）**：A 组纯逻辑（估算 token /
  分界线 / **材料人称与压缩提示的人称规约**）；**B 组起假上游，拿"它真正收到的 body"做断言**
  （摘要进 system 了吗、人格仍在前面吗、`messages` 是否逐字节不变、有没有串会话、
  有没有多收字段、流式端点同样注入）；C 组 `summarize` 端点（dry 不写不调、没到阈值一次都不调、
  `force` 真写、**原文一条不删**、第二次是增量、**`rebuild` 从头重压**、端到端）；
  D 组接线 / **v2 老库自动补列** / 开关。
  ⚠️ 它自带 8800/8801/8802 三个端口，且**先停第一间房子再起第二间**（两个进程抢 SQLite 写锁
  会产出假红）。
  ⚠️ 写断言时**别用 `d.get("k") or -1`** —— `foldable_rows=0` / `prev_summary_chars=0`
  是合法值但 falsy，会被当成"字段缺失"→ 假红（09-19 真踩两条）。本套有 `num()` 帮手。
- `tools/generate_check.py` — **P2 ⑨ 停止/重答/多版本验收（82 项）**：A 纯逻辑 19 ·
  B 网关照停（**假身体 + 慢上游，从出口倒着验**）19 · C retry/reroll 17 · D 红线/接线/开关 16。
  B 组必须**自己写一个假身体**（照抄 `api_loop.stream_chat` / `run_model` 的形状，
  **含 fallback 那段**），因为"停止变成偷偷换模型重跑"这个病**只会在身体那一侧显形**；
  断言打在**上游被调用的次数**上。
  ⚠️ 它的 `D9` 把 `SCHEMA_VERSION` **写死** —— 那是**有意**的，守"没人偷偷动表结构"。
  有意升级 schema 时就来回改这一行（09-20 ⑩-a 把它从 3 改成 4；
  09-22 usage 记账加第 5 张表又从 4 改成 5）。`memory_check.D11` 是同一手法。
- `tools/memory_check.py` — **P2 ⑩-a 记忆层验收（87 项）**：A store 层 30 ·
  B 迁移 v3→v4（**真造一个 v3 老库来升**）19 · C 端点 23 · D 红线/接线/开关 15。
  🔴 B 组是重点：手工造 v3 形状的库（把 DDL 里 `source` 那行摘掉）+ 一条老记忆
  + 两条 `messages`，跑 `ensure_schema()` 升上来，再验老行**还在**、正文**一个字没改**、
  新列是 **NULL**（**不是 `'chat'`** —— 带 `DEFAULT` 会伪造来源）、`messages` 的 DDL
  **逐字未变**、幂等。
  ⚠️ 它**不起端口**（进程内 ASGI TestClient）→ 不跟别的套抢端口。
  ⚠️ 断言的比对面要跟它想守的事对齐：守"投影里没这个**字段**"就该比**键名集合**，
  别比整段 JSON —— 测试数据自己的正文里可能就有那个词（A30 真踩过）。
- `tools/usage_check.py` — 🆕 **P2 usage 记账验收（78 项）**：A 四家形状归一 17 ·
  B **真造 v4 老库升 v5** + 记账 15 · C 聚合与命中率（**手算对账**）13 ·
  D 端点 / 红线 / 接线 / 开关 22 · E 出站索要账单（真跑 `adapt`）5 · F 接线守门 6。
  🔴 重点守的是**数据的意义**，不是"有没有数据"：捡不到必须是 `None` 不是 `0`、
  `ok=0` 必有理由、命中率按 `cache_in_prompt` 分口径、空数据 = `null` 而不是 `0`、
  真 0 命中 = `0.0`（与 `null` 严格区分）、**没有 POST 写路由**（405）、
  出站流式必须带 `stream_options`、`LLM_STREAM_USAGE=0` 能逐字节退回。
  ⚠️ 它**不起端口、不连外网**。
- `tools/model_ui_check.mjs` — 🆕 **设置页模型/参数前端（35 项，jsdom 真跑 `index.html`）**：
  专治"后端接口对、前端逻辑错"这类只有真跑页面才看得见的问题 ——
  假状态、PUT 失败不回滚、以及"拉不到就硬编一个"这三件事各有用例守着
- `tools/jscheck.py` — 抽出 `web/*.html` 内联 JS 做语法检查（index / album / workshop / archive 全覆盖）
