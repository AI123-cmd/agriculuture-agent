# T2-22 报告守卫 ReportGuard

## 这一项要做什么

在 `app/agent/agents/report_guard.py` 中实现报告守卫，输出 `ReportDecision`。它检测识别信号并在命中时中止问诊、给出强制动作与本地联系入口。它是仅有的两个守卫组件之一，失败行为是 fail-closed：有疑即报。 本项要纠正的认知盲区是：**不是只有群死群伤才需报告，个别猪只出现可疑症状同样触发报告义务**。因此 `trigger_source = "signal"` 的判定条件必须极低，单个识别信号命中即触发。

## 前置依赖

- 依赖 T2-13：必须已提供 `BaseAgent`、`FailureMode.FAIL_CLOSED`；依赖 T2-12：必须已提供 `ReportDecision`、`ContactInfo`、`ReportSignal`
- 依赖 T2-08：必须已提供 `ConsultationState`，守卫读取全量槽位；依赖 T2-09：必须已提供 `ObservationResult`，现场事实是信号来源之一
- 依赖 T2-02：必须已提供养猪流程的必问槽位与识别信号槽位配置；外部条件：识别信号清单、法定报告对象与联系方式由农技与产品确认
- 与 T2-28 的关系：主图中的挂载位置（`observe` 与 `interview` 之后各一次）由 T2-28 实现

## 参考章节

- 技术方案 第5章 5.3.7 报告守卫契约 ReportDecision（全字段与优先级说明）；技术方案 第5章 5.3.7 优先级（作为条件边的旁路监听者，在 observe 与 interview 之后各挂一次）
- 技术方案 第5章 5.1 智能体总表（⑦报告守卫，极低自治，规则优先，fail-closed）；产品说明书 5.7 认识盲区（不是只有群死群伤才需报告）
- 技术方案 第6章 6.7 转人工升级（法定报告义务的输出内容）；技术方案 第7章 7.4 养猪流程的结构特例（report_guard 在 observe 之前插入）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/report_guard.py` | `ReportGuardAgent`、联系方式解析 |
| 新增 | `app/agent/agents/report_signals.py` | 识别信号表与匹配逻辑（纯函数） |
| 新增 | `tests/agent/test_report_guard.py` | 单信号触发、fail-closed、联系入口的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出 `ReportGuardAgent` |

## 实现要点

### 信号定义

```python
class SignalRule(BaseModel):
    """一条识别信号规则。keywords 命中任一即触发。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    signal_id: str
    description: str
    keywords: tuple[str, ...]
    slot_key: str | None = None
    severity: Literal["signal", "group", "mass_death"] = "signal"
    source: Literal["slot", "text", "observation"] = "slot"
# (signal_id, 描述, 关键词, 槽位, 等级)
_SIGNAL_TABLE: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("sudden_death", "无明显诱因突然死亡",
     ("突然死", "猝死", "早上还好好的", "一夜之间死了"), "pig_death_without_cause", "signal"),
    ("skin_hemorrhage", "皮肤出现出血点或紫斑",
     ("出血点", "紫斑", "发紫", "身上有红点"), "pig_skin_lesion", "signal"),
    ("high_fever", "持续高热",
     ("高烧", "发烧不退", "体温高"), "pig_fever", "signal"),
    ("abortion", "母猪流产或死胎",
     ("流产", "死胎", "早产"), "pig_abortion", "signal"),
    ("multi_pen", "多个栏舍同时出现相似症状",
     ("好几个栏", "几栋都有", "相邻栏也"), "pig_scope_multi_pen", "group"),
    ("mass_death", "短期内群体性死亡",
     ("死了好多", "成片死", "大量死亡"), "pig_death_scale", "mass_death"),
)
PIG_SIGNALS: tuple[SignalRule, ...] = tuple(
    SignalRule(signal_id=s, description=d, keywords=k, slot_key=sk,
               severity=lv, source="slot")
    for s, d, k, sk, lv in _SIGNAL_TABLE
)
```

前四条都是 `severity="signal"`，任何一条命中即触发报告。这正是"个别猪只出现可疑症状同样触发报告义务"的落地。后两条是更严重的情形，会体现在 `trigger_source` 上，但不影响是否触发（前四条已足够）。关键词表属农技口径，需人工确认，执行者按给定清单录入。

### 匹配逻辑（纯函数）

```python
def match_signals(state: ConsultationState, observations: ObservationResult | None,
                  rules: tuple[SignalRule, ...] = PIG_SIGNALS) -> list[str]:
    """返回命中的信号 id 列表。任一命中即触发，不做数量门槛。"""
    hits: list[str] = []
    slots = state.get("slots") or {}
    text = state.get("user_message") or ""
    for rule in rules:
        if rule.source == "slot" and rule.slot_key:
            slot = slots.get(rule.slot_key)
            if slot is None or slot.status != "filled" or not slot.value:
                continue
            if _contains_any(slot.value, rule.keywords) or _is_affirmative(slot.value):
                hits.append(rule.signal_id)
        elif rule.source == "text" and _contains_any(text, rule.keywords):
            hits.append(rule.signal_id)
    return hits

def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)

def _is_affirmative(value: str) -> bool:
    """槽位值本身即为肯定回答时命中（如 options 为是/否 时值为'是'）。"""
    return value.strip() in {"是", "有", "出现", "已出现", "是的"}
```

匹配是**纯字符串包含**，不调用模型。技术方案 5.1 把报告守卫的自治程度标为"极低（规则优先）"，6.5 的独立性表明确要求"不调用模型"。因此即使模型认为"这一例不严重"，也不能阻止信号命中后的触发。`_contains_any` 与 `_is_affirmative` 是并集关系，同时支持选项式提问与自由描述。

### 决定 trigger_source

```python
def resolve_trigger_source(hits: list[str], rules: tuple[SignalRule, ...] = PIG_SIGNALS) -> str:
    """按最严重的一条信号决定 trigger_source。未命中返回 none。"""
    if not hits:
        return "none"
    severity_of = {r.signal_id: r.severity for r in rules}
    severities = [severity_of.get(h, "signal") for h in hits]
    if "mass_death" in severities:
        return "mass_death"
    if "group" in severities:
        return "group"
    return "signal"
```

`trigger_source` 记录的是**最严重的命中等级**，而不是"命中了哪一级才算触发"。数量或范围信息（group、mass_death）用于让输出文案更贴合实际情况，不用于抬升触发门槛。

### 类骨架与决策构造

```python
class ReportGuardAgent(BaseAgent[ReportDecision]):
    name = "report_guard"
    output_schema = ReportDecision
    task_type = TaskType.REPORT_GUARD
    failure_mode = FailureMode.FAIL_CLOSED      # 有疑即报
    max_attempts = 1                            # 守卫是规则实现，无重试语义
    def __init__(self, contacts: ContactDirectory) -> None:
        super().__init__(gateway=None)          # 守卫不持有模型网关
        self._contacts = contacts
    def collect_decision(self, state: ConsultationState, ctx: AgentContext) -> ReportDecision:
        """核心决策：规则匹配，无模型参与。"""
        raw = state.get("observations")
        obs = ObservationResult.model_validate(raw) if raw else None
        hits = match_signals(state, obs)
        source = resolve_trigger_source(hits)
        if not hits:
            return ReportDecision(triggered=False, matched_signals=[], trigger_source="none",
                                  mandatory_actions=[], contacts=[], stop_consultation=False)
        return ReportDecision(
            triggered=True, matched_signals=hits, trigger_source=source,
            mandatory_actions=self._mandatory_actions(source),
            contacts=self._resolve_contacts(ctx), stop_consultation=True)
```

`super().__init__(gateway=None)` 需要基类支持：守卫不使用模型，`run` 方法应被覆盖为直接调用 `collect_decision`，不走模型调用与重试路径。这个特例要在类 docstring 中说明，避免读者以为遗漏了网关注入。

### 强制动作按等级

```python
    def _mandatory_actions(self, source: str) -> list[str]:
        base = [
            "立即把发病猪只与健康猪只分开，单独饲喂与管理",
            "对栏舍、用具与通道进行消毒，人员进出更换衣物鞋具",
            "不要自行处理或出售病死猪只",
        ]
        if source in ("group", "mass_death"):
            base.append("暂停猪只与人员的场内外流动")
            base.append("尽快联系当地畜牧兽医部门，说明发病头数与时间")
        else:
            base.append("尽快联系当地畜牧兽医部门报告，个别发病也需要报告")
        return base
```

`signal` 分支的最后一条刻意写明"个别发病也需要报告"。这条文案本身就承担纠正认知盲区的作用：农户看到的不只是"要报告"，还有"我这种情况也要报"，从而不会因为"只有一头"而自行判断无需报告。动作里包含"不要自行处理或出售病死猪只"，这是法定义务提示；文案中不得出现任何疾病名称或"确诊"字样，因为技术方案 6.7 明确养猪流程的疫情报告"不做疫情的最终认定"。

### 联系入口

```python
    def _resolve_contacts(self, ctx: AgentContext) -> list[ContactInfo]:
        """按地区解析联系入口。解析不到时必须留空并告警，不编造号码。"""
        contacts = self._contacts.for_region(ctx.region_code)
        if not contacts:
            log.error("report contacts unresolved",
                      extra={"region_code": ctx.region_code, "trace_id": ctx.trace_id})
            return [ContactInfo(kind="hotline", name="当地畜牧兽医部门", phone=None,
                                note="请通过当地政府或农业农村部门查询联系电话")]
        return contacts
```

解析不到联系方式时返回一条 `phone=None` 的条目并记 ERROR 日志。绝不编造电话号码：编造的联系方式会把农户引向无效渠道，比不提供更糟。`ContactInfo.phone` 允许为空（T2-12 已定义），正是为这种情况留的出口。

### fail-closed 路径

```python
    async def fail_closed(self, state: ConsultationState, ctx: AgentContext, reason: str) -> ReportDecision:
        """守卫失败路径：有疑即报。宁可多报一次，不可漏报一次。"""
        log.error("report guard failed, defaulting to report",
                  extra={"reason": reason, "trace_id": ctx.trace_id})
        return ReportDecision(
            triggered=True, matched_signals=["guard_failure"], trigger_source="signal",
            mandatory_actions=["暂停猪只移动与售卖", "尽快联系当地畜牧兽医部门说明情况"],
            contacts=self._resolve_contacts(ctx), stop_consultation=True)
```

守卫自身异常时**仍然报**。守卫是 fail-closed 组件，宁可多报一次，不可漏报一次。误报的代价是农户多打一个电话确认，漏报的代价是动物疫病扩散，这个取舍在产品说明书 6.1 中有依据（安全性优先级最高，且不接受用准确率来换）。 `matched_signals` 填 `guard_failure` 而不是伪造一个具体症状名，是为了让审计能区分"因规则命中而报"与"因守卫异常而报"。后者占比升高说明守卫实现有问题，需要修复而不是调信号表。 按技术方案 5.3.7，本契约的判定**优先于问诊与判定环节**：主图在 `observe` 与 `interview` 节点之后各挂一次检查，命中即 `stop_consultation = True` 并跳转 `handoff`，已收集的问诊内容保留但不继续追问。挂载点由 T2-28 实现，本项只提供 `collect_decision` 作为条件边的判据来源。

## 验收标准

- `pytest tests/agent/test_report_guard.py` 全部通过，含三类用例：单个 `signal` 级信号命中即 `triggered=True`、全部信号未命中时 `trigger_source="none"` 且 `stop_consultation=False`、守卫内部异常时仍返回 `triggered=True`；断言 `match_signals` 对"昨天死了一头"文本返回非空并可命中 `sudden_death`
- 断言仅一条 `signal` 级命中时 `trigger_source="signal"`，同时命中 `mass_death` 时为 `"mass_death"`；断言 `_mandatory_actions("signal")` 中包含"个别发病也需要报告"字样
- 断言所有 `mandatory_actions` 文案不含任何疾病名称或"确诊"字样；断言联系方式解析失败时返回一条 `phone=None` 的 `ContactInfo` 且记录 ERROR 日志
- 断言 `ReportGuardAgent.__init__` 不接收模型网关，`collect_decision` 执行期间零模型调用；断言 `ReportDecision.triggered=True` 时 `stop_consultation` 恒为 True
- 断言同一状态连续调用 `collect_decision` 100 次，`matched_signals` 顺序与内容完全一致；`ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0
