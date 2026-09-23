"""工具目录：汇总 MCP tools → LangGraph tool 列表。

生产使用 langchain-mcp-adapters 将独立 MCP Server 转成 LangGraph tool；
此处以 langchain_core.tools.StructuredTool 统一封装，便于后续绑定到模型。
运行期取证由 fetch_environment 节点直接调用 tools/*.py 的函数完成。
"""

from langchain_core.tools import StructuredTool

from app.tools import knowledge, profile, sensors, weather


def build_tools() -> list:
    """返回可绑定到大模型的工具列表（JSON 友好签名）。"""

    async def sensors_tool(user_id: str) -> dict:
        return await sensors.query_sensors(None, user_id)

    async def profile_tool(user_id: str) -> dict:
        return await profile.query_profile(None, user_id)

    return [
        StructuredTool.from_function(
            coroutine=sensors_tool,
            name="query_sensors",
            description="读取地块空气/土壤温湿度、光照、pH/EC 传感器快照",
        ),
        StructuredTool.from_function(
            func=lambda location: weather.query_weather(location),
            name="query_weather",
            description="查询指定地点的当前/预报天气",
        ),
        StructuredTool.from_function(
            coroutine=profile_tool,
            name="query_profile",
            description="检索农户档案、作物与画像标签",
        ),
        StructuredTool.from_function(
            func=knowledge.search_knowledge,
            name="search_knowledge",
            description="检索作物病虫害/施肥知识",
        ),
    ]
