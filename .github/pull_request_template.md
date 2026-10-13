## 改动说明

<!-- 这次改了什么、为什么改。一句话说清"为什么"，比罗列 diff 更有用。 -->

## 发布前核对

逐条勾选；每条右侧是它对应的自动化检查。判定标准与完整清单见
[docs/release-checklist.md](../docs/release-checklist.md)。

- [ ] **迁移**：本次新增或修改了迁移，且能干净回滚
      （CI 第 5 项 `alembic upgrade head && downgrade -1 && upgrade head` 已通过）
- [ ] **评测**：离线评测一致率未相对基线下降
      （`eval-gate` 作业已通过；若为首次评测无基线，门禁会放行并提示）
- [ ] **红线**：受影响的流程配置七条红线完整
      （启动检查 + `R-REDLINE-LOAD` 单测已通过）
- [ ] **无绕过**：未引入 `# noqa` 或 `continue-on-error` 绕过分层检查
      （CI 第 4 项 `check_layers.py` + `check_no_noqa.py` 已通过）
- [ ] **提示词**：若改动提示词，已提高其版本号并写入 generation span
      （`tests/observability/test_langfuse.py` 已通过）
- [ ] **无临时物**：无调试脚本、注释掉的大段代码、裸 `print`
      （CI 第 1 至 3 项 + `tests/test_no_print.py` 已通过）
- [ ] **流程版本**：若改动流程配置，已提高 `config_version`（无自动化检查，人工确认）
- [ ] **tobacco**：若本次涉及 `tobacco` 流程，已确认产品说明书附录 A 的 A9 结论

## 自检方式

本地跑与 CI 第 1 至 7 项一致的序列：`bash backend/scripts/ci.sh`（需 `CI_RUN_EVAL=1`
才跑评测项）。不一致会由 `tests/test_ci_config.py` 拦下。
