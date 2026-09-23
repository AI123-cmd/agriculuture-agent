#!/usr/bin/env bash
# T1-04 CI 门禁：空库 upgrade、回滚一步、再前滚，并拦住伪迁移回潮。
#
# 用法：bash scripts/check_migrations.sh
# 退出码：0 通过，非 0 失败。
set -euo pipefail

cd "$(dirname "$0")/.."

# 1) 禁止 metadata.create_all / drop_all 回潮：迁移必须是显式 DDL 序列
if grep -rn "metadata\.create_all\|metadata\.drop_all" app alembic; then
  echo "禁止在应用与迁移中使用 metadata.create_all/drop_all，请改为显式迁移" >&2
  exit 1
fi

# 2) 迁移中不得用 sa.inspect 猜列是否存在
if grep -rn "sa\.inspect" alembic/versions/; then
  echo "迁移中不得用 sa.inspect 猜列，请写明确的 DDL" >&2
  exit 1
fi

# 3) 空库跑到 head，回滚一步，再前滚
alembic downgrade base
alembic upgrade head
alembic downgrade -1
alembic upgrade head

# 4) 库中版本号必须等于代码期望值
CURRENT="$(alembic current 2>/dev/null | tail -n 1 | awk '{print $1}')"
EXPECTED="$(python -c 'from app.core.startup_check import EXPECTED_REVISION; print(EXPECTED_REVISION)')"
if [ "$CURRENT" != "$EXPECTED" ]; then
  echo "迁移版本不匹配：数据库为 $CURRENT，代码期望 $EXPECTED" >&2
  exit 1
fi

echo "check_migrations.sh 通过（当前版本 $CURRENT）"
