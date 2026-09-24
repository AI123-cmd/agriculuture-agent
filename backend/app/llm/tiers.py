"""模型档位定义与任务类型到档位的默认映射（T2-57）。

档位是"能力等级"的抽象，具体模型名与供应商链由环境配置注入（开发环境可用轻量模型
占位，生产环境换正式模型，业务代码不动）。这一点对成本控制很重要：小模型档位在预发
环境可用更便宜的模型跑通链路。

安全闸门不在这个映射里——`TaskType` 中没有闸门项。方案 4.4 明确"安全闸门不使用大模型，
确定性实现"。若有人试图给闸门加一个 `TaskType` 走模型，会因为映射里没有对应项而在
装配期报错，这是"闸门不依赖大模型"从架构层面的保证。
"""

from dataclasses import dataclass
from enum import Enum

from app.llm.enums import TaskType


class ModelTier(str, Enum):
    """模型档位。"""

    SMALL = "small"  # 小模型：分类、抽取、改写
    MEDIUM = "medium"  # 中等模型：检索改写、相关性判定
    PRIMARY = "primary"  # 主模型：问诊、判定
    VISION = "vision"  # 垂类图像加多模态：现场采集


@dataclass(frozen=True, slots=True)
class TierSpec:
    """一个档位的配置：模型名、供应商链、token 上限、上下文轮数。

    `provider_chain` 按优先级排列，最后一项约定为私有化降档候选（T2-58 的降级链：
    主供应商 A 到同档备选 B 到私有化模型）。供应商名对应 `ModelGateway` 持有的
    适配器字典的键。
    """

    tier: ModelTier
    model: str
    provider_chain: tuple[str, ...]
    max_tokens: int
    max_history_rounds: int


# 按方案 4.4 的表格逐项落实。`TaskType.TRIAGE` 用 SMALL（分类加槽位抽取，输出枚举，
# 可被规则校验）；`OBSERVE` 用 VISION（图像候选加事实归纳）；`INTERVIEW` 与
# `DIAGNOSE` 用 PRIMARY（直接影响体验与准确性）；`RETRIEVE` 用 MEDIUM；
# `PRESCRIBE`/`SNAPSHOT_WRITER`/`REPORT_GUARD` 用 SMALL（话术改写与模板转述，
# 不得新增事实，能力需求低）。
DEFAULT_TIER_MAP: dict[TaskType, ModelTier] = {
    TaskType.TRIAGE: ModelTier.SMALL,
    TaskType.OBSERVE: ModelTier.VISION,
    TaskType.INTERVIEW: ModelTier.PRIMARY,
    TaskType.RETRIEVE: ModelTier.MEDIUM,
    TaskType.DIAGNOSE: ModelTier.PRIMARY,
    TaskType.PRESCRIBE: ModelTier.SMALL,
    TaskType.PANEL: ModelTier.PRIMARY,
    TaskType.SNAPSHOT_WRITER: ModelTier.SMALL,
    TaskType.REPORT_GUARD: ModelTier.SMALL,
}


__all__ = ["DEFAULT_TIER_MAP", "ModelTier", "TierSpec"]
