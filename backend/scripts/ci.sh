#!/usr/bin/env bash
# 本地与 CI 共用的门禁序列（T1-12 建立，T6-11 扩展为完整清单）。
#
# 用法：bash scripts/ci.sh
# 退出码：0 通过，非 0 失败（首个失败即中止）。
#
# 为什么不把命令散在 ci.yml 与文档里：本地与 CI 用同一份脚本，才能避免
# "本地过了 CI 挂"。新增门禁只改这里一处。
#
# **第 1 至 7 项与 .github/workflows/ci.yml 逐条一致**（第 8 项评测按需跑，CI 里独立成
# `eval-regression.yml` 作业）。`tests/test_ci_config.py` 会文本比对两边的命令序列，
# 改这里就必须同步改 ci.yml，否则测试失败。
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

# 顺序：静态检查在前（失败最快、反馈最便宜），迁移与测试在后（需要数据库与更多耗时）。
# 分层依赖放在类型检查之后：分层违规常同时表现为类型错误，先报基础门禁能少一轮往返。

step "1 ruff check"
"$PY" -m ruff check app

step "2 ruff format --check"
"$PY" -m ruff format --check app

step "3 mypy app"
"$PY" -m mypy app

step "4 分层依赖检查（不允许 noqa 跳过）"
"$PY" scripts/check_layers.py
"$PY" scripts/check_no_noqa.py

step "5 迁移可升降"
"$PY" -m alembic upgrade head
"$PY" -m alembic downgrade -1
"$PY" -m alembic upgrade head

step "6 闸门规则单测"
"$PY" -m pytest -q tests/gate

step "7 图路径测试"
"$PY" -m pytest -q tests/graph

# 第 8 项：离线评测集回归。默认不跑（它比其余用例慢，且 CI 里已有独立作业）；
# 需要时 `CI_RUN_EVAL=1 bash scripts/ci.sh` 打开。
if [ "${CI_RUN_EVAL:-0}" = "1" ]; then
  step "8 离线评测集回归"
  "$PY" -m pytest -q -m eval tests/eval
fi

step "9 其余测试（契约与集成）"
"$PY" -m pytest -q -p no:randomly tests/contracts tests/

echo ""
echo "scripts/ci.sh 通过"
