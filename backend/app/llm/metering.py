"""token 计量（T2-59 的最小实现）。

网关在候选链上的每一次**成功**调用后回写一条计量记录。失败的调用不写：失败响应没有
可信的 token 数，写进去只会污染成本统计。失败次数由熔断器承担，与计量分开。

落库（`llm_call` 表）、成本估算与聚合查询在 T2-59 的任务范围内补齐。本阶段提供接口与
一个空实现，让网关的编排逻辑可以在无数据库的前提下被完整测试。
"""

from typing import Any, Protocol


class TokenMeter(Protocol):
    """计量接口。网关只依赖这一个方法。"""

    async def record(
        self,
        task_type: Any,
        response: Any,
        candidate: Any,
        *,
        is_fallback: bool = False,
    ) -> None: ...


class NullTokenMeter:
    """不落库的计量实现。用于无数据库的单元测试与未装配计量的场景。"""

    async def record(
        self,
        task_type: Any,
        response: Any,
        candidate: Any,
        *,
        is_fallback: bool = False,
    ) -> None:
        return None


__all__ = ["NullTokenMeter", "TokenMeter"]
