"""AI 对话历史模型。

问诊出入参已迁至 `app/schemas/consult.py`（T2-33）；本模块只保留历史列表模型。
"""

import builtins

from pydantic import BaseModel, Field


class HistoryItem(BaseModel):
    """历史列表的一行。

    T1-11 起不再带 `messages`：列表页只需要标题、预览、时间，完整会话走
    `GET /api/v1/ai/consult/{id}`。原实现把每条会话的全部消息塞进列表响应，
    一页 20 条会话可能要传上千条消息，只为渲染 20 行摘要。
    """

    id: str
    title: str
    preview: str
    messageCount: int
    updatedAt: int


class HistoryResponse(BaseModel):
    # 字段名 `list` 是接口契约，必须保留；用 builtins.list 避免与字段名相互遮蔽
    list: builtins.list[HistoryItem] = Field(default_factory=list)
    total: int = 0


class MessageItem(BaseModel):
    """会话详情里的一条消息。`role` 固定 user / ai，与落库口径一致。"""

    role: str
    content: str
    imageUrl: str | None = None


class ConversationDetail(BaseModel):
    """单个会话的完整消息。历史列表不返回消息，回看走本模型。"""

    sessionId: str
    title: str
    messages: builtins.list[MessageItem] = Field(default_factory=list)

