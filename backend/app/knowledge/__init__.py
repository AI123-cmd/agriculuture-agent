"""知识库包：条目状态语义与后续索引/检索能力的统一入口。

T2-47 只落地状态枚举与表结构；索引构建器（T2-48）、混合检索（T2-49）、地区回退
（T2-50）、重排序（T2-51）陆续在本包内补齐，故包初始化只导出稳定的状态常量，
避免过早冻结尚未存在的模块名。
"""

from app.knowledge.status import SEARCHABLE_STATUSES, KnowledgeStatus

__all__ = ["SEARCHABLE_STATUSES", "KnowledgeStatus"]
