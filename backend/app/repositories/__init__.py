"""数据访问层入口：暴露各仓储单例。

仓储是纯逻辑对象（无状态、无配置），故用模块级单例而非 `lru_cache` 工厂。
"""

from app.repositories.base import OwnedRepository
from app.repositories.conversation import ConversationRepository, conversation_repo

__all__ = [
    "OwnedRepository",
    "ConversationRepository",
    "conversation_repo",
]
