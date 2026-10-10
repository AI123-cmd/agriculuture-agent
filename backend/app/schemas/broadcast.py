"""提示快照接口契约（T3-10）。

`/broadcast/snapshot` 的两个动作共用同一份快照视图 `SnapshotView`（定义在
`app.broadcast.view`，内部属性蛇形、接口输出小驼峰）：

- `SnapshotResponse`：POST「更新提示」的返回。除快照外带 `updateCompleted` 与频次提示
  `updateNotice`，前端据此决定是否在按钮下方显示"今日已更新 N 次，明天会有新内容"。
- `SnapshotTodayResponse`：GET 读当日快照的返回，只有快照。

`snapshot` 允许为 `None`：快照被标记 `invalidated` 时不返回内容（视同当日尚未生成），
前端展示空状态并引导农户点击更新，因而用可空字段而不是空对象表达。
"""

from pydantic import BaseModel

from app.broadcast.view import SnapshotView


class SnapshotResponse(BaseModel):
    """POST /broadcast/snapshot 的返回。"""

    snapshot: SnapshotView | None
    updateCompleted: bool
    updateNotice: str | None = None


class SnapshotTodayResponse(BaseModel):
    """GET /broadcast/snapshot 的返回。"""

    snapshot: SnapshotView | None


__all__ = ["SnapshotResponse", "SnapshotTodayResponse"]
