# 发布检查清单

本清单只列**自动化检查覆盖不到**的事项。每条都指向对应的自动化检查（或显式标注"无"
并写出理由），使"人工判断"与"机器门禁"的边界清晰：不把机器能判的事推给人，也不把
人该判的事伪装成机器结论。

自动化检查的完整清单见 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
（八项）与 [`.github/workflows/eval-regression.yml`](../.github/workflows/eval-regression.yml)
（评测门禁）。本地用 `bash backend/scripts/ci.sh` 跑与 CI 第 1 至 7 项一致的序列。

## 一、发布前人工核对项

| 人工核对项 | 对应的自动化检查 |
| --- | --- |
| 新增或修改的流程配置是否提高了 `config_version` | 无（人工）；评测报告会显示 `config_version` 变化，见 [compare_eval_reports.py](../backend/scripts/compare_eval_reports.py) 的版本提示 |
| 七条流程红线是否完整（缺失则流程不可用） | 启动检查（T1-08，`app/core/startup_check.py`）+ `tests/gate/rules/test_redline_load.py` 的 `R-REDLINE-LOAD` 单测 |
| 本次是否新增迁移，能否干净回滚 | CI 第 5 项"迁移可升降"（`alembic upgrade head && downgrade -1 && upgrade head`）；另见 `backend/scripts/check_migrations.sh` |
| 是否引入 `# noqa` 或 `continue-on-error` 绕过分层检查 | CI 第 4 项（`check_layers.py` + `check_no_noqa.py`）；`tests/test_ci_config.py` 断言 CI 无未登记的 `continue-on-error` |
| 评测一致率是否相对基线下降 | 独立作业 `eval-gate`（`pytest -m eval` + `compare_eval_reports.py`） |
| 提示词是否有版本号且写入 generation span | `tests/observability/test_langfuse.py` |
| 是否存在临时物（调试脚本、注释掉的大段代码、裸 `print`） | CI 第 1 至 3 项 + `tests/test_no_print.py` |
| 若本次改动涉及 `tobacco` 流程，需先确认产品说明书附录 A 的 A9 结论 | 无（人工）；评测步骤对环境变量控制的条件跳过已就位，不让未定项长期让 CI 变红 |
| 版本号是否递增 | 无（人工）：`pyproject.toml` 的 `version` 何时递增取决于发布节奏与对外承诺，无法由代码判定；此处显式标注"无"而非勉强套一项检查 |

## 二、评测基线的更新时机

`baseline.json` 由**主分支最近一次成功运行的报告**固化而来，不是"上一次运行"。
用上一次运行会让连续两次各降 0.9 个百分点（单次都未超 1 个百分点的阈值）逐次通过，
累积成明显退化而无人察觉。

更新动作由 `.github/workflows/eval-regression.yml` 的 `update-baseline` 作业自动执行，
只在该作业 `needs: [eval-gate]` 且主分支推送后触发，提交信息形如
`chore(eval): 更新离线评测基线 [skip ci]`。因此"基线何时被更新"在 git 历史里有据可查。

**初始化基线**：首次接入时仓库中没有 `baseline.json`，门禁按"首次评测"放行并提示
（见 `compare_consistency` 的语义）。主分支第一次成功运行后即会生成基线。

## 三、发布失败时的回退

1. **回退代码**：按发布方式的既有流程回滚到上一个 tag。
2. **回退数据库**：`cd backend && alembic downgrade -1`（可多次），或按 CI 第 5 项已验证的
   路径回退到指定 revision。迁移只保证"一步可回滚"，跨多步回退需要人工确认中间态。
3. **禁止 `docker compose down -v`**：该命令会删除 `pgdata` 卷，等同清库。详见
   [deploy/README.md](../deploy/README.md) 的备份与升级一节。

## 四、这条流水线拦不住什么

诚实地列出边界，避免把"CI 绿了"当成"可以发布"：

- **模型质量**：评测在 `USE_MOCK_LLM=true` 下跑，只回归编排与契约；语义断言（分诊、问诊、
  结论、闸门命中）需要真实模型的离线重放与人工抽检（T6-09 的质量抽检）。
- **知识库准确性**：评测集的 `knowledge_version` 变化会提示但不阻断，知识内容对不对
  仍靠 T5 系列的人工审核流程。
- **部署环境差异**：Compose 编排的冒烟不在 CI 内（CI 无 Docker 依赖），需在预发环境
  按 `deploy/README.md` 的启动顺序验证。
