#!/usr/bin/env python
"""worker 容器的启动入口（T6-10）。

**为什么需要一个部署入口而不是直接 `arq app.worker.WorkerSettings`**：容器需要一个
可自检、可列任务的入口，便于 `docker compose exec worker ... --list-jobs` 核对
"定时任务与事件消费者确实注册上了"（T6-10 验收标准）。ARQ 自带 CLI 没有列任务选项，
故这里在启动 worker 之前先做两件事：

1. `--list-jobs`：打印注册表（函数任务 + cron 任务）后退出，供编排与运维核对；
2. 无参数：调用 ARQ 自己的 `run_worker`，等价于 `arq app.worker.WorkerSettings`。

**注册表真相只有一处**：本文件从 `app.worker.WorkerSettings` 读取任务，不另建清单——
否则"worker 里注册了什么"会有两个答案。任务清单的职责划分（哪些是 cron、哪些是事件
消费者）见 `app/worker.py` 的模块说明。

**为什么不 `depends_on` 数据库**：worker 是后台消费进程，启动时数据库与 Redis 未就绪
时 ARQ 会自行重连（`RedisSettings` 的连接重试），不需要 Compose 的 healthcheck 门控；
给它加 `depends_on: postgres` 会在数据库短暂重启时连带重启 worker、打断正在跑的 cron。
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":  # pragma: no cover  生产为 Linux 容器，此分支不执行
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from arq.worker import run_worker  # noqa: E402

from app.worker import WorkerSettings  # noqa: E402

# 任务名 → 职责说明。只用于 `--list-jobs` 的可读输出，不参与运行；
# 缺项时打印原始任务名（宁可少一句话，也不因为这个映射漏了而漏列任务）。
_RESPONSIBILITY: dict[str, str] = {
    "dispatch_outbox": "发件箱投递（T1-13，每 5 秒一轮）",
    "scan_due_followups": "回访到期扫描（T3-09，每日 7 点）",
    "deliver_due_reach": "触达发送与重试（T3-12/T3-13，最佳窗口内每 5 分钟）",
    "collect_daily_metrics": "指标采集与质量抽检（T6-09，每日 3 点）",
    "snapshot_requested": "快照生成（T3-08，事件消费者）",
    "prescription_created": "处方创建后登记回访（T3-09，事件消费者）",
    "followup_due": "回访到期立即推送（T3-12，事件消费者）",
    "reach_failed": "触达失败确认（T3-14，事件消费者）",
    "consultation_closed": "问诊关闭（事件消费者）",
    "subscription_consumed": "订阅额度扣减（事件消费者）",
    "outbox_unhandled": "未注册事件的兜底消费者",
}


def _list_jobs() -> int:
    """打印注册表：函数任务与 cron 任务各一段。返回进程退出码。"""
    functions = [fn.__qualname__ for fn in WorkerSettings.functions]
    cron_names = [job.name for job in WorkerSettings.cron_jobs]

    print("ARQ 任务注册表（来自 app.worker.WorkerSettings）")
    print(f"  时区：{WorkerSettings.timezone}")
    print(f"  函数任务（{len(functions)} 个，按队列消费）：")
    for name in sorted(functions):
        print(f"    - {name}: {_RESPONSIBILITY.get(name, '（未登记的职责说明）')}")
    print(f"  cron 任务（{len(cron_names)} 条，按时间触发）：")
    for name in sorted(cron_names):
        # cron 名形如 `cron:collect_daily_metrics`，剥前缀后查职责表。
        key = name.split(":", 1)[-1]
        print(f"    - {name}: {_RESPONSIBILITY.get(key, '（未登记的职责说明）')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--list-jobs" in args:
        return _list_jobs()
    # 与 `arq app.worker.WorkerSettings` 等价：交给 ARQ 自己的运行器，
    # 由它建事件循环、装配 cron 调度与优雅停机（SIGTERM）。
    run_worker(WorkerSettings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
