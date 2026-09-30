"""模型调用记录表（T2-59，技术方案 10.1 计量与审计 / 10.4 成本控制）。

每次模型调用落一条记录，两个职责：**成本归属**（哪个流程哪个环节花钱最多）与
**版本对比**（模型升级前后同任务的消耗变化）。它是旁路数据——写失败不影响模型调用，
业务事务回滚也不应把它带走。

主键用 `BigInteger`（BIGSERIAL）而不是 UUID 字符串：这是一张高写入量的记录表，整数
主键的索引更紧凑，且它不承载归属（归属由 `user_id` 表达）。这与全局约定"时序表用
`BIGSERIAL`"的口径一致。

`cost_micros` 用整数存微元（1e-6 元）而不是浮点存元：成本要拿来做预算判断，浮点累加
在长期统计里会出现分位偏移。展示时除以 1e6 转为元。

**只追加**：本表不提供更新与删除路径。`input_digest` 存输入哈希而非原文，与 T2-55 的
内容审计一致——农事问诊内容含地块与作物状况，属敏感数据，落库的只能是哈希与统计。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_TABLE_COMMENT = "模型调用记录：只追加，用于成本归属与版本对比"


class LlmCall(Base):
    """一次模型调用记录。只追加，用于成本归属与版本对比。"""

    __tablename__ = "llm_call"
    __table_args__ = (
        # 维度 + 时间的复合索引：看板按日/周/月查某维度，`ix_llm_call_task_created`
        # 与 `ix_llm_call_agent_created` 支撑"时间范围加维度"的聚合。
        Index("ix_llm_call_task_created", "task_type", "created_at"),
        Index("ix_llm_call_agent_created", "agent", "created_at"),
        {"comment": _TABLE_COMMENT},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 匿名调用（未登录）可为空；有归属时用于成本按用户分摊。
    user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, comment="归属用户ID，匿名调用可为空"
    )
    consultation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    process_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(30), nullable=False, index=True, comment="智能体名")
    task_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(10), nullable=False, comment="模型档位")
    provider: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(60), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(40), nullable=True, comment="供应商返回的版本号")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="成本，单位 1e-6 元")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="是否走了备选供应商")
    is_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="是否降档（私有化候选）")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="成功或最终失败")
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="输入哈希，不留原文")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


__all__ = ["LlmCall"]
