"""AI 对话相关模型。"""

import builtins

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    sessionId: str
    cropTag: str | None = None
    imageUrl: str | None = None


class ChatResponse(BaseModel):
    sessionId: str
    reply: str
    relatedSuggestions: list[str] = []
    tagsAdded: list[str] = []


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
