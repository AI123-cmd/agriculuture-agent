"""提示快照服务：把生成链路（T3-08 的 `app.broadcast.snapshot`）包装成接口层要的形状。

两个方法对应 `/broadcast/snapshot` 的两种语义：

- `generate`（POST）：农户主动点「更新提示」时调用。先记一次点击计数，再走生成链路拿当日
  快照；返回值额外带 `updateCompleted` 与频次提示 `updateNotice`，前端据此决定是否在按钮
  下方显示"今日已更新 N 次，明天会有新内容"。
- `get_today`（GET）：取当日快照。按 T3-08 的落地口径同样委托 `generate_snapshot`——生成
  链路自身是幂等的（同日已有有效快照即原样返回），因此 GET 与 POST 内容一致，差别只在 POST
  会额外触发点击计数与频次提示。

**序列化在此手工完成**，不引入 `app/schemas/broadcast.py`：接口契约的统一视图模型由 T3-10
的 `app/broadcast/view.py` 给出，本层先把 T3-08 需要的字段拼出来，避免两套模型先行打架。

条目列表只含 `suppressed=False` 的行：被条数控制压制的条目仍留在库里供产品复盘（T3-06），
但不进展示列表。过滤放在此处而非交给前端，避免某处漏判就把被压制的提示露出去。

`displayStatus` 按 `snapshot_date` 与今天计算：同日为 `today`，跨日为 `older`——跨日弱化的
完整三值口径（`today/yesterday/stale` 加 `days_ago`）属于 T3-10，本层不做前置实现。
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.broadcast.snapshot import build_update_notice, generate_snapshot, register_click
from app.models import PromptItem, PromptSnapshot


class BroadcastService:
    """快照服务：生成（POST 语义）与查询（GET 语义）。"""

    async def generate(self, db: AsyncSession, user_id: str, farm_entry_id: str | None) -> dict:
        """生成/刷新当日快照（POST 语义）。

        `register_click` 先于生成调用：点击计数只服务频次文案、不参与任何业务判定，先记一次
        才能保证"本次点击"被计入，即便后续生成走的是幂等命中分支。
        `merged` 传 `True` 表示本次已把新信号并入快照（生成链路完成合并去重后返回）。
        """
        today = date.today()
        click = await register_click(user_id, today)
        snapshot = await generate_snapshot(db, user_id, farm_entry_id)
        return {
            "snapshot": _to_snapshot_dict(snapshot, today),
            "updateCompleted": True,
            "updateNotice": build_update_notice(click, True),
        }

    async def get_today(self, db: AsyncSession, user_id: str, farm_entry_id: str | None) -> dict:
        """取当日快照（GET 语义），委托生成链路以保证与 POST 同源同口径。"""
        return {"snapshot": _to_snapshot_dict(await generate_snapshot(db, user_id, farm_entry_id), date.today())}


def _to_item_dict(item: PromptItem) -> dict:
    """一条展示条目：五段式字段（呼告/事实/影响/行动）+ 级别 + 执行勾选。

    `sourceKind` 与 `ruleId` 一并给出：前者供前端区分三路输入，后者在"同日更新"时用于
    与既有条目做差集（T3-08 第二节），前端不必自行推断。
    """
    return {
        "itemId": item.id,
        "sourceKind": item.source_kind,
        "severity": item.severity,
        "callout": item.callout,
        "fact": item.fact,
        "impact": item.impact,
        "action": item.action,
        "suppressed": bool(item.suppressed),
        "executed": bool(item.executed),
        "ruleId": item.rule_id,
    }


def _to_snapshot_dict(snapshot: PromptSnapshot, today: date) -> dict:
    """把快照 ORM 对象摊平为接口返回的 dict；展示列表只含未被压制的条目。

    `items` 走 `getattr` 取值：条目列表由生成链路在构造快照对象时挂上（不经 ORM 关系加载），
    缺省时按空列表处理，避免"没有条目"变成一次 AttributeError。
    """
    items = [item for item in (getattr(snapshot, "items", None) or ()) if not item.suppressed]
    return {
        "snapshotId": snapshot.id,
        "farmEntryId": snapshot.farm_entry_id,
        "revision": snapshot.revision,
        "generatedAt": snapshot.generated_at.isoformat(),
        "envSource": snapshot.env_source,
        "displayStatus": "today" if snapshot.snapshot_date == today else "older",
        "items": [_to_item_dict(item) for item in items],
    }


__all__ = ["BroadcastService"]
