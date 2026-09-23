"""诊断节点导出与公共工具。"""
from sqlalchemy.orm import Session


def _db_from_config(config) -> Session | None:
    """从 RunnableConfig 取出 ai_service 注入的数据库会话。"""
    if config and config.get("configurable"):
        return config["configurable"].get("db")
    return None


from .intake import intake  # noqa: E402
from .collect_symptoms import collect_symptoms  # noqa: E402
from .collect_management import collect_management  # noqa: E402
from .fetch_environment import fetch_environment  # noqa: E402
from .diagnose import diagnose  # noqa: E402
from .prescribe import prescribe  # noqa: E402

__all__ = [
    "intake",
    "collect_symptoms",
    "collect_management",
    "fetch_environment",
    "diagnose",
    "prescribe",
    "_db_from_config",
]