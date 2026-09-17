#!/usr/bin/env bash
# vulnhound 一键启动：环境安装 + 镜像构建 + 起服务
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8900}"

# 1) venv + 依赖
if [ ! -d .venv ]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
.venv/bin/pip install -q -e ".[dev]"

# 2) agent 沙箱镜像
if ! docker image inspect vh-agent >/dev/null 2>&1; then
  echo "==> 构建 Docker 镜像 vh-agent"
  docker build -t vh-agent agent/
else
  echo "==> 镜像 vh-agent 已存在（如需重建：docker build -t vh-agent agent/）"
fi

# 3) LLM key
if [ -z "${LLM_API_KEY:-}" ]; then
  echo "!! 未设置 LLM_API_KEY（DeepSeek）。请 export LLM_API_KEY=sk-... 后重试。" >&2
  exit 1
fi

# 4) 起服务
echo "==> 启动 http://127.0.0.1:${PORT}"
exec .venv/bin/uvicorn server.app:app --port "${PORT}"
