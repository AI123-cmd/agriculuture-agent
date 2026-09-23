"""知识条目模型（`knowledge` schema）。

本项只建向量列，保证列类型存在且比较算子可用；HNSW / IVFFlat 索引在 T2-48
按真实数据量调参后再建 —— 现在建只会得到一个可能不合适的参数。
"""

from pgvector.sqlalchemy import Vector
from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class KnowledgeItem(Base, TimestampMixin):
    """农技知识条目，`embedding` 维度必须与 T2-56 选定的 embedding 模型一致。

    当前按 1024 建列（与 `Settings.EMBEDDING_DIM` 一致）。换模型时需要
    `ALTER TABLE ... ALTER COLUMN embedding TYPE vector(<新维度>)`，
    且已有数据必须重新编码，否则维度不匹配会报错。
    """

    __tablename__ = "knowledge_item"
    __table_args__ = {"schema": "knowledge"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    region_code: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    facts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
