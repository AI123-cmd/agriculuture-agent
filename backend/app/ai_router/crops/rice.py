"""水稻专家模型。"""
from ..base import CropModel
from ..registry import register


@register("水稻")
class RiceModel(CropModel):
    name = "水稻"
    model_id = "deepseek-v4-flash"
    system_role = (
        "你是水稻种植专家，熟悉超级稻栽培、水肥管理、稻飞虱/稻瘟病/纹枯病等病虫害防治。"
        "请结合症状、农事管理与环境（温湿度、土壤）给出紧扣田间可执行的诊断与用药建议，"
        "信息不足时引导补充照片与关键症状，并遵守安全用药规范。"
    )