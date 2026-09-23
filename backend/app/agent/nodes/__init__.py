"""诊断节点导出与公共工具。"""

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import Repositories


def _db_from_config(config: RunnableConfig | None) -> AsyncSession | None:
    """从 RunnableConfig 取出 ai_service 注入的数据库会话。"""
    if config and config.get("configurable"):
        return config["configurable"].get("db")
    return None


def _repos_from_config(config: RunnableConfig | None) -> Repositories | None:
    """取出数据访问入口（仓储 + 会话的绑定对象），供工具层使用。

    `app/tools/` 的签名接收 `Repositories` 而不是 `AsyncSession`，ORM 类型因此不再
    沿着调用链渗进节点层。本函数是节点侧唯一的组装点；`app/agent/` 不做仓储读写，
    只把对象透传给工具。节点自身仍需要裸会话的场景（如 `diagnose` 注入记忆）
    继续用 `_db_from_config`，那部分重构留给 T2-13。
    """
    db = _db_from_config(config)
    return Repositories(db) if db is not None else None


# _db_from_config 必须先于子模块导入定义：各节点通过 `from app.agent.nodes import` 取用它，
# 若移到文件末尾会形成包初始化期的循环导入。E402 在此为必要例外，已在 pyproject 登记豁免。
from .collect_management import collect_management
from .collect_symptoms import collect_symptoms
from .diagnose import diagnose
from .fetch_environment import fetch_environment
from .intake import intake
from .prescribe import prescribe

__all__ = [
    "intake",
    "collect_symptoms",
    "collect_management",
    "fetch_environment",
    "diagnose",
    "prescribe",
    "_db_from_config",
    "_repos_from_config",
]
