# T2-39 用药史冲突规则 R-CONFLICT

## 这一项要做什么

在 `app/gate/rules/medication_conflict.py` 中实现 `R-CONFLICT`：处方推荐的药剂与档案记录的近期用药之间，不得存在重复用药或已知禁忌搭配。它与 `R-MAX-USE` 读同一份用药史但回答不同问题——次数超限是"量"，重复与禁忌是"质"。重复用药会加速抗药性形成（技术方案 9.2 指出抗药性暂停用药名单正是为此建模为 `status = suspended`），禁忌搭配则可能直接产生药害。等级 `block`。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`MedicationRecord`、`GateViolation`、`GateRuleId`。
- 依赖 T2-35：必须已提供 `KnowledgeFacts` 假实现，本项需要 `forbidden_combinations` 字段。
- 依赖 T2-11：必须已提供 `PesticideItem.forbidden_combinations` 的字段语义（来自知识条目）。
- 依赖 T2-21：处方智能体的 `_passes_history` 已做输入侧预过滤，本规则是在闸门侧的二次校验；两者都实现是刻意的，"检索层不放行比红线拦截更早生效，红线作为第二道防线"。
- 外部条件：禁忌搭配清单（`forbidden_combinations`）为农技内容，需人工确认（全局约定第八节）。
- 与 T2-37 的关系：两条规则共用 `medication_history` 的窗口口径（`season_start`），窗口定义必须一致，本项复用 T2-37 的 `_history_used` 同一份过滤逻辑（抽到 `app/gate/matching.py`）。

## 参考章节

- 技术方案 第8章 8.2 规则清单（`R-CONFLICT` 一行：与档案记录的近期用药不得存在重复用药或已知禁忌搭配）
- 技术方案 第8章 8.1 闸门在链路中的位置（档案用药史是闸门的三类输入之一）
- 技术方案 第9章 9.2 知识库分层（L3 条目的 `forbidden_combinations` 字段；抗药性暂停用药建模为 `status = suspended` 而非删除）
- 技术方案 第5章 5.3.5（`pesticide_items` 只能由代码填充，检索时依据 `process_id` + 判定结论 + 地区 + 档案用药史）
- 产品说明书 4.2.3 功能点 5（需要注意什么，含安全间隔期、禁忌搭配）；第5章各流程"不得给出超出…的方案"

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/medication_conflict.py` | `MedicationConflictRule` |
| 新增 | `app/gate/matching.py` | 用药史窗口过滤与药剂身份归一（T2-37 与 T2-39 共用） |
| 修改 | `app/gate/rules/dosage.py` | `_history_used` 改为调用 `app/gate/matching.py` |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_medication_conflict.py` | 重复用药与禁忌搭配的正反例 |
| 新增 | `tests/gate/test_matching.py` | 窗口过滤与归一化的单元测试 |

## 实现要点

### 共用匹配层

```python
# app/gate/matching.py
"""用药史匹配的共用逻辑。闸门内多条规则读同一份用药史，窗口口径必须一致。"""

def recent_records(history: list[MedicationRecord], season_start: date | None) -> list[MedicationRecord]:
    """按季节窗口过滤用药史。season_start 为 None 时不过滤（全部纳入）。"""
    if season_start is None:
        return list(history)
    return [r for r in history if r.used_on >= season_start]

def identity_key(name: str) -> str:
    """药剂身份归一：去空白、去剂型后缀括号、统一全半角。用于重复用药判定。"""
    normalized = unicodedata.normalize("NFKC", name).strip()
    normalized = re.sub(r"[（(].*?[)）]", "", normalized)
    return re.sub(r"\s+", "", normalized)

def conflicts_with(entry_combos: tuple[str, ...], recent: list[MedicationRecord]) -> list[str]:
    """返回与近期用药构成禁忌搭配的药剂名清单。"""
    used = {identity_key(r.pesticide_name) for r in recent if r.pesticide_name}
    return [c for c in entry_combos if identity_key(c) in used]
```

窗口过滤抽到共用模块，是因为 `R-MAX-USE` 与 `R-CONFLICT` 若各自实现一遍 `used_on >= season_start`，两处会漂移，出现"次数按本季算、冲突按全年算"这种难以察觉的不一致，而这类不一致在安全规则里直接等于漏判。`identity_key` 归一是必要推断：知识条目的 `forbidden_combinations` 与档案的 `pesticide_name` 来自不同录入方，同一药剂可能写作"吡虫啉"与"吡虫啉（可湿性粉剂）"，不归一会漏判禁忌。

### 规则实现

```python
@register
class MedicationConflictRule(Rule):
    """R-CONFLICT：不得与档案近期用药重复，也不得构成已知禁忌搭配。"""

    rule_id = GateRuleId.CONFLICT.value
    severity: GateSeverity = "block"
    description = "不得与近期用药重复或构成禁忌搭配"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        recent = recent_records(ctx.medication_history, ctx.season_start)
        if not recent:
            return violations
        used_keys = {identity_key(r.pesticide_name): r for r in recent if r.pesticide_name}

        for i, item in enumerate(ctx.prescription.pesticide_items):
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            key = identity_key(item.name)
            prev = used_keys.get(key)
            if prev is not None:
                violations.append(self.violation(
                    field=f"pesticide_items[{i}]",
                    message=(f"《{item.name}》与 {prev.used_on.isoformat()} 的用药重复，"
                             f"连续使用同一药剂会加速抗药性"),
                ))
            combos = entry.forbidden_combinations if entry is not None else ()
            hits = conflicts_with(combos, recent)
            if hits:
                violations.append(self.violation(
                    field=f"pesticide_items[{i}].forbidden_combinations",
                    message=(f"《{item.name}》与近期用过的 {'、'.join(hits)} "
                             f"属已知禁忌搭配"),
                ))
        return violations
```

### 三处判定口径

第一，**重复用药以归一后的药剂名为键，而不是 `knowledge_id`**。用药史记录的是农户实际打的药（可能来自其他渠道，未必有本系统的 `knowledge_id`），因此只能用名字比对。这正是"重复"与"次数超限"必须分开的原因：次数需要精确条目 id，重复只能用名字。

第二，**无近期用药时直接返回**。首次用药的农户不应因"用药史为空"产生任何违规。这条早退也避免了对空历史的无意义循环。

第三，**禁忌搭配只查"条目的禁忌清单是否被近期用药命中"这一个方向**。不查"近期用药的禁忌清单是否被本次药剂命中"，因为后者的数据往往缺失（历史记录可能没有对应的知识条目）。单向查询是"宁可少报也不误报"的取舍；但要注意这条与守卫类的 fail-closed 原则不矛盾——单向查询覆盖的是主要场景（新药的标签禁忌必然包含已用过的老药），漏报概率低，而双向查询在历史数据不全时会产生大量无法解释的命中，反而降低闸门可信度。

### 与输入侧预过滤的关系

T2-21 的 `_passes_history` 在生成侧已剔除重复用药与禁忌搭配的候选条目。本规则的存在有三条理由：模型仍可能从候选之外的条目里选择（契约允许 `pesticide_items` 为模型选择后的子集，但模型的"选择"必须仍在候选中，预过滤与闸门是两道独立防线）；用药史可能在问诊过程中更新（农户刚补录了一条用药记录）；预过滤逻辑若被改动，闸门仍能兜住。因此命中 `R-CONFLICT` 时首先要检查的是"预过滤是否失效"，而不是直接调提示词。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-CONFLICT'` 聚合，按 message 特征分两类。

- **"用药重复"**：拆分到按 `process_id`。该分支命中率长期偏高，说明检索侧的用药史过滤没有生效（T2-21 的 `_passes_history` 未被调用或参数错误），属回归信号，应作为 CI 告警项。
- **"已知禁忌搭配"**：该分支的每一次命中都应生成一条知识条目复核工单（8.5 第一条），因为禁忌清单的正确性依赖农技确认。命中数突增通常意味着某条知识条目的 `forbidden_combinations` 被批量修改。
- 交叉观察：`R-CONFLICT` 命中而 `R-MAX-USE` 未命中，说明农户确实是首次使用但药剂与近期用药冲突，属真实有效拦截；两条同时命中，说明检索侧两道防线都失效，优先级最高。

## 验收标准

- `pytest tests/gate/rules/test_medication_conflict.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：用药史为空，返回空列表。
  - 正例 2：用药史有记录但全部早于 `season_start`，返回空列表。
  - 反例 1：用药史含与本次同名药剂（同季内），返回 1 条 block 违规，message 含"用药重复"与对应日期。
  - 反例 2：本次药剂条目的 `forbidden_combinations=("代森锰锌",)`，本季用药史含"代森锰锌（可湿性粉剂）"，返回 1 条 block 违规，message 含"已知禁忌搭配"（验证 `identity_key` 归一后仍能命中）。
- `pytest tests/gate/test_matching.py` 全部通过，含：`recent_records` 在 `season_start=None` 时返回全量；`identity_key(" 吡虫啉（可湿性粉剂） ")` 与 `identity_key("吡虫啉")` 相等；全角括号同样被去除。
- 断言 T2-37 的 `MaxUsePerSeasonRule._history_used` 调用 `app/gate/matching.py` 的同一函数，两处窗口口径一致（构造一条"恰在 `season_start` 当天"的记录，断言两条规则都纳入）。
- 断言 `rule_id == "R-CONFLICT"`、`severity == "block"`，`build_rules(["R-CONFLICT"])` 可实例化。
- 该模块及 `matching.py` 内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。
