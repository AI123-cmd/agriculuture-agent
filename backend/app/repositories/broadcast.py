"""农事播报日志仓储（仅保留注销清理用途）。

`BroadcastLog` 是旧播报链路的数据表，按 T3-01 的规划在 T3-08 完成后停止写入、
由 `prompt_snapshot` / `prompt_item` 取代。在新链路完成前，注销账号仍需清空该表的
本人数据，故保留本仓储；旧链路的 `list_by_date` / `mark_done` 等流程方法已随旧接口
一并废弃，不再提供。
"""

from app.models import BroadcastLog
from app.repositories.base import OwnedRepository


class BroadcastLogRepository(OwnedRepository[BroadcastLog]):
    def __init__(self) -> None:
        super().__init__(BroadcastLog)


broadcast_repo = BroadcastLogRepository()

__all__ = ["BroadcastLogRepository", "broadcast_repo"]
