"""知识库索引构建任务入口（T2-48）。

把"选哪个向量化实现、读哪个目录、是否 dry-run"这些运行期决策收在一处，供启动引导、
定时任务与 CI 复用；索引逻辑本身在 `app/knowledge/indexer.py`。CI 与预发用
`build(dry_run=True)` 在校验条目文件的同时不写库。

向量化实现的选择与 `USE_MOCK_LLM` 绑定：mock 模式下用确定性伪向量，不发起任何真实
请求（测试用 `respx` 断言零请求）。这样内容建设阶段不需要向量化服务也能跑通全链路，
只是不验证真实语义相似度。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.knowledge.embedding import Embedder, HttpEmbedder, MockEmbedder
from app.knowledge.indexer import BuildReport, KnowledgeIndexer, load_seed_dir

logger = logging.getLogger(__name__)


def build_embedder() -> Embedder:
    """按配置选择向量化实现。`USE_MOCK_LLM=true` 时用 mock，避免测试误连真实服务。"""
    settings = get_settings()
    if settings.USE_MOCK_LLM:
        return MockEmbedder()
    return HttpEmbedder(url=settings.EMBEDDING_API_URL, api_key=settings.EMBEDDING_API_KEY)


async def build_knowledge_index(session: AsyncSession, *, dry_run: bool = False) -> BuildReport:
    """读取包内条目数据文件并构建索引。

    **不提交事务**：是否提交由调用方决定（与 `ConfigSyncService` 同一约定），这样
    「部分条目构建成功」可以被整体回滚，不留半构建的库。
    """
    items = load_seed_dir()
    indexer = KnowledgeIndexer(session, build_embedder())
    report = await indexer.build(items, dry_run=dry_run)
    logger.info(
        "knowledge index built",
        extra={
            "upserted": report.upserted,
            "skipped": report.skipped,
            "region_rows": report.region_rows,
            "errors": len(report.errors),
            "dry_run": dry_run,
        },
    )
    return report


__all__ = ["build_embedder", "build_knowledge_index"]
