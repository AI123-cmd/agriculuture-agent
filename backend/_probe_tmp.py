import asyncio
from typing import Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel


class SlotValue(BaseModel):
    key: str
    value: str | None = None
    status: Literal["filled", "unknown", "missing"] = "missing"
    source: Literal["user_answer"] = "user_answer"


class State(TypedDict, total=False):
    slots: dict[str, SlotValue]
    rounds: int


async def node_a(state: State) -> dict:
    answers = interrupt({"stage": "ask"})
    existing = state.get("slots") or {}
    print("existing type:", {k: type(v).__name__ for k, v in existing.items()})
    out = dict(existing)
    out["x"] = SlotValue(key="x", value="v", status="filled")
    return {"slots": out, "rounds": (state.get("rounds") or 0) + 1}


async def node_b(state: State) -> dict:
    slots = state.get("slots") or {}
    for k, v in slots.items():
        print("node_b slot:", k, type(v).__name__, getattr(v, "status", None))
    return {}


def build(checkpointer):
    g = StateGraph(State)
    g.add_node("a", node_a)
    g.add_node("b", node_b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    return g.compile(checkpointer=checkpointer)


async def main() -> None:
    saver = InMemorySaver()
    graph = build(saver)
    cfg = {"configurable": {"thread_id": "t1"}}
    r1 = await graph.ainvoke({"slots": {}, "rounds": 0}, config=cfg)
    print("r1 keys:", list(r1.keys()))
    print("r1 __interrupt__:", r1.get("__interrupt__"))
    snap = await graph.aget_state(cfg)
    print("next during interrupt:", snap.next)
    r2 = await graph.ainvoke(Command(resume={"answers": []}), config=cfg)
    print("r2 slots:", {k: type(v).__name__ for k, v in (r2.get("slots") or {}).items()})
    print("r2 rounds:", r2.get("rounds"))
    snap2 = await graph.aget_state(cfg)
    print("next after end:", snap2.next)

    # fresh graph object, same saver -> resume from checkpoint
    graph2 = build(saver)
    snap3 = await graph2.aget_state(cfg)
    print("fresh graph next:", snap3.next)
    print("fresh graph slots types:", {k: type(v).__name__ for k, v in (snap3.values.get("slots") or {}).items()})


asyncio.run(main())
