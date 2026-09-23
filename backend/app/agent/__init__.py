"""LangGraph 编排层入口：暴露 build_graph / get_agent。"""
import asyncio

from .graph import build_graph

__all__ = ["build_graph", "get_agent"]

_agent = None
_agent_loop: asyncio.AbstractEventLoop | None = None


async def get_agent():
    """返回编译后的诊断图单例（含 SQLite checkpointer）。

    图绑定创建时的事件循环；跨循环（如测试隔离）自动重建。
    """
    global _agent, _agent_loop
    loop = asyncio.get_running_loop()
    if _agent is None or _agent_loop is not loop:
        _agent = await build_graph()
        _agent_loop = loop
    return _agent