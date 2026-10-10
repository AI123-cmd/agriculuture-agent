"""农事台账与执行回执接口契约（T3-15，技术方案 13.1）。

三个模型服务两条接口：

- `LedgerSaveResponse`：`POST /ai/prescription/{id}/ledger` 的返回。回执也带上台账条目的
  关键字段，使前端存入成功后可立即渲染这条记录，不必再发一次列表查询；`ledgerSaved=True`
  供前端把按钮切到"已存入台账"状态（幂等点击的抑制）。`pesticides` 是入台账时从处方卡复制
  的自包含快照——知识条目日后 `deprecated` 也不影响这里的展示（台账必须自包含）。
- `ExecuteRequest`：`POST /broadcast/items/{id}/execute` 的入参。`executed=True` 表示勾选已
  执行，`False` 表示取消（13.1 注明"可勾选已执行（可取消）"）。
- `ExecuteResponse`：勾选结果，回传 `executed` 与 `executedAt`，使前端无需再查一次即可刷新
  勾选状态。取消时 `executedAt` 为 `None`（与服务层"取消即清空时间戳"一一对应）。

**刻意不设 `user_id` 字段**：台账与提示条目是农户的私有农事记录，`user_id` 只能从令牌取
（路由层 `get_current_user`），允许客户端指定会造成越权读写（产品说明书 4.5.2 功能点 4）。

字段名用小驼峰直写（与 `app/schemas/consult.py`、`app/schemas/subscription.py` 同一风格）。
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class LedgerSaveResponse(BaseModel):
    """处方卡存入台账后的返回：台账条目视图 + `ledgerSaved` 状态标记。"""

    entryId: str
    entryDate: date
    processId: str | None = None
    title: str | None = None
    actions: list[dict[str, Any]] = []
    pesticides: list[dict[str, Any]] = []
    safetyIntervalDays: int | None = None
    followupNeeded: bool = False
    ledgerSaved: bool = True


class ExecuteRequest(BaseModel):
    """执行勾选入参：`True` 勾选、`False` 取消（一次点击完成）。"""

    executed: bool


class ExecuteResponse(BaseModel):
    """执行勾选结果：`executed` 与勾选时点；取消时 `executedAt` 为 `None`。"""

    id: str
    executed: bool
    executedAt: datetime | None = None


__all__ = ["ExecuteRequest", "ExecuteResponse", "LedgerSaveResponse"]
