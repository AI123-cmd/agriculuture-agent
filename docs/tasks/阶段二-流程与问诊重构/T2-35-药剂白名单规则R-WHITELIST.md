# T2-35 药剂白名单规则 R-WHITELIST

## 这一项要做什么

在 `app/gate/rules/pesticide_whitelist.py` 中实现 `R-WHITELIST`：处方里 `pesticide_items` 的每一项，其 `knowledge_id` 必须存在于已核准知识库且 `status = approved`。它是"模板出事实、模型只改写"的第一道自动检查，也是真实事故教训（农户按 AI 生成的除草剂方案导致上百亩作物绝收）在代码里的落点：模型不能凭空写出一个知识库里没有的药剂。等级 `block`。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register` 注册表、`GateContext`、`KnowledgeFacts` 协议、`GateViolation`、`GateRuleId`。
- 依赖 T2-11：必须已提供 `Prescription.pesticide_items` 与 `PesticideItem.knowledge_id` 的字段语义；闸门侧按 `GatePesticideItem` 同名镜像读取。
- 依赖 T2-47 与 T2-48：`KnowledgeFacts.get_entry` / `is_approved` 的实际实现由知识库层提供；本项只依赖协议，单测用假实现。`status` 的四值语义（`approved` 才可用，`suspended` 与 `deprecated` 排除）取自 T2-47。
- 外部条件：已核准药剂条目内容为农技内容，需人工确认（全局约定第八节）。
- 与 T2-21 的关系：处方智能体在填充时已保证逐字段照抄，本规则是二次校验；与 T2-42 的关系：`R-REDLINE` 检查禁用品种，本规则检查条目是否已核准，两者不重叠。

## 参考章节

- 技术方案 第8章 8.2 规则清单（`R-WHITELIST` 一行：`pesticide_items` 每一项的 `knowledge_id` 必须存在于已核准知识库且 `status = approved`）
- 技术方案 第8章 8.1 闸门在链路中的位置（知识库是闸门的三类输入之一）；第8章 8.3 规则实现示例（`check` 返回 `list[GateViolation]` 的写法）
- 技术方案 第5章 5.3.5 `pesticide_items` 的来源约束（只能由代码从知识库检索结果填充）；第9章 9.2 知识库分层（L3 处置知识必须经核准流程，`status` 四值）
- 产品说明书 4.2.3 功能点 5（涉及农药的处方必须来自本地植保知识库中已核准的条目）；第4章 4.2.4 接口关系表（植保知识库缺失时禁止生成处方）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/pesticide_whitelist.py` | `PesticideWhitelistRule` |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_pesticide_whitelist.py` | 正例与反例；含 suspended 与 deprecated 两态 |
| 新增 | `tests/gate/fixtures/fake_knowledge.py` | `KnowledgeFacts` 的假实现，供全部规则测试共用 |

## 实现要点

### 规则实现

```python
# app/gate/rules/pesticide_whitelist.py
from app.gate.enums import GateRuleId, GateSeverity
from app.gate.rules.base import Rule, register
from app.gate.contracts import GateContext, GateViolation

@register
class PesticideWhitelistRule(Rule):
    """R-WHITELIST：药剂条目必须来自已核准知识库。

    为什么这条排在最前：知识库是"事实"的唯一合法来源。一旦允许模型
    生成不在库中的药剂，后续所有剂量、间隔期、登记作物校验都失去比对基准。
    """

    rule_id = GateRuleId.WHITELIST.value
    severity: GateSeverity = "block"
    description = "药剂条目必须存在于已核准知识库"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        for item in ctx.prescription.pesticide_items:
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            if entry is None:
                violations.append(self.violation(
                    field=f"pesticide_items[{item.knowledge_id}]",
                    message=f"药剂《{item.name}》不在已核准知识库中（条目 {item.knowledge_id} 不存在）",
                ))
                continue
            if not ctx.knowledge.is_approved(item.knowledge_id):
                violations.append(self.violation(
                    field=f"pesticide_items[{item.knowledge_id}]",
                    message=(
                        f"药剂《{item.name}》引用的条目 {item.knowledge_id} 当前状态为 "
                        f"{entry.status}，不可用于处方"
                    ),
                ))
                continue
            if entry.name != item.name:
                violations.append(self.violation(
                    field=f"pesticide_items[{item.knowledge_id}].name",
                    message=(
                        f"药剂名与知识库不一致：处方为《{item.name}》，"
                        f"条目 {item.knowledge_id} 为《{entry.name}》"
                    ),
                ))
        return violations
```

### 三处判定的取舍

第一，**`get_entry` 为 `None` 与 `status != approved` 分开报**。前者意味着模型写了一个不存在的 `knowledge_id`，属于"模型越界"，处置是调提示词；后者意味着条目被暂停使用或已废弃，属于"知识库状态变更"，处置是查知识条目。8.5 第一条复盘用途要求能区分这两类，若合并成一条消息就无法分类。

第二，**名不一致也判 block**。技术方案 5.3.5 要求 `pesticide_items` 由代码填充，理论上名不会不一致；一旦不一致，说明模型改写了药剂名（例如把"吡虫啉"改成"吡虫灵"），这正是"模型改写事实"的最危险形态。宁可拦截也不放行。

第三，**空列表直接放行**。`pesticide_items = []` 是合法状态（技术方案 5.3.5：候选为空时 `pesticide_items = []` 且 `requires_tech_station = True`），本规则不产生违规。是否允许无药剂处方由别的规则与流程配置决定，本规则只管"有药剂时必须可溯源"。

### 与其他规则的边界

| 规则 | 检查什么 | 本规则不检查什么 |
| --- | --- | --- |
| `R-WHITELIST`（本项） | 条目是否存在、是否 approved、名是否一致 | 不检查剂量、间隔期、登记作物 |
| `R-REGISTERED-CROP`（T2-36） | 条目的登记作物是否含当前作物 | 不检查条目是否存在 |
| `R-REDLINE`（T2-42） | 文本是否出现禁用品种名或禁用短语 | 不检查条目状态 |

三条规则都读同一份 `pesticide_items` 与 `KnowledgeFacts`，但各自只回答一个问题是刻意的：单条规则失败时的提示语能直接指向一个具体原因，而不是给出一团混合信息。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-WHITELIST'` 聚合计数，分两个维度看。

- 按 `process_id` 分组的命中数：某条流程持续偏高，说明该流程的检索候选集有漏，模型在"没有候选"时倾向于编造条目。
- 按 `violations[].message` 是否含 `不在已核准知识库中` 分组：这条分支的占比单独统计，它对应"模型越界"，是需要调提示词的信号；占比低而 `状态为 suspended` 分支占比高，则是知识条目被批量暂停，属运维事件。
- 兜底断言：该规则的命中数不得为 0。长期为 0 意味着 `pesticide_items` 从未被真正校验（例如闸门拿到的是空列表），此时应告警而不是庆幸。

## 验收标准

- `pytest tests/gate/rules/test_pesticide_whitelist.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：两个药剂条目均在库中且均为 `approved`，`check` 返回空列表。
  - 正例 2：`pesticide_items = []`，返回空列表。
  - 反例 1：`knowledge_id="K-9999"` 不在库中，返回 1 条 block 违规，`field == "pesticide_items[K-9999]"`，message 含"不在已核准知识库中"。
  - 反例 2：条目存在但 `status="suspended"`，返回 1 条 block 违规，message 含"当前状态为 suspended"。
  - 反例 3：条目 `approved` 但 `entry.name` 与 `item.name` 不同，返回 1 条 block 违规，`field` 以 `.name` 结尾。
- 断言该规则在 `status="deprecated"` 时同样拦截（`is_approved` 返回 False），并在断言处注明 `deprecated` 保留用于历史追溯、不可用于新处方。
- 断言 `PesticideWhitelistRule.severity == "block"` 且 `rule_id == "R-WHITELIST"`，且 `build_rules(["R-WHITELIST"])` 能实例化它。
- 该模块内 `grep -n "app.agent\|app.services" ` 无结果（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。
