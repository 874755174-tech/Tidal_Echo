#!/bin/bash
# ============================================================================
# 容器入口 —— 一个容器里跑两个进程，都是 Tidal_Echo 自带的东西
# ============================================================================
#   relay    后端 app.py（+ PWA 静态）：消息落库 / SSE / 鉴权 / Web Push
#            监听 0.0.0.0:$PORT，对外，Zeabur 的流量进这里
#   api_loop 官方自带的「服务器端 API 身体」（examples/api_loop.py）
#            监听 127.0.0.1:3020，只在本容器内可见，不对外暴露
#
#   两者通过回环地址通信（relay 推 /loop/ingest，api_loop 回 /channel/out），
#   所以它们必须在同一个容器里 —— 这也正是「方案 A：一个服务」的含义。
#
# ⚠️ 本文件属于【我们的部署适配层】，不是 Tidal_Echo 原有能力。
#    里面**没有**任何 KaelLife 的代码或调用。
# ============================================================================
set -u

PORT="${PORT:-8080}"
export PORT

# ---- 项目根目录：自动定位，不写死 /app -------------------------------------
# 容器里是 /app；本地（Windows/macOS/Linux）跑时自动取本脚本的上级目录。
# 想强制指定就 export APP_ROOT=/your/path
#
# ⚠️ Git Bash 的 pwd 会返回 POSIX 风格路径（/c/Users/...），
#    而 Windows 版 Python 会把它拼成 C:\c\Users\... → 路径重复打不开。
#    所以这里把 /c/... 转回 C:/...；容器(/) 与 mac/linux 不受影响。
_norm_path() {
  case "$1" in
    /[a-zA-Z]/*)
      # /c/Users/...  →  C:/Users/...
      _d="$(printf '%s' "$1" | cut -c2 | tr '[:lower:]' '[:upper:]')"
      _r="$(printf '%s' "$1" | cut -c4-)"
      printf '%s:/%s' "$_d" "$_r"
      ;;
    *) printf '%s' "$1" ;;
  esac
}

if [ -z "${APP_ROOT:-}" ]; then
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  APP_ROOT="$(_norm_path "$(cd "$_here/.." && pwd)")"
else
  APP_ROOT="$(_norm_path "$APP_ROOT")"
fi
export APP_ROOT

# ---- Python 解释器：优先用项目内 .venv，其次 PATH 上的 python ---------------
# 容器里没有 .venv，直接用系统的 python（Dockerfile 已装好依赖）。
# 本地跑时若不自动切到 .venv，会报 "No module named uvicorn"。
if [ -z "${PYTHON:-}" ]; then
  if [ -x "$APP_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON="$APP_ROOT/.venv/Scripts/python.exe"          # Windows
  elif [ -x "$APP_ROOT/.venv/bin/python" ]; then
    PYTHON="$APP_ROOT/.venv/bin/python"                  # macOS / Linux
  else
    PYTHON="python"
  fi
fi
export PYTHON

echo "[entrypoint] APP_ROOT=$APP_ROOT"
echo "[entrypoint] PYTHON=$PYTHON"

# ---- 进程间地址（两个都在本容器内，走回环）---------------------------------
export RELAY_URL="${RELAY_URL:-http://127.0.0.1:${PORT}}"
export RELAY_LOOP_INGEST_URL="${RELAY_LOOP_INGEST_URL:-http://127.0.0.1:3020/loop/ingest}"

# ---- 持久化：默认全部落在 /data -------------------------------------------
# Zeabur 的容器磁盘是临时的（重新部署就清空），所以这些必须在 /data 下，
# 并在 Zeabur 面板把持久卷挂到 /data。不改这些 = 聊天记录随时会没。
export RELAY_DB="${RELAY_DB:-/data/relay.db}"
export RELAY_UPLOAD_DIR="${RELAY_UPLOAD_DIR:-/data/uploads}"
export RELAY_BRAIN_FILE="${RELAY_BRAIN_FILE:-/data/brain_target}"
export LOOP_CONFIG="${LOOP_CONFIG:-/data/api_loop.config.json}"
export VAPID_PRIVATE_PEM="${VAPID_PRIVATE_PEM:-/data/private_key.pem}"

mkdir -p "$(dirname "$RELAY_DB")" "$RELAY_UPLOAD_DIR" 2>/dev/null || true

# ---- VAPID 私钥：支持用 base64 环境变量注入 --------------------------------
# Zeabur 的变量面板不适合贴多行 PEM，所以约定：
#   VAPID_PRIVATE_PEM_B64 = base64(private_key.pem) 的一整行
# 首次启动时解开写到 /data/private_key.pem，之后就一直用这个文件。
# （没配就跳过，锁屏推送自动降级为不可用，不影响聊天。）
if [ -n "${VAPID_PRIVATE_PEM_B64:-}" ] && [ ! -f "$VAPID_PRIVATE_PEM" ]; then
  if printf '%s' "$VAPID_PRIVATE_PEM_B64" | base64 -d > "$VAPID_PRIVATE_PEM" 2>/dev/null; then
    chmod 600 "$VAPID_PRIVATE_PEM" 2>/dev/null || true
    echo "[entrypoint] VAPID 私钥已从 VAPID_PRIVATE_PEM_B64 写入 $VAPID_PRIVATE_PEM"
  else
    rm -f "$VAPID_PRIVATE_PEM"
    echo "[entrypoint] 警告：VAPID_PRIVATE_PEM_B64 解不开，锁屏推送将不可用" >&2
  fi
fi

echo "[entrypoint] PORT=$PORT  RELAY_DB=$RELAY_DB  LOOP_CONFIG=$LOOP_CONFIG"
if [ ! -f "$RELAY_BRAIN_FILE" ]; then
  echo "[entrypoint] 提示：还没有 brain_target —— 第一次打开网页后，"
  echo "[entrypoint]       去「设置」把接消息的一方切到 API loop，否则没人回话。"
fi

# ---- 进程守护：单个进程挂了就单独重启它，另一个继续服务 ----------------------
run_forever() {
  local name="$1"; shift
  while true; do
    echo "[entrypoint] starting $name ..."
    "$@"
    echo "[entrypoint] $name 退出了（code=$?），2 秒后重启" >&2
    sleep 2
  done
}

# 收到停止信号时，把整个进程组一起带走（容器优雅退出用）
trap 'echo "[entrypoint] stopping ..."; kill 0' TERM INT

# relay：对外，必须监听 0.0.0.0（原代码 app.py 里写死 127.0.0.1 的那行只影响
# `python app.py` 这种启动方式；这里用 uvicorn 命令行，所以无需改源码）
run_forever relay \
  "$PYTHON" -m uvicorn serve:app --host 0.0.0.0 --port "$PORT" --app-dir "$APP_ROOT/deploy" &

# api_loop：官方自带的临时身体，绑 127.0.0.1:3020（它自己的默认值，正好合适）
run_forever api_loop "$PYTHON" "$APP_ROOT/examples/api_loop.py" &

wait
