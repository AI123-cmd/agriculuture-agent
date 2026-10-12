"""内置厂商模板的幂等导入（T5-02）。

模板随代码发布，不靠人工插库。启动时从 `app/domain/vendor_templates.json` 导入，以固定 `id`
为键做 upsert，**只增改不删除**：运营会在库里补充新采购型号的模板，启动清理会把这些手工数据
抹掉。因此 JSON 是"内置基线"，数据库是"基线加运营增量"。

幂等是必需的：容器滚动重启会多次执行，非幂等的插入会让第二次启动直接撞唯一约束失败。
"""

import json
from importlib.resources import files

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VendorTemplate

# 模板数据集所在包。用 `importlib.resources` 而非 `Path(__file__)`：打包安装后包内资源仍可读。
TEMPLATES_PACKAGE = "app.domain"
TEMPLATES_FILENAME = "vendor_templates.json"

# 内置模板的 id 前缀，便于与运营手工新增的模板区分。
BUILTIN_ID_PREFIX = "tpl_"


def load_builtin_templates() -> list[dict]:
    """读取包内模板数据集，返回条目列表。"""
    raw = files(TEMPLATES_PACKAGE).joinpath(TEMPLATES_FILENAME).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"{TEMPLATES_FILENAME} 顶层必须是数组")
    return data


async def seed_vendor_templates(db: AsyncSession) -> int:
    """幂等导入内置模板：按 id upsert，不删除运营手工新增的模板。

    返回处理的条目数。不提交事务，由调用方决定（与 `ConfigSyncService` 的口径一致：部分失败
    可整体回滚，不留半同步的库）。
    """
    items = load_builtin_templates()
    for item in items:
        existing = await db.get(VendorTemplate, item["id"])
        if existing is None:
            db.add(VendorTemplate(**item))
        else:
            for key, value in item.items():
                setattr(existing, key, value)
    return len(items)


__all__ = ["BUILTIN_ID_PREFIX", "load_builtin_templates", "seed_vendor_templates"]
