import asyncio
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class S(TypedDict, total=False):
    stage: str
    gate_result: dict


async def gatenode(state):
    decision = interrupt({"kind": "gate_confirm", "decision": "degrade", "options": ["continue", "abort"]})
    return {"stage": "handoff", "gate_result": {"decision": "degrade", "confirmed": decision == "continue"}}


def gate_sub():
    g = StateGraph(S)
    g.add_node("gate_check", gatenode)
    g.add_edge(START, "gate_check")
    g.add_edge("gate_check", END)
    return g.compile()


async def main():
    parent = StateGraph(S)
    parent.add_node("gate", gate_sub())
    parent.add_edge(START, "gate")
    parent.add_edge("gate", END)
    app = parent.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "g1"}}
    r = await app.ainvoke({"stage": "prescribe"}, cfg)
    print("FIRST", r)
    snap = await app.aget_state(cfg)
    print("NEXT", snap.next)
    r2 = await app.ainvoke(Command(resume="continue"), cfg)
    print("SECOND", r2)
    snap2 = await app.aget_state(cfg)
    print("NEXT2", snap2.next)


asyncio.run(main())
