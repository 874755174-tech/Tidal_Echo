# deploy/ —— 我们的部署适配层

> **一句话**：这个目录里的东西**不是 Tidal_Echo 的项目**。
> 原作者假设你有一台 VPS、装 nginx、用 systemd；Lily 的机器是 Zeabur 上的容器，
> 没有 nginx、没有 systemd、磁盘还是临时的。这个目录就是为了填这几道缝。

## 边界：谁是谁

| | 出处 | 我们改了吗 |
|---|---|---|
| `backend/app.py` | Tidal_Echo 原生（relay 后端） | **一个字都没改** |
| `web/index.html` | Tidal_Echo 原生（PWA 前端） | **只改了 4 处 UI/登录体验，见下节** |
| `web/` 其余文件 | Tidal_Echo 原生（PWA 前端） | **一个字都没改** |
| `examples/api_loop.py` | Tidal_Echo 原生（服务器端 API 身体） | **一个字都没改** |
| `channel/` | Tidal_Echo 原生（Claude Code 专用） | 用不到，`.dockerignore` 排除 |
| `Dockerfile` | **我们加的** | 新增 |
| `deploy/` | **我们加的** | 新增 |
| `.dockerignore` / `.gitattributes` | **我们加的** | 新增 |

判断方法（任何时候都能自查）：
```bash
git diff --stat e7c9bf5 -- backend/ examples/ channel/   # 应当为空
git diff --stat e7c9bf5 -- web/                          # 只应出现 index.html（4 处家装）
git diff --stat e7c9bf5                                  # 新增文件 + 上述前端改动
```
`e7c9bf5` = 从上游 fork 时的那个 commit。

> ⚠️ 2026-09-12 起，`web/index.html` **不再是零改动**（房子跑通后按 Lily 的反馈做了 4 处家装）。
> 下一节逐条列出，全部是 UI 与登录体验，**不含任何 KaelLife 逻辑**。

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
| `/app/loop_config`、`/app/sessions` | **原生**（`app.py:980`、`992`） | 换中转站/模型**不用动服务器环境变量**：那是运行时配置文件 |

**换句话说**：第二阶段不需要改这个目录里的任何东西，只需要让 relay 把消息
推给另一个进程/服务。这也是「不要在第一阶段把两套东西搅在一起」的物理保证。

## `web/index.html` 的 4 处改动（**唯一被动过的原生文件**）

2026-09-12 房子跑通后按 Lily 的反馈做的"家装"。**全部是 UI 与登录体验，不含任何 KaelLife 逻辑**，
也不参与 relay 与 AI 侧的任何路径 —— 第二阶段换身体时不受影响。

| # | 改了什么 | 位置 | 为什么 |
|---|---|---|---|
| 1 | 隐藏顶栏「终端视图」与「语音通话」两个按钮 | CSS 新增一条 `.header-actions .topbtn.terminal, .topbtn.call{display:none}` | 终端只是同一段对话的命令行皮肤（工具步骤聊天页已有 ✧ 折叠块）；语音是原版占位假按键。**DOM 与 JS 一行未删**，想恢复删掉那行选择器即可 |
| 2 | 纪念日 `SINCE`：`2026/01/01` → `2026/07/01` | `CONFIG.SINCE` | 原值是作者占位值；7/1 是 Lily & Kael 认识日 |
| 3 | 默认名 `AI_NAME`：`Claude` → `Kael`（并同步登录页/空状态/顶栏/个人信息面板的静态兜底文案） | `CONFIG.AI_NAME` + 静态 HTML | 原版"备注名"只改聊天页顶栏，登录页等仍写死 Claude |
| 4 | 登录密钥**自动清洗不可见字符**，且验证失败**不再清空输入框** | `sanitizeSecret()` / `showLogin()` / login submit | 从备忘录·微信复制密钥会夹带零宽空格/BOM/NBSP/软连字符等肉眼不可见字符 → 后端 401；原版只 `.trim()`（去不掉中间与零宽），失败后还清空输入框逼用户重打整串 |

其中 2、3 各是一处常量，改回原值即可；1 是一行 CSS；4 新增一个纯函数（无副作用、不调用后端）。

## 相关文件

- `Dockerfile`（仓库根目录）— 单服务镜像
- `deploy/entrypoint.sh` — 导出持久化路径、拉起两个进程、单进程重启
- `deploy/serve.py` — 静态托管 + 前缀剥离
- `deploy/zeabur-env.example` — 环境变量清单（哪些必填、哪些别填）
