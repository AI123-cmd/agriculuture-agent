# T2-43 红线配置加载校验 R-REDLINE-LOAD

## 这一项要做什么

在 `app/gate/rules/redline_load.py` 中实现 `R-REDLINE-LOAD`：流程红线配置必须成功加载，否则直接判定 `block`。这条规则是"红线缺失即不可用"在闸门内的落点——`process_redlines_loaded=False` 时**直接判 block，不降级为通用规则**。产品说明书 9.3 明确"某条流程的红线配置缺失时，该流程应处于不可用状态，禁止以通用规则代替"，技术方案 5.3.6 也写明"该字段为 False 时直接判定 block，而不是降级为通用规则继续输出"。它是全系统 fail-closed 最纯粹的一条规则：不检查处方内容，只检查前提条件是否成立。等级 `block`。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`GateViolation`、`GateRuleId`、引擎的 `_PRECHECK_RULE_IDS` 短路机制（本规则排在执行顺序第一位）。
- 依赖 T2-06：必须已提供 `LoadedRedlines`（含 `loaded`、`unavailable_reason`、`process_id`、`version`、`declared_rule_ids()`）与 `RedlineValidator`。本项消费其产物，不重复校验配置结构。
- 依赖 T2-24：路由层的 `route_after_intake` 等在 `redlines_loaded=False` 时指向 `handoff`；本规则是同一约束在闸门内的第二道执行点（路由拦住"能不能进流程"，闸门拦住"能不能出方"）。
- 依赖 T2-45：闸门子图把本规则的结果映射为 `GateResult.process_redlines_loaded` 与 `decision=block`。
- 外部条件：无（本规则不读任何外部内容）。

## 参考章节

- 技术方案 第8章 8.4 流程红线配置化（"红线缺失即不可用"的落地：启动时校验，任一条流程红线为空或加载失败即标记不可用，入口处直接返回转人工引导）；第8章 8.2 规则清单（`R-REDLINE-LOAD` 一行）
- 技术方案 第5章 5.3.6 门禁契约（`process_redlines_loaded = False` 时直接判定 block，而不是降级为通用规则继续输出）
- 技术方案 第6章 6.2 路由决策的四个约束（第四条：每条流程红线配置未加载时，路由直接指向 `handoff`）
- 技术方案 第8章 8.5 审计与复盘（`checked_rules` 反查漏配：声明了但没执行是配置错误）
- 产品说明书 9.3 流程专属红线不可缺省（缺失即视为不可用，不得降级为通用规则输出）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/redline_load.py` | `RedlineLoadRule` |
| 修改 | `app/gate/engine.py` | 确认本规则在 `_PRECHECK_RULE_IDS` 中且提前短路 |
| 新增 | `tests/gate/rules/test_redline_load.py` | 加载成功/失败、不降级、短路行为用例 |
| 新增 | `tests/gate/test_redline_load_no_fallback.py` | 断言失败时不执行通用规则（回归守卫） |

## 实现要点

### 规则实现

```python
@register
class RedlineLoadRule(Rule):
    """R-REDLINE-LOAD：流程红线配置必须成功加载，否则直接 block。

    产品说明书 9.3：流程专属红线不可缺省，缺失即不可用，禁止以通用规则代替。
    因此本规则不检查处方内容——前提不成立时，任何关于处方的判断都不该被相信。
    """

    rule_id = GateRuleId.REDLINE_LOAD.value
    severity: GateSeverity = "block"
    description = "流程红线配置必须成功加载"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        if ctx.redlines.loaded:
            return []
        reason = ctx.redlines.unavailable_reason or "未给出原因"
        return [self.violation(
            field="process_redlines_loaded",
            message=(
                f"流程 {ctx.process_id.value} 的红线配置未成功加载"
                f"（{reason}），按不可用处置，不降级为通用规则"
            ),
        )]
```

整条规则只有两个分支，这是刻意的：它必须足够简单到无法出错，因为它决定的是"其余校验结果是否可信"。

### 为什么不能降级为通用规则

"降级为通用规则继续输出"看起来更"服务用户"，但在本产品里是绝对错误的。三条理由。

第一，**红线的差异正是产品安全的基础**。茶流程要求"已登记 + 安全间隔期 + 残留限量"三重条件，辣椒流程要求"不得把青枯病与枯萎病混为一谈"，杂草流程要求"不得推荐激素类除草剂用于无人机施药"。这些约束写在各流程的红线配置里，通用规则里没有。用通用规则跑茶流程，等于把一套针对旱地大田的宽松标准套在"鲜叶入口"的茶树上。

第二，**缺失的原因不可知**。`unavailable_reason` 可能是"配置文件被删"、"YAML 语法错误"、"校验不通过"、"引用了不存在的规则 id"。这些原因下，系统对"红线是什么"一无所知。在不知道约束是什么的情况下继续输出用药建议，是把不确定性转嫁给农户。

第三，**fail-closed 在配置层的体现**。全局约定第六节区分两类失败行为：守卫类（报告守卫、安全闸门）fail-closed，其余智能体降级但仍服务。本规则属于守卫类，不确定时的正确选择是更保守的一方。

### 引擎短路与 checked_rules

`GateEngine._PRECHECK_RULE_IDS` 把本规则排在执行顺序第一位，且 `_gate_open` 在 `redlines.loaded=False` 时返回 False，使后续规则整体跳过。两次实测结果：

| 场景 | `checked_rules` | `decision` | `passed` |
| --- | --- | --- | --- |
| 红线加载成功 | 全部规则 id | 按违规聚合 | 视违规而定 |
| 红线加载失败 | `["R-REDLINE-LOAD"]` | `block` | `False` |

这带来一个必须显式处理的后果：红线加载失败时，`checked_rules` 天然不覆盖 `redlines.declared_rule_ids()`。8.5 第三条复盘用途要求"定期核对 `checked_rules` 是否覆盖声明的全部规则 id，发现声明了但没执行是配置错误"。因此需要区分两种"未覆盖"：

- **因短路而未覆盖**：`process_redlines_loaded=False`，属预期，审计表记 `decision=block`，由 14.2 的"block 事件必须人工复核"流程处置。
- **因漏配而未覆盖**：`process_redlines_loaded=True` 但 `checked_rules` 少了某个声明的规则 id，属实现缺陷，应写审计告警并告警开发者。

判据代码：

```python
# app/gate/engine.py 中的审计辅助（供 T2-46 调用）
def uncovered_rule_ids(self, ctx: GateContext, result: GateResult) -> list[str]:
    """返回声明了但未执行的规则 id。红线未加载时短路导致的缺口不算缺口。"""
    if not result.process_redlines_loaded:
        return []
    declared = set(ctx.redlines.declared_rule_ids())
    return sorted(declared - set(result.checked_rules))
```

### 与启动自检、路由层的三层防线

| 层 | 位置 | 时机 | 行为 |
| --- | --- | --- | --- |
| 配置层 | `RedlineValidator`（T2-06） | 加载时 | 产出 `LoadedRedlines`，`loaded` 布尔 |
| 启动层 | `build_availability` + T1-08 自检 | 进程启动时 | 七条流程任一不可用 → 该流程在路由表标记不可用 |
| 路由层 | `route_after_*`（T2-24） | 每次会话 | `redlines_loaded=False` 直接指向 `handoff` |
| 闸门层 | 本规则 | 每次开方 | `loaded=False` 直接 `block` |

四层都做同一件事不是冗余，而是"物理上无法绕过"的落地方式：任何一层单独失效（例如路由的 state 字段被旧检查点覆盖、启动自检被跳过），其余层仍能拦住。这也是 L1 分层规则存在的意义——闸门不依赖智能体层，因此它这一层始终可用。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-REDLINE-LOAD'` 聚合。

- **该规则的命中数必须恒为 0**。任何一次命中都意味着一条流程在运行期处于不可用状态，属需要立即介入的事故，而非可接受的拦截。监控方式不是"看命中率"，而是"命中即告警"。
- **按 `violations[].message` 中的 `unavailable_reason` 分类**：若是"配置文件被删"或"引用了不存在的规则 id"，属配置发布事故，回滚配置即可；若是"redlines 为空"，属配置遗漏，需补齐红线（且补齐前该流程应保持不可用）。
- **与 `checked_rules` 长度的联合观察**：该规则命中时 `checked_rules == ["R-REDLINE-LOAD"]`（长度 1）。若命中时 `checked_rules` 长度大于 1，说明引擎短路逻辑未生效，属实现缺陷，优先级最高。
- **兜底断言**：本规则的命中数不得被"优化"掉。若长期为 0 且从未有过加载失败的演练，需在测试环境人为制造一次加载失败，验证告警链路可用。

## 验收标准

- `pytest tests/gate/rules/test_redline_load.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：`ctx.redlines.loaded=True` 且 `redlines` 非空，返回空列表。
  - 正例 2：`ctx.redlines.loaded=True` 但 `redlines` 为空元组（理论上不应出现，用于确认判据只看 `loaded`），返回空列表。
  - 反例 1：`loaded=False`、`unavailable_reason="redlines 为空"`，返回 1 条 block 违规，`field == "process_redlines_loaded"`，message 含流程 id 与原因。
  - 反例 2：`loaded=False`、`unavailable_reason=None`，返回 1 条违规，message 含"未给出原因"。
- `pytest tests/gate/test_redline_load_no_fallback.py` 全部通过，断言：`loaded=False` 时 `evaluate` 的 `checked_rules == ["R-REDLINE-LOAD"]`、`decision == "block"`、`passed is False`，且**任何通用规则（`R-WHITELIST`、`R-DOSAGE` 等）均未执行**（用 mock 计数各规则 `check` 的调用次数，断言均为 0）。
- 断言 `uncovered_rule_ids` 在 `loaded=False` 时返回空列表（短路缺口不算漏配），在 `loaded=True` 且人为从 `checked_rules` 移除一个声明规则时返回该 id。
- 断言本规则在 `build_rules()` 的返回列表里排第一位（`_PRECHECK_RULE_IDS` 生效），并断言 `severity == "block"`。
- 断言 `GateContext` 上不存在任何"降级为通用规则"的开关字段（`grep` 断言无 `fallback_rules`、`allow_generic` 之类字段）。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。
