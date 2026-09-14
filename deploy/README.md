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
| `app_ext/llm_routes.py` | `/app/ext/providers` 与 `/app/ext/llm/*` |
| `tools/providers_check.py` | 验收 106 项（纯逻辑 + 真 HTTP 往返 + 端点 + 红线） |

**三条铁律**（对应 `架构与产品路线规划.md` §4.1）：

1. **前端只能选允许列表里的** —— `/app/ext/providers` 只回 `id / label / models / available / key_masked`，
   **没有 endpoint、没有 key、连环境变量名都没有**（验收里有专门三项断言这件事）。
2. **key 不出服务端** —— key 只在 `providers._view()` 里从环境变量读出来，只进 headers。
3. **不接受任意 URL** —— 调用方传的是 `provider_id`（如 `"relay"`），
   真实地址由**服务端 env**决定。（中转站地址可变**不违反**这条：地址是你填的，不是前端提交的。）

### 🔴 对下游伪装成「OpenAI 兼容端点」

    POST /app/ext/llm/chat      请求体 = OpenAI 风格，响应 = OpenAI 风格 SSE
    POST /app/ext/llm/complete  请求体 = OpenAI 风格，响应 = OpenAI 风格 JSON

这是本阶段最省事的一个决定：`examples/api_loop.py` **已经**会解析 OpenAI SSE，
将来接身体时它只要把 base 换成房子、key 换成 `RELAY_SECRET`，**解析代码一行都不用改**，
而供应商密钥从此不出房子。换供应商对下游完全透明。

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

**新增环境变量见 `deploy/zeabur-env.example` 的 P1 段。** 逃生开关：
`APP_EXT_LLM_DISABLED=1`（只关网关，四张表照常）。

> ⚠️ **这组 `PROVIDER_*` 暂时不替代 `LLM_API_*`**：临时人偶现在还是自己调模型。
> 网关是"车道修好了但还没通车" —— 等 P3 接 KaelLife 时才会把身体指过来。
> 在那之前它已经有用：**能真实验证模型名通不通**（那是以前完全没法做的事）。

## `web/index.html`：7 处家装 + 1 处 bug 修复（**唯一被动过的原生文件**）

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
| 补丁 | 🔴 **修一个原版就有的会话归属 bug**：在「旧主线 / Desktop 记录」（虚拟会话 `__legacy__`）里发消息，回复会落到别的会话 | 新增 `ensureRealSession()`（`doSend` / `apiSend` 两个发送入口各拦一道）+ 页面级常驻提示条（`#pageNotice` / `LEGACY_NOTICE`，2026-09-14 晚从面板内挪到页面级） | 根因：**会话归属有两份**（前端视图 / 身体全局）→ 详见 `扩展边界.md` §2.5。**这一条是修 bug，不是家装**（唯一一处涉及行为的改动） |

其中 2、3 各是一处常量，改回原值即可；1 是一行 CSS；4、5、6 是新增纯前端函数；
7 是修一个原版就有的 CSS 变量 bug；**补丁**那条是修一个原版就有的**会话归属 bug**（唯一涉及行为的改动）。

## 相关文件

- `Dockerfile`（仓库根目录）— 单服务镜像
- `deploy/entrypoint.sh` — 导出持久化路径、拉起两个进程、单进程重启
- `deploy/serve.py` — 静态托管 + 前缀剥离 + 挂载会话兜底与 P0 地基
- `deploy/sessions_fallback.py` — 会话列表不依赖 AI 身体的兜底（含安全红线说明）
- `deploy/sessions_manage.py` — 会话归档 / 删除 / 改名（数据库直读直写，不走身体）
- `deploy/app_ext/` — **P0 地基**：四张表（`users`/`settings`/`sessions`/`memories`）+ 身份层 + `/app/ext/*`
- `deploy/app_ext/providers.py` · `llm_gateway.py` · `llm_routes.py` — **P1 模型网关**：供应商允许列表 + 三格式适配 + `/app/ext/providers`、`/app/ext/llm/*`
- `deploy/zeabur-env.example` — 环境变量清单（哪些必填、哪些别填；**P1 段在最后**）
- `tools/verify_all.py` — **一次跑完全部验收**（六套 + 红线，exit 0 = 全绿）
- `tools/secaudit.py` — 访问控制体检（21 项，不连公网）
- `tools/sessioncheck.py` — 会话数据层 + 兜底断言（19 项）
- `tools/sessionfallback_check.py` — 兜底四场景 + 鉴权红线（34 项）
- `tools/sessions_manage_check.py` — 会话归档/删除/改名 后端（40 项）
- `tools/session_ui_check.mjs` — 会话归档/删除/改名 前端（40 项，jsdom 真跑 `index.html`）
- `tools/app_ext_check.py` — **P0 地基验收（55 项：库层 + HTTP 层 + 红线）**
- `tools/providers_check.py` — **P1 模型网关验收（106 项：纯逻辑 + 真 HTTP + 端点 + 红线；自带本地假上游，不需要真 key）**
- `tools/jscheck.py` — 抽出 `index.html` 内联 JS 做语法检查
