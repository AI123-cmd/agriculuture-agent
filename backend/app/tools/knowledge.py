"""作物知识库检索工具（MCP）：检索病虫害/施肥知识。"""


def search_knowledge(query: str) -> dict:
    """检索作物知识库；知识库未接入时返回内置常见防治要点兜底。"""
    # 未来接入向量知识库，此处为规则兜底，保证工具链路可跑通
    base = {
        "query": query,
        "source": "knowledge_base",
    }
    results = [
        "常见病害多与高温高湿环境相关，注意田间通风排湿。",
        "施肥应测土配方，避免单一过量偏施氮肥。",
        "用药坚持轮换、交替原则，单季同类药剂不超过 2 次。",
    ]
    return {**base, "results": results}
