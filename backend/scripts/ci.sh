#!/usr/bin/env bash
# 本地与 CI 共用的门禁序列（T1-12）。
#
# 用法：bash scripts/ci.sh
# 退出码：0 通过，非 0 失败（首个失败即中止）。
#
# 为什么不把命令散在 ci.yml 与文档里：本地与 CI 用同一份脚本，才能避免
# "本地过了 CI 挂"。新增门禁只改这里一处。
set -euo pipefail

cd "$(dirname "$0")/.."

# 优先用仓库内虚拟环境的解释器：CI 与本地都据此确定依赖版本，
# 避免 PATH 上的全局 python 缺 pgvector / asyncpg 导致误报。
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"          # Windows (Git Bash / MSYS)
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"                  # Linux / macOS
else
  PY="python"
fi

step() {
  echo ""
  echo "=== $* ==="
}

# 顺序：格式化与 lint → 类型 → 分层依赖。
# 分层依赖放在类型检查之后：分层违规常同时表现为类型错误，先报基础门禁能少一轮往返。
step "ruff format --check"
"$PY" -m ruff format --check app tests

step "ruff check"
"$PY" -m ruff check app tests

step "mypy app"
"$PY" -m mypy app

step "check_layers"
"$PY" scripts/check_layers.py

step "pytest"
"$PY" -m pytest -q

echo ""
echo "scripts/ci.sh 通过"
