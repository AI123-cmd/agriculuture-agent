"""播报相关模型。"""

from pydantic import BaseModel


class BroadcastItem(BaseModel):
    id: str
    time: str
    title: str
    content: str
    done: bool
    type: str


class BroadcastTodayResponse(BaseModel):
    date: str
    items: list[BroadcastItem] = []


class MarkDoneRequest(BaseModel):
    itemId: str
