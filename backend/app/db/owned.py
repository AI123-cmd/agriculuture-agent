"""归属安全的模型混入：所有业务表统一带 user_id 字段。

两个设计取舍：

1. 用 `declared_attr` 而非普通 `mapped_column`。多个子类继承同一混入时，
   SQLAlchemy 会尝试复用同一个 `Column` 对象并报 "Column object already
   assigned to Table"；`declared_attr` 让每个子类各自生成独立列对象。
2. `index=True` 写在混入里。全局约定要求"所有业务表带 user_id，索引必建"，
   放在混入里使新增表自动带上，不依赖执行者记得加。
"""

from typing import Protocol

from sqlalchemy import String
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class OwnedMixin:
    """归属字段混入。长度 String(36) 与全局约定的主键长度一致。"""

    @declared_attr
    def user_id(cls) -> Mapped[str]:
        return mapped_column(String(36), nullable=False, index=True, comment="归属用户ID")


class OwnedModel(Protocol):
    """供 OwnedRepository 做类型约束的最小协议。"""

    id: Mapped[str]
    user_id: Mapped[str]
