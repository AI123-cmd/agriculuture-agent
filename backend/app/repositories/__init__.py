"""数据访问层入口：暴露各仓储单例。

仓储是纯逻辑对象（无状态、无配置），故用模块级单例而非 `lru_cache` 工厂。
调用方只从这里取单例，不自行实例化，避免出现第二份实现。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.base import OwnedRepository
from app.repositories.consultation import ConsultationRepository, consult_repo
from app.repositories.conversation import (
    ConversationRepository,
    CropRepository,
    conversation_repo,
    crop_repo,
)
from app.repositories.device import (
    DeviceConfigRepository,
    DeviceRepository,
    FieldMappingRepository,
    device_config_repo,
    device_repo,
    field_mapping_repo,
)
from app.repositories.env import EnvReadingRepository, env_reading_repo
from app.repositories.field import FieldRepository, field_repo
from app.repositories.followup import FollowupRepository, followup_repo
from app.repositories.ledger import LedgerRepository, ledger_repo
from app.repositories.location import LocationRepository, location_repo
from app.repositories.memory import MemorySummaryRepository, memory_repo
from app.repositories.message import ConversationScopedRepository, message_repo
from app.repositories.prescription import PrescriptionRepository, prescription_repo
from app.repositories.profile import FarmerProfileRepository, profile_repo
from app.repositories.quota import QuotaRepository, quota_repo
from app.repositories.reach import ReachRepository, reach_repo
from app.repositories.snapshot import SnapshotRepository, snapshot_repo
from app.repositories.tag import ProfileTagRepository, tag_repo
from app.repositories.user import UserRepository, user_repo
from app.repositories.vendor_template import VendorTemplateRepository, vendor_template_repo


@dataclass(frozen=True)
class Repositories:
    """一次调用的数据访问入口：把会话与仓储绑成一个对象传递。

    `app/tools/` 与 `app/broadcast/` 原本接收 `AsyncSession | None`，ORM 类型因此
    渗进了 `app/agent/nodes/`（L2 的隐患）。改为接收本对象后，工具层签名里不再
    出现 `AsyncSession`，"无库上下文"（离线兜底）仍用 `None` 表达，语义不变。
    """

    db: AsyncSession
    profile: FarmerProfileRepository = profile_repo
    crop: CropRepository = crop_repo
    tag: ProfileTagRepository = tag_repo
    device: DeviceRepository = device_repo
    device_config: DeviceConfigRepository = device_config_repo
    field_mapping: FieldMappingRepository = field_mapping_repo
    env_reading: EnvReadingRepository = env_reading_repo
    location: LocationRepository = location_repo
    field: FieldRepository = field_repo
    memory: MemorySummaryRepository = memory_repo
    conversation: ConversationRepository = conversation_repo
    message: ConversationScopedRepository = message_repo
    consult: ConsultationRepository = consult_repo
    prescription: PrescriptionRepository = prescription_repo
    followup: FollowupRepository = followup_repo
    snapshot: SnapshotRepository = snapshot_repo
    ledger: LedgerRepository = ledger_repo
    quota: QuotaRepository = quota_repo
    reach: ReachRepository = reach_repo
    user: UserRepository = user_repo
    vendor_template: VendorTemplateRepository = vendor_template_repo


__all__ = [
    "OwnedRepository",
    "ConversationRepository",
    "ConversationScopedRepository",
    "ConsultationRepository",
    "CropRepository",
    "FarmerProfileRepository",
    "DeviceRepository",
    "DeviceConfigRepository",
    "FieldMappingRepository",
    "EnvReadingRepository",
    "FieldRepository",
    "FollowupRepository",
    "LedgerRepository",
    "LocationRepository",
    "MemorySummaryRepository",
    "PrescriptionRepository",
    "ProfileTagRepository",
    "SnapshotRepository",
    "QuotaRepository",
    "ReachRepository",
    "UserRepository",
    "VendorTemplateRepository",
    "Repositories",
    "conversation_repo",
    "consult_repo",
    "crop_repo",
    "followup_repo",
    "ledger_repo",
    "message_repo",
    "prescription_repo",
    "profile_repo",
    "device_repo",
    "device_config_repo",
    "field_mapping_repo",
    "env_reading_repo",
    "field_repo",
    "location_repo",
    "memory_repo",
    "snapshot_repo",
    "quota_repo",
    "reach_repo",
    "tag_repo",
    "user_repo",
    "vendor_template_repo",
]
