# 话农点金 · 多智能体协同改造任务总表

> 上游依据：《话农点金_多智能体协同技术实现方案》（`docs/话农点金_多智能体协同技术实现方案.md`）、《话农点金_产品说明书_v2.0》
>
> 任务总量：112 项，按六个阶段组织
> 后端语言：Python 3.11+
> 每项任务都是可独立交给一个执行者完成的工作单元

---

## 这套任务文件怎么用

每个任务文件是一份自足的施工说明：交代做什么、依赖什么、改动哪些文件、写成什么样、怎么验证做完。执行者不需要回读整份技术方案，但需要按文件里标注的章节号去查证细节。

执行顺序按阶段推进，阶段内按编号推进。**跨阶段的依赖不能跳过**：第一步的数据归属与迁移链路是后面所有功能的地基，第二步的流程配置是第三、五步的事实来源。

每份任务文件里的"验收标准"是硬门槛。条目写的是可以跑出来或被观察到的结果，不是"代码写完了"这类无法核对的状态。

---

## 阶段划分

| 阶段 | 目录 | 任务数 | 对应产品说明书步骤 | 完成标志 |
| --- | --- | --- | --- | --- |
| 第一步 底座归位 | `阶段一-底座归位/` | 14 | 9.5 第一步 | 验收项 1、2、3、7、8 通过 |
| 第二步 流程与问诊重构 | `阶段二-流程与问诊重构/` | 61 | 9.5 第二步 | 验收项 4、5、11 通过 |
| 第三步 提示与触达 | `阶段三-提示与触达/` | 14 | 9.5 第三步 | 验收项 6、14 通过 |
| 第四步 适老化改造 | `阶段四-适老化改造/` | 5 | 9.5 第四步 | 验收项 12、13 通过 |
| 第五步 传感器接入 | `阶段五-传感器接入/` | 9 | 9.5 第五步 | 设备可上报并被问诊引用 |
| 第六步 质量与交付 | `阶段六-质量与交付/` | 9 | 6.2、9.1 | 三层评估体系可运行 |

产品说明书 9.2 的十四条验收项编号，在下方各阶段的表格里逐条对照。

---

## 第一步 底座归位

这一阶段不产生农户可见的变化，但它决定后面所有功能的安全性。归属校验、令牌吊销、迁移链路这三项如果留到后面补，会在大批功能上线后变成难以回退的改造。

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T1-01 | 依赖与运行环境基线调整 | 无 | `requirements.txt`、`pyproject.toml` |
| T1-02 | PostgreSQL 接入与异步数据库引擎改造 | T1-01 | `app/db/session.py`、`app/db/base.py` |
| T1-03 | pgvector 与 TimescaleDB 扩展初始化 | T1-02 | 扩展初始化脚本、`env_reading` 时序表 |
| T1-04 | Alembic 迁移链路重建与启动建表移除 | T1-02、T1-03 | `alembic/versions/0001_baseline.py`、`alembic/env.py` |
| T1-05 | OwnedMixin 与 OwnedRepository 归属安全基类 | T1-02 | `app/db/owned.py`、`app/repositories/base.py` |
| T1-06 | 归属校验缺陷整改 | T1-05 | `app/services/`、`app/api/v1/` 全量整改 |
| T1-07 | 令牌吊销与登出失效修复 | T1-05 | `app/services/auth_service.py`、`app/core/security.py` |
| T1-08 | 环境隔离配置与生产启动自检 | T1-01 | `app/core/config.py`、`app/core/startup_check.py` |
| T1-09 | 数据库会话回滚与事务边界 | T1-02、T1-05 | `app/db/session.py`、`app/services/` |
| T1-10 | CORS 收紧与上传校验修复 | T1-08 | `app/main.py`、`app/integrations/storage.py` |
| T1-11 | 历史列表查询性能优化 | T1-05 | `app/services/ai_service.py`、`app/repositories/consultation.py` |
| T1-12 | 分层依赖检查脚本与 CI 门禁 | T1-05 | `scripts/check_layers.py`、CI 配置 |
| T1-13 | event_outbox 事务性发件箱 | T1-02、T1-04 | `app/events/outbox.py`、投递进程 |
| T1-14 | MySQL 历史数据迁移到 PostgreSQL | T1-04、T1-05 | 数据迁移脚本、校验报告 |

对应的十四条验收项：第 1 条（归属）、第 2 条（令牌）、第 3 条（登录降级）、第 7 条（历史列表性能）、第 8 条（会话回滚）。

---

## 第二步 流程与问诊重构

工作量最大的阶段，也是本次改造的核心。七个小组按依赖顺序推进：配置层 → 契约层 → 智能体层 → 编排层 → 闸门层 → 知识库层 → 模型网关。

### 配置层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-01 | ProcessId 与核心枚举定义 | 无 | `app/agent/state.py` 枚举部分 |
| T2-02 | 流程配置 Pydantic 模型 | T2-01 | `app/process_config/models.py` |
| T2-03 | 流程配置加载器与校验 | T2-02 | `app/process_config/loader.py` |
| T2-04 | 七条流程 YAML 配置编写 | T2-03 | `app/process_config/seeds/*.yaml` |
| T2-05 | 流程配置版本化与热加载 | T2-03、T2-07 | `app/process_config/registry.py` |
| T2-06 | 流程红线配置模型与完整性校验 | T2-03 | `app/process_config/redlines.py` |
| T2-07 | 流程配置表与配置同步落库 | T2-02、T1-04 | `app/models/process_config.py` |

### 契约层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-08 | ConsultationState 与 ConsultStage 重构 | T2-01 | `app/agent/state.py` |
| T2-09 | 分诊与现场观察契约 | T2-08 | `app/agent/contracts/triage.py`、`observation.py` |
| T2-10 | 问诊与证据契约 | T2-08 | `app/agent/contracts/interview.py`、`evidence.py` |
| T2-11 | 判定与处方契约 | T2-10 | `app/agent/contracts/diagnosis.py`、`prescription.py` |
| T2-12 | 门禁与报告守卫契约 | T2-09 | `app/agent/contracts/gate.py`、`report.py` |

### 智能体层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-13 | Agent 基类与契约校验重试降级 | T2-09、T2-56 | `app/agent/agents/base.py` |
| T2-14 | 分诊智能体 Triage | T2-13、T2-03 | `app/agent/agents/triage.py` |
| T2-15 | 现场采集智能体 FieldObserver | T2-13 | `app/agent/agents/field_observer.py` |
| T2-16 | 问诊智能体 Interviewer | T2-13、T2-17 | `app/agent/agents/interviewer.py` |
| T2-17 | 追问候选池筛选器 | T2-03、T2-08 | `app/agent/agents/candidate_pool.py` |
| T2-18 | 证据核查智能体 Retriever | T2-13、T2-50 | `app/agent/agents/retriever.py` |
| T2-19 | 判定智能体 Diagnoser | T2-13 | `app/agent/agents/diagnoser.py` |
| T2-20 | 受限并行会诊 Panel 与聚合器 | T2-19 | `app/agent/agents/panel.py` |
| T2-21 | 处方智能体 Prescriber | T2-13、T2-50 | `app/agent/agents/prescriber.py` |
| T2-22 | 报告守卫 ReportGuard | T2-13 | `app/agent/agents/report_guard.py` |
| T2-23 | 转人工上下文包生成器 | T2-19 | `app/agent/agents/handoff.py` |

### 编排层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-24 | 条件边函数 routes | T2-08 | `app/agent/graph/routes.py` |
| T2-25 | 主图组装 main | T2-24、T2-26 | `app/agent/graph/main.py` |
| T2-26 | 流程子图工厂函数 | T2-16、T2-18、T2-19、T2-21 | `app/agent/graph/factories.py` |
| T2-27 | 一期五条流程子图 | T2-26 | `app/agent/graph/subgraphs/{rice,tea,chili,weed,pig}.py` |
| T2-28 | 养猪流程子图与报告守卫结构特例 | T2-25、T2-22 | `app/agent/graph/subgraphs/pig.py` |
| T2-29 | 油菜与通用农业流程子图 | T2-26 | `app/agent/graph/subgraphs/{rapeseed,general}.py` |
| T2-30 | Postgres 检查点器替换与中断恢复 | T1-02 | `app/agent/checkpointer.py` |
| T2-31 | 轮次与成本预算守卫 | T2-08 | `app/agent/budget.py` |
| T2-32 | 人工介入与断点续问 | T2-30、T2-25 | `app/agent/graph/main.py` |
| T2-33 | 问诊接口改造为图驱动 | T2-25、T2-32 | `app/services/ai_service.py`、`app/api/v1/ai.py` |

### 闸门层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-34 | 闸门规则引擎与规则基类 | T2-12 | `app/gate/engine.py`、`app/gate/rules/base.py` |
| T2-35 | 药剂白名单规则 R-WHITELIST | T2-34、T2-48 | `app/gate/rules/pesticide_whitelist.py` |
| T2-36 | 登记作物匹配规则 R-REGISTERED-CROP | T2-34、T2-48 | `app/gate/rules/registered_crop.py` |
| T2-37 | 剂量区间与每季次数规则 | T2-34、T2-48 | `app/gate/rules/dosage.py` |
| T2-38 | 安全间隔期规则 R-INTERVAL | T2-34、T2-04 | `app/gate/rules/safety_interval.py` |
| T2-39 | 用药史冲突规则 R-CONFLICT | T2-34、T2-48 | `app/gate/rules/medication_conflict.py` |
| T2-40 | 地区适宜性规则 R-REGION | T2-34、T2-51 | `app/gate/rules/region_fitness.py` |
| T2-41 | 数字溯源规则 R-PROVENANCE | T2-34、T2-11 | `app/gate/rules/provenance.py` |
| T2-42 | 流程专属红线规则 R-REDLINE | T2-34、T2-06 | `app/gate/rules/process_redline.py` |
| T2-43 | 红线配置加载校验 R-REDLINE-LOAD | T2-34、T2-06 | `app/gate/rules/redline_load.py` |
| T2-44 | 表述语气与无数据支撑规则 | T2-34 | `app/gate/rules/presentation.py` |
| T2-45 | 闸门子图接入与过闸门流程 | T2-34、T2-25 | `app/agent/graph/gate_graph.py` |
| T2-46 | 闸门审计表与影子模式 | T2-45、T1-04 | `app/models/gate_audit.py`、`app/gate/audit.py` |

### 知识库层

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-47 | 知识条目模型与表结构 | T1-03、T1-04 | `app/models/knowledge.py` |
| T2-48 | 知识库索引构建器 | T2-47 | `app/knowledge/indexer.py` |
| T2-49 | 混合检索 KnowledgeRepository | T2-47、T2-52 | `app/knowledge/repository.py` |
| T2-50 | 地区回退匹配与来源标注 | T2-49 | `app/knowledge/region.py` |
| T2-51 | 重排序 rerank 接入 | T2-49 | `app/knowledge/rerank.py` |
| T2-52 | 知识库内容建设一期五条流程 | T2-48 | `app/knowledge/seeds/*.jsonl` |
| T2-53 | 知识库内容建设油菜与通用兜底 | T2-48 | `app/knowledge/seeds/*.jsonl` |
| T2-54 | 知识条目核准流程与状态机 | T2-47 | `app/services/knowledge_review.py` |

### 模型网关

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T2-55 | 模型网关统一入口 | T1-08 | `app/llm/gateway.py` |
| T2-56 | 供应商适配器与结构化输出 | T2-55 | `app/llm/providers/` |
| T2-57 | 任务类型路由与模型档位 | T2-55、T2-02 | `app/llm/router.py` |
| T2-58 | 熔断器与降级链 | T2-55 | `app/llm/circuit.py` |
| T2-59 | token 计量与成本看板 | T2-55、T1-04 | `app/llm/metering.py` |
| T2-60 | 提示词模板组织与版本管理 | T2-02、T2-03 | `app/agent/prompts/` |
| T2-61 | 现有 ai_router 迁移到流程配置 | T2-57、T2-03 | 删除 `app/ai_router/`，功能并入新结构 |

对应的十四条验收项：第 4 条（图片上传链路）、第 5 条（死规则清理）、第 11 条（受理即分流）。

闸门层与知识库层是这一阶段的两条收尾线。闸门是唯一拥有否决权的组件，它的每一条规则都要能被单独测试；知识库是药剂白名单与数字溯源的比对基准，没有它闸门无从判定。

---

## 第三步 提示与触达

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T3-01 | 提示快照数据模型与表结构 | T1-04、T1-05 | `app/models/snapshot.py` |
| T3-02 | 环境事实三级回退采集 | T3-01、T5-08 | `app/broadcast/inputs/env_facts.py` |
| T3-03 | 农事历与档案输入采集 | T3-01、T2-04 | `app/broadcast/inputs/calendar.py` |
| T3-04 | 问诊与回访信号采集 | T3-01、T3-09 | `app/broadcast/inputs/consult.py` |
| T3-05 | 规则层匹配引擎（零模型可用） | T3-02、T3-03、T3-04 | `app/broadcast/rules/engine.py` |
| T3-06 | 优先级排序与条数控制 | T3-05 | `app/broadcast/priority.py` |
| T3-07 | 文案生成与模板出事实 | T3-05、T2-55 | `app/broadcast/writer.py` |
| T3-08 | 快照生成链路与幂等锁 | T3-06、T3-07 | `app/broadcast/snapshot.py` |
| T3-09 | 回访调度服务 | T2-04、T1-04 | `app/services/followup_service.py` |
| T3-10 | 跨日弱化与展示状态 | T3-08 | `app/broadcast/view.py` |
| T3-11 | 订阅授权接口与额度模型 | T1-02、T1-16 | `app/services/quota_service.py` |
| T3-12 | 一次性订阅消息发送链路 | T3-11、T3-08 | `app/broadcast/notifier.py` |
| T3-13 | 静默时段与推送窗口控制 | T3-12 | `app/broadcast/window.py` |
| T3-14 | 触达记录与失败原因分类 | T3-12 | `app/models/reach_record.py` |
| T3-15 | 处方卡台账与执行回执 | T2-33、T3-08 | `app/services/ledger_service.py` |

对应的十四条验收项：第 6 条（提示幂等与条数控制）、第 14 条（主动触达通道）。

这一步的先后关系有一条硬约束：推送内容必须取自已落库的快照条目，实现方式是外键约束而非代码约定。触达链路要晚于快照链路完成。

---

## 第四步 适老化改造

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T4-01 | 公共样式变量与公共类收敛 | 无 | `miniprogram/app.wxss` |
| T4-02 | 逐页字号行距热区核验调整 | T4-01 | 各页面 WXSS |
| T4-03 | 首页层级精简 | T4-01 | `miniprogram/pages/index/` |
| T4-04 | 语音朗读与语音输入 | T4-02 | `miniprogram/utils/voice.ts` |
| T4-05 | 后端文案长度约束配合 | T3-07 | 快照与问诊文案字数上限 |

对应的十四条验收项：第 12 条（字号与热区）、第 13 条（首页层级）。

这一阶段以前端为主，但有两处需要后端配合：提示文案的字数上限、问诊问题的并列选项上限。这两项如果留到前端截断，会在适老化验收时反复返工。

---

## 第五步 传感器接入

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T5-01 | 设备配置表与字段映射表 | T1-04、T1-05 | `app/models/device.py` |
| T5-02 | 厂商模板库与内置模板 | T5-01 | `app/models/vendor_template.py`、模板数据 |
| T5-03 | 设备绑定接口 | T5-01 | `app/api/v1/device.py` |
| T5-04 | 样例报文解析与字段推断 | T5-03、T5-02 | `app/services/device_service.py` |
| T5-05 | 字段映射保存与校验 | T5-04 | `app/services/mapping_service.py` |
| T5-06 | 设备上报接口与密钥校验 | T5-01、T1-04 | `app/api/v1/device_report.py` |
| T5-07 | 读数合法性校验与质量标记 | T5-06 | `app/services/env_service.py` |
| T5-08 | 环境看板接口与来源标注 | T5-07、T3-02 | `app/api/v1/environment.py` |
| T5-09 | 设备列表与解绑 | T5-03 | `app/api/v1/device.py` |

这一步是增强项，缺席不影响前四步可用。但 `env_reading` 时序表要在第一步就建好，否则后期改时序表结构的代价很高。

---

## 第六步 质量与交付

| 编号 | 任务 | 依赖 | 主要产出 |
| --- | --- | --- | --- |
| T6-01 | 离线评测集 Schema 与运行器 | T2-08 | `tests/eval/schema.py`、`runner.py` |
| T6-02 | 七条流程评测集内容 | T6-01、T2-52 | `tests/eval/cases/*.json` |
| T6-03 | 闸门规则单测用例 | T2-34 至 T2-44 | `tests/gate/` |
| T6-04 | 图路径测试 | T2-25 | `tests/graph/` |
| T6-05 | 契约测试 | T2-09 至 T2-12 | `tests/contracts/` |
| T6-06 | OpenTelemetry 链路追踪接入 | T2-25 | `app/observability/tracing.py` |
| T6-07 | Langfuse 私有化部署与提示词版本对比 | T6-06 | 部署配置、`app/observability/langfuse.py` |
| T6-08 | 结构化日志与敏感信息脱敏 | T6-06 | `app/observability/logging.py` |
| T6-09 | 线上指标采集与模型输出抽检 | T6-06、T2-46 | `app/observability/metrics.py` |
| T6-10 | Docker Compose 部署编排 | T1-01 | `docker-compose.yml` |
| T6-11 | CI 流水线与发布检查 | T1-12、T6-01 | CI 配置 |

---

## 全局约定

所有任务共用一份约定，见 [00-全局约定与执行规范.md](./00-全局约定与执行规范.md)。内容包括目标目录结构、分层依赖的硬规则、契约设计约束、任务文件的统一格式、以及每个任务的完成定义。

动手前先读这一份。它规定的几条约束（闸门不得依赖智能体层、智能体不得直接访问 ORM、任何模块不得直接使用 `db.query`）会由 CI 脚本检查，违反会让构建失败。
