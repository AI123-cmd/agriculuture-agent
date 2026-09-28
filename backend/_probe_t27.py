import asyncio
import traceback
from typing import TypedDict

try:
    import app.agent  # noqa: F401
    print("import app.agent OK")
except Exception as e:
    print("import app.agent FAIL:", type(e).__name__, e)

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

print("StateGraph get_graph:", hasattr(StateGraph, "get_graph"))
print("Compiled get_graph:", hasattr(CompiledStateGraph, "get_graph"))


class S(TypedDict, total=False):
    stage: str
    panel_needed: bool


def n_observe(s):  return {"stage": "observe"}
def n_guard(s):    return {}
def n_interview(s): return {"stage": "interview"}
def n_verify(s):   return {"stage": "verify"}
def n_judge(s):    return {"stage": "judge"}
def n_prescribe(s): return {"stage": "prescribe"}


def route_guard(s):     return "interview"
def route_interview(s): return "verify"
def route_judge(s):     return "prescribe"


g = StateGraph(S)
g.add_node("observe", n_observe)
g.add_node("report_guard", n_guard)
g.add_node("interview", n_interview)
g.add_node("verify", n_verify)
g.add_node("judge", n_judge)
g.add_node("prescribe", n_prescribe)
g.add_edge(START, "observe")
g.add_edge("observe", "report_guard")
g.add_conditional_edges("report_guard", route_guard, {"observe": "observe", "interview": "interview", "handoff": "__end__"})
g.add_conditional_edges("interview", route_interview, {"interview": "interview", "verify": "verify", "handoff": "__end__"})
g.add_edge("verify", "judge")
g.add_conditional_edges("judge", route_judge, {"interview": "interview", "prescribe": "prescribe", "handoff": "__end__"})
g.add_edge("prescribe", END)
cg = g.compile()
print("subgraph nodes:", sorted(cg.get_graph().nodes.keys()))

p = StateGraph(S)
p.add_node("sub", cg)
p.add_node("handoff", n_guard)
p.add_edge(START, "sub")
p.add_conditional_edges("sub", lambda s: "handoff", {"handoff": "handoff"})
p.add_edge("handoff", END)
pc = p.compile()
print("parent nodes:", sorted(pc.get_graph().nodes.keys()))


async def run():
    out = []
    async for chunk in pc.astream({"stage": "intake"}):
        out.append([k for k in chunk.keys()])
    return out


try:
    print("parent stream:", asyncio.run(run()))
except Exception as e:
    traceback.print_exc()
