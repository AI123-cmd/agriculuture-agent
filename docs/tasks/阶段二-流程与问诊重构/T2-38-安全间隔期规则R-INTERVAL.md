# T2-38 安全间隔期规则 R-INTERVAL

## 这一项要做什么

在 `app/gate/rules/safety_interval.py` 中实现 `R-INTERVAL`：安全间隔期必须显式给出，且必须与下一轮采摘计划核算不冲突；茶流程缺少 `next_pick_date` 即 block。这条规则承载的是茶农最真实的痛点——"采不了"比减产更严重（产品说明书 5.3）。技术方案 8.3.2 给出了本规则的完整实现，本项**原样保留该实现**（含茶流程专属的两条分支），只在其上补齐注册装饰器、通用流程的等价约束与测试。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`GateViolation`、`GateRuleId`、`ctx.slot_date()` 与 `ctx.today`。
- 依赖 T2-04：必须已提供茶流程配置（`required_slots` 含 `tea_last_pick`，红线 `TEA-03` 与 `TEA-04` 均引用 `R-INTERVAL`），以及 `next_pick_date` 在流程配置中的归属说明。
- 依赖 T2-11：必须已提供 `Prescription.safety_interval_days` 的字段语义（`int | None`，顶层取候选中最保守者）。
- 依赖 T2-06：`RULE_PAYLOAD_SPEC` 中 `R-INTERVAL` 接受键 `{min_days, by_pesticide}`；本项读 `ctx.rule_configs` 中的 `min_days` 与 `by_pesticide`。
- 外部条件：安全间隔期数值为农技内容，需人工确认（全局约定第八节）。

## 参考章节

- 技术方案 第8章 8.3.2 安全间隔期与采摘计划冲突（本规则的完整实现原文，含茶流程专属分支）
- 技术方案 第8章 8.4 流程红线配置化（`tea.yaml` 的 `TEA-03` 要求字段存在、`TEA-04` 要求与采摘计划不冲突，闸门侧实现为两条判定分支）
- 技术方案 第9章 9.1 流程配置模型（`redlines` 的 `data.by_pesticide` 结构）；第5章 5.3.5 处方契约（`safety_interval_days`）
- 产品说明书 第5章 5.3 茶流程（上一轮采摘时间与下一轮采摘计划缺失则不得出用药方案；不得开出未核算安全间隔期与每季最多使用次数的方案；不得开出与采摘节奏冲突的方案）
- 产品说明书 4.2.3 功能点 7（茶流程回访时点按安全间隔期结束与下一轮采摘计划）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/safety_interval.py` | `SafetyIntervalRule`（方案 8.3.2 原文 + 注册装饰器 + 通用流程分支） |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_safety_interval.py` | 茶流程四条用例 + 通用流程两条用例 |
| 修改 | `app/process_config/seeds/tea.yaml` | 确认 `tea_next_pick` 槽位声明（若 T2-04 未声明则补） |

## 实现要点

### 方案 8.3.2 原文（逐字保留）

```python
# gate/rules/safety_interval.py
class SafetyIntervalRule:
    """R-INTERVAL：安全间隔期必须明确，且不得与采摘节奏冲突。

    产品说明书 5.3：不得开出未核算安全间隔期与每季最多使用次数的方案；
    不得开出与采摘节奏冲突的方案——茶农面临的不只是减产，而是"采不了"。
    """
    rule_id = "R-INTERVAL"
    severity = "block"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations = []

        # 1. 安全间隔期必须显式给出（杂草流程允许依据标签，但标签本身即知识条目）
        if ctx.prescription.pesticide_items and ctx.prescription.safety_interval_days is None:
            violations.append(GateViolation(
                rule_id=self.rule_id,
                severity="block",
                message="涉及用药但未给出安全间隔期",
                field="safety_interval_days",
            ))

        # 2. 茶流程：与下一轮采摘计划核对
        if ctx.process_id == ProcessId.TEA:
            next_pick = ctx.slots.get("next_pick_date")
            if ctx.prescription.pesticide_items and next_pick is None:
                violations.append(GateViolation(
                    rule_id=self.rule_id,
                    severity="block",
                    message="茶流程缺少下一轮采摘计划，无法核算安全间隔期",
                    field="slots.next_pick_date",
                ))
            elif next_pick and ctx.prescription.safety_interval_days:
                days_to_pick = (next_pick - ctx.today).days
                if days_to_pick < ctx.prescription.safety_interval_days:
                    violations.append(GateViolation(
                        rule_id=self.rule_id,
                        severity="block",
                        message=(
                            f"距下一轮采摘仅 {days_to_pick} 天，"
                            f"短于安全间隔期 {ctx.prescription.safety_interval_days} 天"
                        ),
                        field="safety_interval_days",
                    ))
        return violations
```

原文保留，不删不改。以下三处是在原文之上的补全。

第一，**槽位读取改为 `ctx.slot_date("next_pick_date")`**。原文写 `next_pick = ctx.slots.get("next_pick_date")` 并直接做 `next_pick - ctx.today`，隐含假设该槽位已是 `date` 对象。落到实现时槽位值来自 state，可能是 ISO 字符串或 `unknown`。`GateContext.slot_date()` 封装了"空、unknown、解析失败一律返回 None"，正好对上原文的 `next_pick is None` 分支——解析失败也走"缺少下一轮采摘计划"这条 block，是 fail-closed 的正确方向。这是本项**唯一**对原文逻辑的形态调整，语义不变：非日期一律视为缺失。

第二，**加 `@register` 与类继承**，`class SafetyIntervalRule(Rule):`，`severity` 标注为 `GateSeverity`。方案 8.3.2 的原始类名为 `SafetyIntervalRule`，与 T2-06 的 `GateRuleId.INTERVAL` 对应，注册表按 `rule_id` 索引，类名不影响。

第三，**补通用流程的"间隔期不低于配置下限"分支**。原文只覆盖了茶流程的采摘计划冲突，但 8.4 的 `tea.yaml` 给出了 `safety_interval.by_pesticide` 结构，且 `R-INTERVAL` 的 `data` 载荷接受 `min_days`。补一个分支：若红线配置给出 `min_days` 而处方顶层 `safety_interval_days` 小于它，判 block。这条对全部流程生效（配置未给 `min_days` 时不触发）。

```python
        # 3. 配置下限核对（TEA-03 的 R-INTERVAL 分支）：适用于全部流程
        floor = self._configured_floor(ctx)
        if floor is not None and ctx.prescription.safety_interval_days is not None:
            if ctx.prescription.safety_interval_days < floor:
                violations.append(self.violation(
                    field="safety_interval_days",
                    message=(f"安全间隔期 {ctx.prescription.safety_interval_days} 天"
                             f"短于本流程配置下限 {floor} 天"),
                ))
```

### 茶流程专属逻辑的三条硬约束

这三条是本项不可妥协的部分，逐条对应产品说明书原文。

| 约束 | 原文对应 | 实现分支 | 缺失时的行为 |
| --- | --- | --- | --- |
| 安全间隔期必须显式给出 | 5.3"不得开出未核算安全间隔期…的方案" | 分支 1 | 涉及用药但 `safety_interval_days is None` → block |
| 必须与下一轮采摘计划核算不冲突 | 5.3"不得开出与采摘节奏冲突的方案" | 分支 2 的 elif | `days_to_pick < safety_interval_days` → block |
| 缺 `next_pick_date` 即 block | 5.3"此项缺失则不得出用药方案" | 分支 2 的 if | `next_pick is None` 且涉及用药 → block |

第三条是"缺失即拦截"而非"缺失则跳过"，这是 fail-closed 的直接体现：农户没说下一轮什么时候采，系统就无法证明方案不冲突，此时唯一安全的结论是不放行。注意该分支仅在 `ctx.prescription.pesticide_items` 非空时触发——没有用药就没有间隔期问题，纯栽培管理建议不受约束。

### 为什么 `days_to_pick` 允许为负

原文用 `elif next_pick and ...` 而非 `> ctx.today`。若 `next_pick` 早于 `today`（农户填了一个已过去的日期），`days_to_pick` 为负，必定小于任何正的间隔期，因此仍然 block。这一行为是对的：已经过了采摘日的方案本身需要人工确认，不能自动化放行。测试应覆盖这一情形。

### 杂草流程与无药剂情形

原文注释指出"杂草流程允许依据标签，但标签本身即知识条目"，即杂草流程不做采摘计划核对（`ctx.process_id == ProcessId.TEA` 的判定使它天然豁免），但仍受分支 1 约束。无药剂处方的三条分支全部不触发，规则返回空列表。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-INTERVAL'` 聚合，按 message 特征分三类统计。

- **"未给出安全间隔期"**：模型或处方层漏填字段，属生成侧缺陷，应对应调处方提示词与 `Prescription` 契约校验。
- **"缺少下一轮采摘计划"**：槽位采集问题。该分支在茶流程高发，直接说明 `tea_next_pick` 槽位的追问优先级或选项设计有问题，是 T2-17 候选池的输入。此值应随迭代快速下降。
- **"短于安全间隔期"**：这类是有效拦截，命中数代表真实避免的合规风险。建议单独建看板，不做下降要求。
- 交叉观察：`R-INTERVAL` 的第三类命中数与 `R-MAX-USE` 命中数同时偏高，说明知识库的间隔期数据偏保守或偏严，需要农技复核数值，而不是继续加拦截。

## 验收标准

- `pytest tests/gate/rules/test_safety_interval.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1（茶）：`pesticide_items` 非空、`safety_interval_days=7`、`next_pick_date=today+10`，返回空列表。
  - 正例 2（通用）：`pesticide_items` 为空、`safety_interval_days=None`，返回空列表（无用药不触发分支 1）。
  - 反例 1（茶）：`pesticide_items` 非空、`safety_interval_days=None`，返回违规，`field == "safety_interval_days"`，message 含"未给出安全间隔期"。
  - 反例 2（茶）：`pesticide_items` 非空、`safety_interval_days=7`、`next_pick_date` 缺失（`slots` 中为 `"unknown"`），返回违规，`field == "slots.next_pick_date"`，message 含"缺少下一轮采摘计划"。
  - 反例 3（茶）：`next_pick_date=today+3`、`safety_interval_days=7`，返回违规，message 含"距下一轮采摘仅 3 天"。
  - 反例 4（茶）：`next_pick_date=today-1`（已过去），返回违规，断言 `days_to_pick` 为负仍触发。
- 断言 `slots` 中 `next_pick_date` 为非 ISO 字符串（如 `"下个月吧"`）时，`ctx.slot_date` 返回 `None` 并触发"缺少下一轮采摘计划"分支，注释注明这是 fail-closed。
- 断言配置 `rule_configs["R-INTERVAL"]["min_days"]=10` 且处方 `safety_interval_days=7` 时返回 1 条违规，message 含"短于本流程配置下限 10 天"。
- 断言 `rule_id == "R-INTERVAL"`、`severity == "block"`，`build_rules(["R-INTERVAL"])` 可实例化。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。
