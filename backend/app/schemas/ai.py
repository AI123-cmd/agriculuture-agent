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


class HistoryMessage(BaseModel):
    id: int
    role: str
    content: str


class HistoryItem(BaseModel):
    id: str
    title: str
    preview: str
    updatedAt: int
    messages: list[HistoryMessage] = []


class HistoryResponse(BaseModel):
    # 字段名 `list` 是接口契约，必须保留；用 builtins.list 避免与字段名相互遮蔽
    list: builtins.list[HistoryItem] = Field(default_factory=list)
    total: int = 0
