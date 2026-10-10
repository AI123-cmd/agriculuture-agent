"""提示快照服务：把生成链路（T3-08 的 `app.broadcast.snapshot`）包装成接口层要的形状。

两个方法对应 `/broadcast/snapshot` 的两种语义：

- `generate`（POST）：农户主动点「更新提示」时调用。先记一次点击计数，再走生成链路拿当日
  快照；返回值额外带 `updateCompleted` 与频次提示 `updateNotice`，前端据此决定是否在按钮
  下方显示"今日已更新 N 次，明天会有新内容"。
- `get_today`（GET）：取当日快照。按 T3-08 的落地口径同样委托 `generate_snapshot`——生成
  链路自身是幂等的（同日已有有效快照即原样返回），因此 GET 与 POST 内容一致，差别只在 POST
  会额外触发点击计数与频次提示。

**出参组装委托 T3-10 的 `app.broadcast.view`**：跨日弱化状态（`display_status` / `days_ago`）、
环境来源标注（`env_note`）、被抑制条目的过滤、紧急条目的 `emphasis` 全部由视图模块算好，
本层只负责取数、接线与拼装响应模型，不再自行摊平 dict，避免展示口径在两处漂移。

`invalidated=True` 的快照不返回内容（`snapshot=None`）：视同当日尚未生成，前端展示空状态并
引导农户点击更新。展示列表只含 `suppressed=False` 的条目，过滤在视图模块内完成。
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.snapshot import build_update_notice, generate_snapshot, register_click
from app.broadcast.view import build_snapshot_view
from app.schemas.broadcast import SnapshotResponse, SnapshotTodayResponse


class BroadcastService:
    """快照服务：生成（POST 语义）与查询（GET 语义）。"""

    async def generate(self, db: AsyncSession, user_id: str, farm_entry_id: str | None) -> SnapshotResponse:
        """生成/刷新当日快照（POST 语义）。

        `register_click` 先于生成调用：点击计数只服务频次文案、不参与任何业务判定，先记一次
        才能保证"本次点击"被计入，即便后续生成走的是幂等命中分支。
        `merged` 传 `True` 表示本次已把新信号并入快照（生成链路完成合并去重后返回）。
        """
        today = date.today()
        click = await register_click(user_id, today)
        snapshot = await generate_snapshot(db, user_id, farm_entry_id)
        return SnapshotResponse(
            snapshot=build_snapshot_view(snapshot, today=today),
            updateCompleted=True,
            updateNotice=build_update_notice(click, True),
        )

    async def get_today(self, db: AsyncSession, user_id: str, farm_entry_id: str | None) -> SnapshotTodayResponse:
        """取当日快照（GET 语义），委托生成链路以保证与 POST 同源同口径。"""
        today = date.today()
        snapshot = await generate_snapshot(db, user_id, farm_entry_id)
        return SnapshotTodayResponse(snapshot=build_snapshot_view(snapshot, today=today))


__all__ = ["BroadcastService"]
