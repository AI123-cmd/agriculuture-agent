"""归属安全的模型混入：所有业务表统一带 user_id 字段。

两个设计取舍：

1. 用普通 `mapped_column` 而非 `declared_attr`。SQLAlchemy 的混入支持会在建表时
   为**每个子类各自复制一份列对象与索引**（实测 9 个子类得到 9 个独立列、各自一份
   `ix_*_user_id`），不存在"复用同一 Column 对象"的问题；子类自行声明 `user_id`
   时混入那一份被丢弃。相比 `declared_attr`，普通写法让 mypy 能识别子类属性。
2. `index=True` 写在混入里。全局约定要求"所有业务表带 user_id，索引必建"，
   放在混入里使新增表自动带上，不依赖执行者记得加。
"""

from typing import Any, Protocol

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class OwnedMixin:
    """归属字段混入。长度 String(36) 与全局约定的主键长度一致。"""

    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="归属用户ID")


class OwnedModel(Protocol):
    """供 OwnedRepository 做类型约束的最小协议。

    属性标注用 `Any`，这是当前 mypy 配置（未启用 `sqlalchemy.ext.mypy.plugin`）
    下唯一可行的写法，两种直觉写法都无法通过：

    - `id: str` / `user_id: str`：声明式模型实例上这两项是 `str`，但类对象上
      是 `InstrumentedAttribute[str]`；仓储既用类属性构造查询（`self.model.user_id
      == uid`，需要列对象）又用实例属性赋值（`obj.user_id = uid`），单一标注无法
      同时满足，mypy 会因 `type[A]` 不满足协议而报 type-var。
    - `id: Mapped[str]`（任务文件原型）：模型实例上不是 `Mapped`，同样不满足。

    用 `Any` 后协议只承担"存在这两个字段"的约束，而仓储对外签名仍返回
    `ModelT | None` / `Sequence[ModelT]` 等具体类型，`Any` 不外泄。
    """

    id: Any
    user_id: Any
