"""任务类型的镜像定义。

`TaskType` 的**唯一定义处**是 `app/agent/state.py`（技术方案 5.2「枚举封闭」）。但
全局约定 L3 禁止 `app/llm/` 导入任何业务模块，而网关的入口签名必须写出 `task_type`
的类型、路由的表要以它为键——两者只能这样调和：在网关侧定义同值枚举，并用一致性
测试（`tests/llm/test_task_type_mirror.py`）断言两侧取值集合相等。

同值枚举在运行时可互换，是因为取值相同且继承 `str`：成员相等比较与哈希都落在字符串
语义上，因此 `DEFAULT_TIER_MAP[state.TaskType.TRIAGE]` 这类"用另一侧枚举做键"的查表
成立。这条性质是镜像方案可行的前提，不是巧合，故由测试固定下来。

镜像的代价是多一处需要同步的定义。用测试而不是"记得改"来保证同步，是这里唯一可靠的
做法——漏改的症状是路由查不到档位而在装配期报错，测试能让它在提交前就暴露。
"""

from enum import Enum


class TaskType(str, Enum):
    """模型网关的任务类型，每个值对应一个模型档位（技术方案 4.4）。

    成员必须与 `app/agent/state.py` 的 `TaskType` 逐一对应，取值也必须一致。
    """

    TRIAGE = "triage"
    OBSERVE = "observe"
    INTERVIEW = "interview"
    RETRIEVE = "retrieve"
    DIAGNOSE = "diagnose"
    PRESCRIBE = "prescribe"
    PANEL = "panel"
    SNAPSHOT_WRITER = "snapshot_writer"
    REPORT_GUARD = "report_guard"


__all__ = ["TaskType"]
