"""七条流程子图注册表：主图按 process_id 分发（T2-27，T2-29 补全）。

注册表是主图（T2-25）分发流程的唯一入口：`route_to_subgraph` 只按 `process_id` 查表，
未注册即抛错。每个 builder 返回**已编译**的子图——主图用 `add_node(name, builder())`
挂载子图，LangGraph 只接受 Runnable（编译后的图），未编译的 `StateGraph` 会在
`add_node` 时报类型错误。

新增流程的改动量被限定为三步：一份 YAML、一个约十五行的子图文件、本表一行。
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph.state import CompiledStateGraph

from app.agent.graph.subgraphs.chili import build_chili_subgraph
from app.agent.graph.subgraphs.general import build_general_subgraph
from app.agent.graph.subgraphs.pig import build_pig_subgraph
from app.agent.graph.subgraphs.rapeseed import build_rapeseed_subgraph
from app.agent.graph.subgraphs.rice import build_rice_subgraph
from app.agent.graph.subgraphs.tea import build_tea_subgraph
from app.agent.graph.subgraphs.weed import build_weed_subgraph
from app.agent.state import ProcessId

SUBGRAPH_BUILDERS: dict[ProcessId, Callable[[], CompiledStateGraph]] = {
    ProcessId.RICE: build_rice_subgraph,
    ProcessId.TEA: build_tea_subgraph,
    ProcessId.CHILI: build_chili_subgraph,
    ProcessId.WEED: build_weed_subgraph,
    ProcessId.PIG: build_pig_subgraph,
    ProcessId.RAPESEED: build_rapeseed_subgraph,
    ProcessId.GENERAL: build_general_subgraph,
}

__all__ = ["SUBGRAPH_BUILDERS"]
