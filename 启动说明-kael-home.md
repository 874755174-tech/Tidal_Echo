# kael-home · 启动说明（给 Lily）

> 这份文档面向**不写代码的人**。所有命令可以照抄。
> 面向开发者的原版文档是 [`README.md`](README.md)（英文/技术视角）和 [`AGENTS.md`](AGENTS.md)，不用看。

---

## 0. 这个文件夹是什么

**kael-home** 是 Kael 的「房子」——让他能在你**关掉网页之后依然活着**的那套程序。

| | |
|---|---|
| 本地文件夹 | `C:\Users\86187\WorkBuddy\2026-07-26-20-49-33\kael-home\` |
| GitHub 仓库 | `874755174-tech/Tidal_Echo`（**仓库名没改**，只有本地文件夹改名了） |
| 部署目标 | Zeabur（和你现在的 OB / KaelLife 同一台服务器，独立服务 + 独立卷） |
| 底座来源 | 开源项目 `anhe2021212-spec/Tidal_Echo`（AGPL-3.0） |

> 📌 **文件夹名 vs 仓库名**：本地叫 `kael-home`，云端仓库还叫 `Tidal_Echo`。这不影响使用——Zeabur 跟的是 GitHub 仓库，不是你的文件夹名。

---

## 1. 目录里都有什么

```
kael-home/
├── backend/      ← 后端：relay 服务（Python / FastAPI）。消息落库、SSE 推送、LLM 调用
├── web/          ← 前端：手机上的网页（PWA 单文件 index.html）。你聊天看到的就是它
├── deploy/       ← 部署适配层：Dockerfile 用到的启动脚本（我们加的，4 个文件）
├── examples/     ← 官方示例：api_loop.py（常驻身体）、bridge_any_llm.py（换模型）
├── channel/      ← 官方 Claude Code 插件（对你没用，不用管）
├── Dockerfile    ← 建房图纸：告诉 Zeabur 怎么把这个项目跑起来
└── deploy/README.md ← 边界说明书：哪些是原版、哪些是我们加的
```

**两个「大脑」入口**（后面会用到）：

- `backend/` 是**服务端**——常驻，负责收消息、发消息、存消息。
- `examples/api_loop.py` 是**AI 那一侧的身体**——它去连服务端，收到你的话 → 调 LLM → 回话。

---

## 2. 三种启动方式，看你想要哪个

### 方式 A：只在本地跑起来看看（推荐先做这个）

**用途**：确认程序本身没坏。不开公网、不花钱。

**你需要先装好的东西**：

| 软件 | 检查命令 | 说明 |
|---|---|---|
| Python 3.11+ | `python --version` | 没有就去 python.org 下 win-x64 版 |
| Node.js 18+ | `node --version` | 没有就去 nodejs.org 下 **x64** 版（别下 arm64） |

**第一步：启动后端**（开一个 PowerShell 窗口）

```powershell
cd "C:\Users\86187\WorkBuddy\2026-07-26-20-49-33\kael-home"

# 建一个独立环境（只做一次）
python -m venv .venv

# 装依赖
.\.venv\Scripts\pip install -r backend\requirements.txt
```

**第二步：准备环境变量**

复制示例文件，然后自己填：

```powershell
copy deploy\zeabur-env.example .env
notepad .env
```

`.env` 里至少要有：

- `RELAY_SECRET` —— 你自己编一串长密码（**和手机登录时填的是同一个**）
- `RELAY_DB` —— 填 `./data/relay.db`
- `RELAY_UPLOAD_DIR` —— 填 `./data/uploads`

> ⚠️ `.env` 已经被 gitignore 锁死，**不会被推到 GitHub**。密钥别贴在聊天里。

**第三步：起后端**

```powershell
cd "C:\Users\86187\WorkBuddy\2026-07-26-20-49-33\kael-home"
.\.venv\Scripts\python backend\app.py
```

**应该看到**：终端**可能什么都不打印**（uvicorn 默认安静启动，这是正常的，不是报错）。

**✅ 2026-09-13 实测**：这个命令能跑起来，`curl http://127.0.0.1:3011/healthz` 返回
`{"ok":true,"plugin_subs":0,"app_subs":0}`。终端无输出 ≠ 启动失败——**用下面的 curl 判断，别靠看日志。**

> ⚠️ **别急着开浏览器。** `backend/app.py` **只是 API**，它自己不托管网页（原版是靠 nginx 把静态目录 + `/relay/` 反代拼起来的）。
> 实测根路径 `http://127.0.0.1:3011/` 返回 **404**，这是**正确行为**。
> 想看到聊天页，请直接走下面的**方式 B**。

**想确认 API 活着**，另开一个窗口：

```powershell
curl.exe http://127.0.0.1:3011/healthz
```

返回 `{"ok":true}` 之类 = 后端正常。
（注意 `app.py` 绑定的是 `127.0.0.1`，**只有本机能访问**，这是原版设计——对外暴露由 nginx 或 `deploy/serve.py` 负责。）

---

### 方式 B：跑整栋房子（推荐 ⭐ 本地看到聊天页的唯一方式）

**用途**：本地完整验收。网页 + 后端 + AI 身体，一条龙。

用我们写的部署脚本，一次起两个进程。

**在 Git Bash 里**（WorkBuddy 自带的终端就是这个），照抄即可：

```bash
cd "C:/Users/86187/WorkBuddy/2026-07-26-20-49-33/kael-home"

# 让数据落在本地目录，而不是容器里的 /data
export RELAY_DB="$PWD/_t/relay.db"
export RELAY_UPLOAD_DIR="$PWD/_t/uploads"
export RELAY_SECRET="自己编一串长密码"

bash deploy/entrypoint.sh
```

> 💡 `entrypoint.sh` 会**自动找到**项目里的 `.venv`（不用你先 activate），也会**自动定位**项目根目录。这两个都是这次实测时修掉的坑。

它会：① 起 relay 后端 → ② 起静态托管（`deploy/serve.py`，监听 `$PORT`）→ ③ 把 `/relay` 前缀剥掉转到后端 → ④ 起 `api_loop` AI 身体（绑 `127.0.0.1:3020`，只在容器内可见）。

浏览器访问 **http://127.0.0.1:8080/**（`PORT` 默认 8080；想换就 `PORT=9000 bash deploy/entrypoint.sh`）。

> **地址就是根路径，没有 `/chat/`。** 网页由 `deploy/serve.py` 直接挂在 `/`（原版靠 nginx 写在 `/chat/`，Zeabur 上没有 nginx，所以我们简化成根路径）。

按下 `Ctrl+C` 停止（两个进程会一起退出）。

**✅ 2026-09-13 实测结果**（本机跑通，不是纸上谈兵）：

| 探测 | 结果 |
|---|---|
| `GET /` | **HTTP 200**（网页正常） |
| `GET /healthz` | `{"ok":true,"plugin_subs":0,"app_subs":0}` |
| `GET /relay/healthz` | `{"ok":true,...}`（`/relay` 前缀剥离正常） |

> 端口速记：**8080 = 网页（对外）**，**3020 = AI 身体（只在本机可见，别去访问它）**。
> 这条路径和 Zeabur 容器里跑的**是同一套**，所以本地跑通了 = 云上大概率也通。

---

### 方式 C：部署到 Zeabur（正式版）

**用途**：让 Kael 真正 24 小时活着，你关网页他也不消失。

完整逐步操作见工作区根目录的 **[`kael-home-Zeabur部署清单.md`](../kael-home-Zeabur部署清单.md)**（配合可视化版 [`kael-home建房清单-盐系风.html`](../kael-home建房清单-盐系风.html)）。

**一句话流程**：
1. `git push` 代码到 GitHub（你手动做）
2. Zeabur 上 **Add Service → Git** → 选 `874755174-tech/Tidal_Echo`
3. Zeabur 自动识别根目录 `Dockerfile` → 开始构建
4. **挂持久卷到 `/data`** ← 最关键，不挂的话重新部署会清空聊天记录
5. 在 Environment Variables 里填 `RELAY_SECRET` / `RELAY_DB=/data/relay.db` / `RELAY_UPLOAD_DIR=/data/uploads` 等
6. Generate Domain → 拿到公网域名

> 🔴 **部署前先确认手机的 iCloud 整机备份是开着的。** 涉及部署/迁移，第一件事永远是备份。

---

## 3. 常见问题

**Q：为什么本地文件夹叫 kael-home，GitHub 上还叫 Tidal_Echo？**
A：改名只改了本地。Zeabur 跟的是 GitHub 仓库名。要一起改也行，但需要重新指向仓库并可能触发重新部署，暂时没必要。

**Q：`pip install` 报错说找不到 requirements.txt？**
A：确认你在 `kael-home` 目录下，且 `backend/requirements.txt` 存在。用绝对路径最保险：
`C:\Users\86187\WorkBuddy\2026-07-26-20-49-33\kael-home\backend\requirements.txt`

**Q：网页打开是空白？**
A：确认地址是 **`http://127.0.0.1:8080/`**（**没有 `/chat/`**，结尾就是根斜杠）。再去终端看一眼 `[serve] backend=... web=...` 那行有没有出现——如果它说"没找到前端目录"，检查 `web/` 文件夹还在不在。

**Q：`/healthz` 通了但网页 404？**
A：说明后端活了但静态托管没挂上。看终端里 `[serve]` 那行输出的 `web=` 路径对不对。

**Q：密钥粘贴上去说"输入失败"？**
A：老版本会夹带零宽字符（已经修了）。万一还遇到，用工作区根目录的 `密钥体检.html` 贴一下，它会告诉你是哪几个字符有问题。

**Q：能改回文件夹名吗？**
A：能，`mv kael-home Tidal_Echo` 即可。但如果已经按这份文档做了别的事，记得同步改回来。

---

## 4. 边界（我们改了什么、没改什么）

| 目录/文件 | 状态 |
|---|---|
| `backend/` `examples/` `channel/` | ✅ **原版一字未改**（`git diff` 可证） |
| `web/index.html` | ⚠️ 4 处 UI 家装 + **应用名 4 处改为 Kael Home**（收按钮、纪念日 7/1、默认名 Kael、密钥清洗），**不含任何 KaelLife 逻辑** |
| `web/manifest.webmanifest` | ⚠️ 应用名 `Tidal Echo` → `Kael Home`（装到桌面显示的名字） |
| `web/sw.js` | ⚠️ 只改了缓存版本号（原作者注释要求每次改前端都要升，否则手机装的 PWA 一直显示旧版） |
| `Dockerfile` · `deploy/` · `.dockerignore` · `.gitattributes` | ➕ 我们新增的部署层，可整体删除而不影响原项目 |
| `KaelLife/` | ⬜ **完全没碰**（第一阶段红线） |

> **版权声明保留**：`web/index.html` 第 114 行（主题名 HARBOR）与第 2236 行（`Tidal Echo — PWA chat shell (open source)`）**故意没改**——那是 AGPL-3.0 的来源署名，必须留着。

---

## 5. 这次（2026-09-13）修掉的 3 个真 bug

改文件夹名时顺带做了完整实测，**发现部署脚本有 3 个隐藏问题**（在 Zeabur 容器里不会暴露，但一换环境就炸）：

| # | 问题 | 后果 | 修法 |
|---|---|---|---|
| 1 | `entrypoint.sh` 写死容器路径 `/app` | 本地跑时找不到 `api_loop.py` | 加 `APP_ROOT` 自动定位 |
| 2 | `entrypoint.sh` 用裸 `python` | 本地跑时报 `No module named uvicorn` | 自动优先用项目内 `.venv` |
| 3 | Git Bash 的 `/c/Users/...` 路径与 Windows Python 冲突 | 拼成 `C:\c\Users\...` 打不开 | 加路径规范化函数 |

> 第 3 条最隐蔽：**只在 Git Bash 下出现**。如果你以后在别的终端跑遇到 `can't open file 'C:\\c\\...'`，就是这个。

**实测结果（本机真实跑通）**：

| 探测 | 结果 |
|---|---|
| `GET /` | **HTTP 200**（网页正常） |
| `GET /manifest.webmanifest` | **HTTP 200** |
| `GET /sw.js` | **HTTP 200** |
| `GET /healthz` | **HTTP 200** → `{"ok":true,...}` |

---

_最后更新：2026-09-13 · 文件名：`启动说明-kael-home.md`_