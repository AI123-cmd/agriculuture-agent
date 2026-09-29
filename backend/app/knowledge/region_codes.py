"""行政区编码解析：上级编码推导与级别判定（T2-50）。

行政区划编码是**层级前缀编码**：省级两位、市级四位、县级六位，上级编码是下级的前缀再
右补零到六位。因此给定 `330102`（某区）能直接推出 `330100`（杭州市）与 `330000`（浙江省），
无须为每个查询查一次字典。前缀推导让回退链的构造成为纯函数、可离线测试；字典只用于把编码
翻译成展示名（"杭州市"），不参与回退链构造。

**编码一律规范到六位**：`region_clause` 用等值比较命中 `region_scope` 列，而该列存的是六位
规范码（T2-48 挂载、T2-49 过滤都以六位为准）。若把市级推成 `3301` 这样的四位前缀，等值比较
永远命不中 `330100`，回退链在市级就会静默落空。故 `parent_code` 取前缀后右补零到六位。

字典以包内资源方式读取（`importlib.resources`），保证打包安装后仍可加载。
"""

import json
from functools import lru_cache
from importlib.resources import files

from app.knowledge.schema import NATIONAL_CODE, NATIONAL_LEVEL

# 规范编码长度。回退链里出现的每个编码都补零到这个长度。
CANONICAL_LENGTH = 6
# 三级行政编码的原始长度：县六位、市四位、省两位。用它判断某个输入是否足以推出一级。
LEVEL_LENGTH: dict[str, int] = {"county": 6, "city": 4, "province": 2}
# 回退链里从具体到泛化的级别顺序。
LEVELS: tuple[str, str, str] = ("county", "city", "province")

REGIONS_PACKAGE = "app.process_config.seeds"
REGIONS_FILENAME = "regions.json"


def parent_code(code: str, level: str) -> str:
    """取某一级的规范编码：截取该级前缀后右补零到六位。

    `parent_code("330102", "city")` → `"330100"`；`parent_code("330102", "province")` → `"330000"`。
    """
    return code[: LEVEL_LENGTH[level]].ljust(CANONICAL_LENGTH, "0")


def build_chain(region_code: str | None) -> list[tuple[str, str]]:
    """返回 (行政级别, 编码) 的有序回退链，从县级到全国。

    无编码时只有全国一级；编码过短时只推出它够得着的级别（如两位码只到省级）。最后一级
    永远是全国兜底，保证任何查询都有兜底路径。
    """
    if not region_code:
        return [(NATIONAL_LEVEL, NATIONAL_CODE)]
    chain: list[tuple[str, str]] = []
    for level in LEVELS:
        if len(region_code) >= LEVEL_LENGTH[level]:
            chain.append((level, parent_code(region_code, level)))
    chain.append((NATIONAL_LEVEL, NATIONAL_CODE))
    return dedupe_chain(chain)


def dedupe_chain(chain: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按**编码**去重，保留每个编码所对应的**最泛化级别**（出现最靠后的那条）。

    编码本身携带级别信息：末四位为 0 是省级（`330000`）、末两位为 0 是市级（`330100`）、
    其余是县级（`330102`）。当输入只填到市级（`330100`）时，县级前缀推导出的编码同样是
    `330100`——它并不是"一个叫 330100 的县"，而是杭州市本身。保留最泛化级别（市级）得到的
    才是正确标注；若保留最先出现的县级，界面会把杭州市标成"县"，且 `display_note` 会因
    判定为县级命中而误判为"无需标注"。`330000` 同理归属省级。

    保序：先在反向上收集每个编码首次（即正向最后）出现的级别，再翻回正向。
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for level, code in reversed(chain):
        if code not in seen:
            seen.add(code)
            out.append((level, code))
    out.reverse()
    return out


@lru_cache(maxsize=1)
def load_region_names() -> dict[str, str]:
    """从 `regions.json` 读取编码到展示名的映射。结果缓存，字典在进程内不变。"""
    path = files(REGIONS_PACKAGE).joinpath(REGIONS_FILENAME)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {code: str(entry["name"]) for code, entry in raw.items()}


def region_name(code: str) -> str:
    """把编码翻译成展示名。字典里没有的编码回退为编码本身，界面仍可读。"""
    return load_region_names().get(code, code)


__all__ = [
    "CANONICAL_LENGTH",
    "LEVELS",
    "LEVEL_LENGTH",
    "REGIONS_FILENAME",
    "REGIONS_PACKAGE",
    "build_chain",
    "dedupe_chain",
    "load_region_names",
    "parent_code",
    "region_name",
]
