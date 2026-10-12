"""每日模型输出质量抽检（T6-09，技术方案 14.5）。

抽检是三层评估体系的第三层里唯一需要**人工**的环节：模型输出无法用规则完全判定，
故每日按流程分层抽样、入库、由人标注。本模块只做"抽样与入库"，不含标注动作——标注由
后台运营写入 `prescription_card.quality_label` / `quality_labeled_at` 与对应的
`eval_run` 行（两处同写，见 `app/models/eval_run.py` 的模块说明）。

三个刻意的设计：

1. **按流程分层，每条流程至少 3 例**（14.5 原文），不随机全局抽。全局随机会让 `rapeseed`、
   `pig` 这类低频流程长期不被抽到，而恰恰低频流程的提示词最缺验证。下限取
   `Settings.QUALITY_SAMPLE_MIN_PER_PROCESS` 与 3 的较大者——配置可以把下限调高，但不能
   低于文档要求。
2. **样本来自 `prescription_card`（已开方）而非全部问诊**：抽检目标是"模型改写的质量"，
   没有处方的问题不涉及改写。
3. **幂等**：以 `(run_date, prescription_card_id)` 唯一约束避免重复入样。本服务先查当天
   已入样的卡再抽样（`exclude_ids`），补跑当天不会产生重复行；数据库的部分唯一索引
   （`uq_eval_run_quality_sample`）是兜底，不依赖"先查后插"的时序正确。

**样本不足不补历史**：某条流程当日不足下限时，返回实际条数并在结果里标 `insufficient`，
不静默拿历史数据凑数——凑数会让"今天的提示词质量"被昨天的样本污染。
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId
from app.core.config import get_settings
from app.db.base import new_id
from app.models import EvalRun
from app.models.eval_run import KIND_QUALITY_SAMPLE
from app.repositories.metrics import MetricsRepository
from app.repositories.prescription import prescription_repo

# 文档硬要求的下限（14.5"每条流程至少 3 例"）。配置只能调高，不能低于它。
_DOC_MIN_PER_PROCESS = 3


@dataclass(frozen=True, slots=True)
class SampleItem:
    """一条抽检样本。`quality_label` / `quality_labeled_at` 入样时均为 `None`，待人工标注。"""

    prescription_card_id: str
    process_id: str
    quality_label: str | None = None
    quality_labeled_at: object | None = None


@dataclass(frozen=True, slots=True)
class SampleBatch:
    """一次抽样的结果。

    `items` 是**本次新建**的样本（幂等补跑时为空元组）；`insufficient` 是当日候选不足下限的
    流程 id 清单，供运维判断"是数据少还是抽样漏了"。
    """

    run_date: date
    items: tuple[SampleItem, ...]
    insufficient: tuple[str, ...]


class QualitySampleService:
    """每日按流程分层抽样。构造时绑定会话，方法自行读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._metrics = MetricsRepository(session)

    async def build_daily_batch(self, day: date) -> SampleBatch:
        """构建某日抽检批次并入库，返回本次新建的样本。

        幂等：先取当天已入样的卡 id 作为 `exclude_ids`，七条流程依次抽取；重复调用同一 `day`
        只会补充"上次没抽到"的空缺（当天候选已经全部入样时不再新增）。

        `insufficient` 按"当天**累计**条数"判定：补跑时若只看本次新抽的条数，会把已经达标的
        流程误报成样本不足。
        """
        limit = max(int(get_settings().QUALITY_SAMPLE_MIN_PER_PROCESS), _DOC_MIN_PER_PROCESS)
        sampled = await self._metrics.existing_quality_sample_card_ids(day)
        counts = await self._metrics.existing_quality_sample_counts(day)
        items: list[SampleItem] = []
        insufficient: list[str] = []
        rows: list[EvalRun] = []
        for process in ProcessId:
            picked = await prescription_repo.pick_for_quality_sample(
                self._session, process.value, day, limit=limit, exclude_ids=sampled
            )
            if counts.get(process.value, 0) + len(picked) < limit:
                insufficient.append(process.value)
            for card in picked:
                sampled.add(card.id)
                items.append(SampleItem(prescription_card_id=card.id, process_id=process.value))
                rows.append(
                    EvalRun(
                        id=new_id("evr"),
                        kind=KIND_QUALITY_SAMPLE,
                        run_date=day,
                        process_id=process.value,
                        prescription_card_id=card.id,
                        quality_label=None,
                        quality_labeled_at=None,
                        triage_correct=None,
                        payload={},
                    )
                )
        if rows:
            await self._metrics.insert_quality_samples(day, rows)
        return SampleBatch(run_date=day, items=tuple(items), insufficient=tuple(insufficient))


__all__ = ["QualitySampleService", "SampleBatch", "SampleItem"]
