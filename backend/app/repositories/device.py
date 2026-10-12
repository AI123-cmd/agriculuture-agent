"""设备仓储：设备台账、设备接入配置与字段映射。"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceConfig, FieldMapping
from app.repositories.base import OwnedRepository, require_user_id


class DeviceRepository(OwnedRepository[Device]):
    def __init__(self) -> None:
        super().__init__(Device)

    async def list_for_user(self, db: AsyncSession, user_id: str) -> Sequence[Device]:
        """本人设备列表（环境看板、播报快照都用它）。"""
        return await self.list_all(db, user_id)


class DeviceConfigRepository(OwnedRepository[DeviceConfig]):
    """设备接入配置仓储。

    常规查询全部由 `OwnedRepository` 强制带 `user_id`。唯一的例外是 `get_by_sn` /
    `get_by_sn_any`——设备上报入口（T5-06）拿到 `device_sn` 时还不知道归属，只能按 SN 定位
    后在校验顺序第二、三步补做归属校验（产品说明书 4.5.2 功能点 4 把"未校验归属的访问路径"
    列为必须修复的安全问题）。两个方法的 docstring 均写明这一例外，避免被当成漏写归属条件。
    """

    def __init__(self) -> None:
        super().__init__(DeviceConfig)

    async def get_by_sn(self, db: AsyncSession, device_sn: str) -> DeviceConfig | None:
        """设备上报专用：按 SN 定位**在绑**设备。

        刻意不带 `user_id`：调用方此时还不知道归属，归属校验必须在取到设备后再做一次
        （T5-06 校验顺序第二步与第三步）。过滤 `unbound_at IS NULL` 使已解绑设备不可上报。
        """
        stmt = select(DeviceConfig).where(
            DeviceConfig.device_sn == device_sn,
            DeviceConfig.unbound_at.is_(None),
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def get_by_sn_any(self, db: AsyncSession, device_sn: str) -> DeviceConfig | None:
        """绑定流程专用：按 SN 定位设备，**不过滤** `unbound_at`。

        刻意不带 `user_id`，理由同 `get_by_sn`。绑定（T5-03）需要识别"曾解绑的行"以便复用
        （rebind）而不是插入一条会撞唯一约束的新行，因此不能过滤 `unbound_at`。
        """
        stmt = select(DeviceConfig).where(DeviceConfig.device_sn == device_sn)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def touch_last_report(
        self, db: AsyncSession, user_id: str, device_id: str, *, reported_at: datetime, online: str
    ) -> int:
        """设备上报成功后刷新在线状态与最近上报时刻（T5-06）。"""
        return await self.update_fields(db, user_id, device_id, last_report_at=reported_at, online_status=online)


class FieldMappingRepository(OwnedRepository[FieldMapping]):
    """字段映射仓储。

    归属以本表 `user_id` 为唯一准据：写入时由 `device_config.user_id` 复制，读取一律带该
    条件，即使 `device_id` 被猜测，跨农户读取也会返回空。
    """

    def __init__(self) -> None:
        super().__init__(FieldMapping)

    async def list_for_device(self, db: AsyncSession, user_id: str, device_id: str) -> Sequence[FieldMapping]:
        """取某台设备的全部映射（上报解析 T5-06 一次取全量）。"""
        return await self.list_all(db, user_id, device_id=device_id)

    async def replace_all(
        self, db: AsyncSession, user_id: str, device_id: str, mappings: Sequence[FieldMapping]
    ) -> int:
        """整体替换一台设备的映射：先按归属删除旧行，再插入新行。

        采取整体替换（而非逐条 upsert）的理由：映射保存（T5-05）提交的是"这台设备完整的映射
        视图"，整体替换能顺带清掉前端已删除的映射。删除带 `user_id` 条件，插入前归一到
        `user_id`；两步由调用方的单事务保证原子（T5-05 在 `unit_of_work` 内调用）。
        """
        uid = require_user_id(self.model.__name__, user_id)
        for mapping in mappings:
            mapping.device_id = device_id
            mapping.user_id = uid
        deleted = await self.delete_all(db, uid, device_id=device_id)
        for mapping in mappings:
            db.add(mapping)
        return deleted


device_repo = DeviceRepository()
device_config_repo = DeviceConfigRepository()
field_mapping_repo = FieldMappingRepository()

__all__ = [
    "DeviceConfigRepository",
    "DeviceRepository",
    "FieldMappingRepository",
    "device_config_repo",
    "device_repo",
    "field_mapping_repo",
]
