"""诊断节点导出与公共工具。"""

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncSession


def _db_from_config(config: RunnableConfig | None) -> AsyncSession | None:
    """从 RunnableConfig 取出 ai_service 注入的数据库会话。"""
    if config and config.get("configurable"):
        return config["configurable"].get("db")
    return None


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
]
