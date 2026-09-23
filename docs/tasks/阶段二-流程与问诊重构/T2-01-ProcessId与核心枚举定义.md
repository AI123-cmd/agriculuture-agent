# T2-01 ProcessId 与核心枚举定义

## 这一项要做什么

在 `app/agent/state.py` 中建立全项目唯一的枚举来源，覆盖流程标识、问诊环节、槽位状态与来源、把握程度、闸门裁定、证据种类、任务类型等取值。技术方案 5.2 要求"枚举封闭"，即这些取值必须是 Python `Enum` 而不是自由字符串，大模型输出的自由文本必须解析落到枚举上，解析失败即视为契约违约。 把这些枚举集中在一处，是为了让契约层、智能体层、闸门层、知识库层、模型网关引用同一份定义。若各处各写一遍字符串常量，一次措辞变更就会在不同层之间产生不匹配，而这类不匹配在安全校验链路上是不可接受的。

## 前置依赖

- 上游任务：无（本批次第一项）；外部环境：Python 3.11+，pydantic 2.x，与 T1-01 确定的 `requirements.txt` 一致
- 现有代码现状：`backend/app/agent/state.py` 当前只定义 `Step`（六值）与 `AgentState`，本项用新枚举整体取代 `Step`

## 参考章节

- 技术方案 第5章 5.2 契约设计总原则（枚举封闭、事实必带溯源、区分未知与缺失）；技术方案 第5章 5.3.1 共享状态 ConsultationState（`ConsultStage` 的十一个取值）
- 技术方案 第6章 6.3 黑板式共享状态（来源优先级表）；技术方案 第8章 8.2 规则清单（`severity` 的 block 与 warn）；第9章 9.1 流程配置模型（`SlotSpec.group` 六个中文取值）
- 技术方案 第5章 5.3.6 门禁契约 GateResult（`decision` 三值）；5.3.7 报告守卫契约（`trigger_source` 四值）；技术方案 第12章 12.2 流程类型与档案模型调整（`object_type` 三值）；第4章 4.4 模型分层策略（任务类型与模型档位）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 修改 | `app/agent/state.py` | 删除 `Step`，新增全部核心枚举与 `SOURCE_PRIORITY` 表 |
| 修改 | `app/core/exceptions.py` | 新增 `EnumParseError`，供契约解析失败时抛出 |
| 修改 | `app/agent/__init__.py` | 导出枚举，供 `contracts/`、`gate/`、`process_config/` 引用 |
| 新增 | `tests/contracts/test_enums.py` | 枚举取值稳定性与解析失败的单元测试 |

## 实现要点

### 枚举基类选择

全部枚举继承 `str, Enum`。原因是枚举值会进入 YAML 配置、JSON 契约、JSONB 列与数据库字符串列，`str` 混入让 `ProcessId.RICE == "rice"` 成立，避免在序列化边界上反复做类型转换，也让 `pydantic` 的 `use_enum_values` 开关不会改变行为。

### 流程标识 ProcessId

```python
class ProcessId(str, Enum):
    RICE = "rice"            # 水稻
    TEA = "tea"              # 茶
    RAPESEED = "rapeseed"    # 油菜
    CHILI = "chili"          # 辣椒
    WEED = "weed"            # 杂草防治，跨作物的问题类型，不绑定地块
    PIG = "pig"              # 养猪
    GENERAL = "general"      # 通用农业兜底
```

七值固定，不接受动态注册。新增流程的改动量固定为"一份 YAML + 一个约 15 行的子图文件 + 路由表注册一行"，因此枚举本身不提供 `add` 之类的扩展口。

### 问诊环节 ConsultStage

```python
class ConsultStage(str, Enum):
    INTAKE = "intake"          # 受理，归属校验与会话装载
    TRIAGE = "triage"          # 判流程
    OBSERVE = "observe"        # 看现场
    INTERVIEW = "interview"    # 问五情
    VERIFY = "verify"          # 查依据
    JUDGE = "judge"            # 下判断
    PRESCRIBE = "prescribe"    # 开方
    GATE = "gate"              # 安全校验
    FOLLOWUP = "followup"      # 登记回访
    HANDOFF = "handoff"        # 转人工
    BLOCKED = "blocked"        # 安全拦截
```

`INTAKE` 至 `PRESCRIBE` 对齐产品说明书 4.2.2 的六步，`GATE`、`FOLLOWUP` 为安全与事件环节，`HANDOFF`、`BLOCKED` 为两条终态。`BLOCKED` 与 `HANDOFF` 必须分开：前者是闸门硬红线拒绝输出，后者是正常业务结果（超边界、法定报告、连续未收敛），二者在审计表与线上指标上要能分开统计。

### 槽位相关枚举

```python
class SlotStatus(str, Enum):
    FILLED = "filled"       # 已确认
    UNKNOWN = "unknown"     # 农户明确答不出，是合法答案
    MISSING = "missing"     # 尚未询问

class SlotSource(str, Enum):
    PROFILE = "profile"
    SENSOR = "sensor"
    WEATHER = "weather"
    USER_ANSWER = "user_answer"
    IMAGE = "image"
    INFERRED = "inferred"
# 数字越小优先级越高，高优先级可覆盖低优先级（技术方案 6.3）
SOURCE_PRIORITY: dict[SlotSource, int] = {
    SlotSource.PROFILE: 1,
    SlotSource.SENSOR: 2,
    SlotSource.WEATHER: 3,
    SlotSource.USER_ANSWER: 4,
    SlotSource.IMAGE: 5,
    SlotSource.INFERRED: 6,
}
```

`UNKNOWN` 与 `MISSING` 必须分开。`MISSING` 表示还没问过，追问候选池筛选器据此决定是否纳入候选；`UNKNOWN` 表示问过且农户答不出，筛选器必须剔除，否则会出现反复逼问同一个问题。档案优先级最高，`inferred` 永远无法覆盖 `profile`，这条由 T2-08 的写入函数按 `SOURCE_PRIORITY` 实现，不写进提示词。

### 判定与闸门相关枚举

```python
class ConfidenceLevel(str, Enum):
    LIKELY = "likely"              # 展示为"很可能是"
    PRELIMINARY = "preliminary"    # 展示为"初步判断"
    UNCERTAIN = "uncertain"        # 展示为"不确定"

class ViolationSeverity(str, Enum):
    BLOCK = "block"
    WARN = "warn"

class GateDecision(str, Enum):
    ALLOW = "allow"
    DEGRADE = "degrade"
    BLOCK = "block"

class TriggerSource(str, Enum):
    SIGNAL = "signal"
    GROUP = "group"
    MASS_DEATH = "mass_death"
    NONE = "none"

class PanelResolution(str, Enum):
    DECISIVE_FEATURE = "decisive_feature"
    ELIMINATION = "elimination"
    UNRESOLVED = "unresolved"
```

`ConfidenceLevel` 只允许三个值，对应三句农户能听懂的展示文案。数值型置信度分数仅用于内部路由，不得进入任何契约字段或展示文案（技术方案 5.3.4 硬约束）。

### 其余支撑枚举

```python
class SlotGroup(str, Enum):
    SEEDING = "种情"
    FERTILIZER = "肥情"
    SOIL_MOISTURE = "墒情"
    SEEDLING = "苗情"
    PEST = "病虫情"
    MANAGEMENT = "管理"

class SlotOwner(str, Enum):
    FIELD_OBSERVER = "field_observer"
    INTERVIEWER = "interviewer"
    PROFILE_LOADER = "profile_loader"

class EvidenceKind(str, Enum):
    PEST = "pest"
    DISEASE = "disease"
    PESTICIDE = "pesticide"
    CULTIVATION = "cultivation"
    THRESHOLD = "threshold"

class AnswerType(str, Enum):
    SINGLE = "single"
    MULTI = "multi"
    TEXT = "text"
    PHOTO = "photo"
    NUMBER = "number"

class ObjectType(str, Enum):
    CROP = "crop"
    LIVESTOCK = "livestock"
    ISSUE = "issue"

class TaskType(str, Enum):
    TRIAGE = "triage"
    OBSERVE = "observe"
    INTERVIEW = "interview"
    RETRIEVE = "retrieve"
    DIAGNOSE = "diagnose"
    PRESCRIBE = "prescribe"
    PANEL = "panel"
    SNAPSHOT_WRITER = "snapshot_writer"
    REPORT_GUARD = "report_guard"
```

`SlotGroup` 用中文枚举值，因为它是配置里给人看的分类；`SlotOwner` 用英文，因为它是代码标识，会出现在 `SlotValue` 写入方的判断中。`TaskType` 与模型网关的任务类型路由（T2-57）对应，每个值对应一个模型档位。

### 未知取值的解析入口

```python
def parse_stage(raw: str) -> ConsultStage:
    """把外部传入的环节字符串解析为枚举，失败即抛异常触发降级。"""
    try:
        return ConsultStage(raw)
    except ValueError as exc:
        raise EnumParseError(f"非法 consult_stage: {raw!r}") from exc

def parse_process_id(raw: str) -> ProcessId:
    """流程标识解析，未登记的流程名不得静默回落到 general。"""
    try:
        return ProcessId(raw)
    except ValueError as exc:
        raise EnumParseError(f"非法 process_id: {raw!r}") from exc
```

所有"自由文本落到枚举"的位置都走同构的解析函数，禁止用 `dict.get(..., default)` 的方式静默兜底。"降级到 general"是一个显式的路由决策，必须由分诊智能体给出 `fallback_reason` 后才能发生，不能在解析层顺手完成。

## 验收标准

- `python -c "from app.agent.state import ProcessId, ConsultStage; print(len(ProcessId), len(ConsultStage))"` 输出 `7 11`
- `pytest tests/contracts/test_enums.py` 全部通过，至少覆盖三条用例：`ProcessId("rice") is ProcessId.RICE` 为真、`parse_stage("unknown_stage")` 抛 `EnumParseError`、`parse_process_id("bees")` 抛 `EnumParseError`
- `python -c "from app.agent.state import SOURCE_PRIORITY, SlotSource; assert SOURCE_PRIORITY[SlotSource.PROFILE] < SOURCE_PRIORITY[SlotSource.INFERRED]"` 返回 0
- `grep -rn "Step\." backend/app/` 无结果，旧 `Step` 枚举的引用已全部清除
- `ruff check`、`ruff format --check`、`mypy app` 三条命令对改动文件返回 0
