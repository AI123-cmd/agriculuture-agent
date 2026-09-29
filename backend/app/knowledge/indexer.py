"""知识库索引构建器（T2-48，技术方案 9.2 / 9.5）。

从条目数据文件读入原始条目，做字段校验、正文向量化，写入 `knowledge_item` 与
`knowledge_region` 两张表。它是 T2-52 / T2-53 内容建设的落库入口，也是闸门规则的
比对数据来源。

三种运行方式共用一个 `build`：全量重建（首次部署）、增量更新（内容迭代）、
dry-run（CI 与预发校验）。增量靠 `content_hash` 指纹比对决定是否重算向量——只改
一个字就把全部条目重新向量化是不可接受的成本。

两条不变量：

- **先算向量再落库**：向量化失败则整条不写入，不留下 `embedding` 为空的条目。
  空向量条目在检索时会静默失效（距离算子对 NULL 返回 NULL，排序里被挤到最后），
  这是最难排查的一类问题。
- **主表与地区表同事务**：避免"条目写了但地区挂载没写"导致检索漏召回。条目未声明
  任何地区时自动挂到全国兜底（`000000`），保证回退链最后一级一定召回得到。
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.embedding import EMBEDDING_DIM, Embedder
from app.knowledge.schema import NATIONAL_CODE, NATIONAL_LEVEL, KnowledgeRegionSpec, KnowledgeSeedItem
from app.models.knowledge import KnowledgeItem, KnowledgeRegion
from app.process_config.loader import load_all_configs
from app.repositories.knowledge import KnowledgeRepository

logger = logging.getLogger(__name__)

SEEDS_PACKAGE = "app.knowledge.seeds"


class IndexBuildError(Exception):
    """单条条目构建失败（向量维度不符等）。收集进报告而非直接中断整轮。"""


@dataclass(slots=True)
class BuildReport:
    """构建报告。dry-run 时只填 `upserted`（本应写入数）与 `skipped`。"""

    upserted: int = 0
    skipped: int = 0
    region_rows: int = 0
    errors: list[str] = field(default_factory=list)


def _to_decimal(value: float | None) -> Decimal | None:
    """float → Decimal。经 `str` 中转避免二进制浮点误差（如 0.1 落成 0.100000000000000006）。"""
    return None if value is None else Decimal(str(value))


class KnowledgeIndexer:
    """知识索引构建。支持全量重建、增量更新与 dry-run。"""

    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._repo = KnowledgeRepository(session)
        self._embedder = embedder

    async def build(
        self,
        items: Iterable[KnowledgeSeedItem],
        *,
        dry_run: bool = False,
        check_process: bool = True,
    ) -> BuildReport:
        """构建全部条目，返回逐项汇总的报告。

        `errors` 收集单条失败而不抛出：一次构建要能报出全部问题条目，而不是修一个
        跑一次。仅 `_upsert_one` 这类被直接调用的方法才抛异常（便于单测断言）。
        """
        report = BuildReport()
        known_processes = self._known_processes() if check_process else None

        for item in items:
            if known_processes is not None and item.process_id.value not in known_processes:
                # 防止"知识库有条目但流程没建"的悬空状态：检索侧永远召不到它。
                report.errors.append(f"{item.knowledge_id}: process_id={item.process_id.value} 无对应流程配置")
                continue

            existing = await self._repo.get_by_id(item.knowledge_id)
            if existing is not None and existing.content_hash == item.content_fingerprint():
                report.skipped += 1
                continue

            report.upserted += 1
            if dry_run:
                continue

            try:
                region_rows = await self._upsert_one(item, existing)
            except IndexBuildError as exc:
                report.upserted -= 1
                report.errors.append(f"{item.knowledge_id}: {exc}")
                continue
            report.region_rows += region_rows

        return report

    async def _upsert_one(self, item: KnowledgeSeedItem, existing: KnowledgeItem | None) -> int:
        """写入一条：先算向量，再写主表，再同步地区表。同一事务内完成。

        返回写入的地区挂载行数。向量维度不符时抛 `IndexBuildError`，调用方决定是
        整轮中断还是收集到报告。
        """
        vector = (await self._embedder.embed([item.embedding_text()]))[0]
        # 以表列固定维度为准比对，而非引用方自报的 `embedder.dimensions`：自报值可能
        # 与 `Vector(1024)` 不符，那时落库会直接报错，不如在这里给出带条目 id 的明确错误。
        if len(vector) != EMBEDDING_DIM:
            raise IndexBuildError(f"向量维度不符：期望 {EMBEDDING_DIM}，实得 {len(vector)}")

        if existing is None:
            self._session.add(self._to_row(item, vector))
        else:
            await self._session.execute(
                update(KnowledgeItem)
                .where(KnowledgeItem.knowledge_id == item.knowledge_id)
                .values(**self._to_update_values(item, vector))
            )
        return await self._sync_regions(item)

    async def _sync_regions(self, item: KnowledgeSeedItem) -> int:
        """同步地区挂载：先删旧挂载，再按条目声明写入。返回写入行数。

        未声明任何地区时自动挂到全国兜底：保证每条条目在回退链最后一级一定可被召回。
        """
        await self._session.execute(delete(KnowledgeRegion).where(KnowledgeRegion.knowledge_id == item.knowledge_id))
        specs = list(item.regions)
        if not specs and item.region_scope is None:
            specs = [KnowledgeRegionSpec(region_code=NATIONAL_CODE, region_level=NATIONAL_LEVEL)]
        for spec in specs:
            self._session.add(
                KnowledgeRegion(
                    id=str(uuid4()),
                    knowledge_id=item.knowledge_id,
                    region_code=spec.region_code,
                    region_level=spec.region_level,
                )
            )
        return len(specs)

    def _to_row(self, item: KnowledgeSeedItem, vector: list[float]) -> KnowledgeItem:
        """构造主表行。`status` 原样落库，构建器不改变既有条目的状态。"""
        return KnowledgeItem(
            knowledge_id=item.knowledge_id,
            process_id=item.process_id.value,
            kind=item.kind.value,
            title=item.title,
            content=item.content,
            pesticide_name=item.pesticide_name,
            registered_crops=list(item.registered_crops),
            dosage_min=_to_decimal(item.dosage_min),
            dosage_max=_to_decimal(item.dosage_max),
            dosage_unit=item.dosage_unit,
            safety_interval_days=item.safety_interval_days,
            max_uses_per_season=item.max_uses_per_season,
            forbidden_combinations=list(item.forbidden_combinations),
            region_scope=item.region_scope,
            effective_date=item.effective_date,
            expire_date=item.expire_date,
            status=item.status.value,
            status_reason=item.status_reason,
            source_ref=item.source_ref,
            content_hash=item.content_fingerprint(),
            embedding=vector,
        )

    def _to_update_values(self, item: KnowledgeSeedItem, vector: list[float]) -> dict:
        """增量更新的列集合。与 `_to_row` 覆盖同一批字段，保持两条路径一致。"""
        return {
            "process_id": item.process_id.value,
            "kind": item.kind.value,
            "title": item.title,
            "content": item.content,
            "pesticide_name": item.pesticide_name,
            "registered_crops": list(item.registered_crops),
            "dosage_min": _to_decimal(item.dosage_min),
            "dosage_max": _to_decimal(item.dosage_max),
            "dosage_unit": item.dosage_unit,
            "safety_interval_days": item.safety_interval_days,
            "max_uses_per_season": item.max_uses_per_season,
            "forbidden_combinations": list(item.forbidden_combinations),
            "region_scope": item.region_scope,
            "effective_date": item.effective_date,
            "expire_date": item.expire_date,
            "status": item.status.value,
            "status_reason": item.status_reason,
            "source_ref": item.source_ref,
            "content_hash": item.content_fingerprint(),
            "embedding": vector,
        }

    @staticmethod
    def _known_processes() -> set[str]:
        """已成功加载配置的流程集合，用于拦截"条目流程不存在"的悬空状态。"""
        return {pid.value for pid in load_all_configs()}


def load_seed_dir(directory: Path | None = None) -> list[KnowledgeSeedItem]:
    """读取条目数据目录下的全部 `*.jsonl`，返回条目列表。

    每行一个 JSON 对象。文件与行号进异常消息，便于定位写坏的条目。`directory` 省略时
    读包内 `app/knowledge/seeds`（打包安装后仍可用）。
    """
    items: list[KnowledgeSeedItem] = []
    if directory is None:
        base = files(SEEDS_PACKAGE)
        paths = sorted((p for p in base.iterdir() if p.name.endswith(".jsonl")), key=lambda p: p.name)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            items.extend(_parse_jsonl(text, path.name))
        return items

    for path in sorted(directory.glob("*.jsonl")):
        items.extend(_parse_jsonl(path.read_text(encoding="utf-8"), path.name))
    return items


def _parse_jsonl(text: str, filename: str) -> list[KnowledgeSeedItem]:
    """逐行解析 jsonl，跳过空行。解析或校验失败时带上文件名与行号。"""
    out: list[KnowledgeSeedItem] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{filename}:{lineno} 不是合法 JSON：{exc}") from exc
        try:
            out.append(KnowledgeSeedItem.model_validate(payload))
        except Exception as exc:  # noqa: BLE001  pydantic 校验错误统一转成带定位的消息
            raise ValueError(f"{filename}:{lineno} 条目校验失败：{exc}") from exc
    return out


__all__ = [
    "SEEDS_PACKAGE",
    "BuildReport",
    "IndexBuildError",
    "KnowledgeIndexer",
    "load_seed_dir",
]
