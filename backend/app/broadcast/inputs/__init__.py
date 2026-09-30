"""三类输入的采集入口。

本包把三类输入（环境事实、档案与农事历、问诊与回访）各自隔离成一个模块，
使 `snapshot.py` 的编排层只依赖"采集函数"这一层抽象，不关心取数细节。
"""

from app.broadcast.inputs.env_facts import EnvFacts, collect_env_facts, is_env_risk_blocked

__all__ = ["EnvFacts", "collect_env_facts", "is_env_risk_blocked"]
