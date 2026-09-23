"""临时探测：子类能否覆盖混入的 declared_attr 列并保留外键/唯一索引。"""

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class B(DeclarativeBase):
    pass


class OwnedMixin:
    @declared_attr
    def user_id(cls) -> Mapped[str]:
        return mapped_column(String(36), nullable=False, index=True, comment="归属用户ID")


class User(B):
    __tablename__ = "user"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)


class Plain(B, OwnedMixin):
    __tablename__ = "plain"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)


class Special(B, OwnedMixin):
    __tablename__ = "special"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("user.id", name="fk_special_user", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )


for cls in (Plain, Special):
    cols = [c.name for c in cls.__table__.columns]
    ix = [(i.name, i.unique, [c.name for c in i.columns]) for i in cls.__table__.indexes]
    fks = [fk.name for fk in cls.__table__.foreign_key_constraints]
    col = cls.__table__.columns["user_id"]
    print(f"{cls.__name__}: columns={cols} nullable={col.nullable} indexes={ix} fks={fks}")
