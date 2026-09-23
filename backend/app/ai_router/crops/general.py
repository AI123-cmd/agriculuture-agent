"""通用农业模型（兜底）。"""

from ..base import CropModel
from ..registry import register


@register("通用农业")
class GeneralModel(CropModel):
    name = "通用农业"
    model_id = None  # 兜底走 settings.LLM_DEFAULT_MODEL
    system_role = (
        "你是全面的农业种植专家，覆盖大田作物、经济作物与果蔬种植。"
        "请根据农户提供的症状、农事管理与环境数据，按『初步判断 → 农事措施 → 用药建议 → 提醒』的结构给出可执行诊断，"
        "信息不足时先引导补充关键证据，用药需提示安全规范与免责。"
    )
