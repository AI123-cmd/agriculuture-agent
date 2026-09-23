"""AI 对话相关模型。"""
from typing import List

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
    list: List[HistoryItem] = Field(default_factory=list)
    total: int = 0