"""知识条目核准接口契约（T2-54）。

后台运营账号调用的入参。`action` 直接复用状态机枚举 `KnowledgeAction`，使接口层与状态机
共用一份动作定义——两处各写一遍字面量必然漂移。`operator_id` **不出现在入参里**：它由令牌
推导（见 `app/api/deps.py` 的 `require_review_operator`），调用方无法伪造成他人。
"""

from pydantic import BaseModel, Field

from app.services.knowledge_review import KnowledgeAction


class KnowledgeReviewRequest(BaseModel):
    """一次状态迁移的入参。`reason` 对 `suspend` / `reject` / `deprecate` 必填，由服务层校验。"""

    action: KnowledgeAction
    reason: str | None = Field(default=None, description="暂停/打回/废弃必填")
    consultedRef: str | None = Field(default=None, description="触发本次变更的问诊 id，可选")


__all__ = ["KnowledgeReviewRequest"]
