"""烟草专家模型。"""
from ..base import CropModel
from ..registry import register


@register("烟草")
class TobaccoModel(CropModel):
    name = "烟草"
    model_id = "deepseek-v4-flash"
    system_role = (
        "你是烟草种植专家，熟悉烤烟育苗、移栽、打顶、烘烤及常见病害（如花叶病、赤星病、黑胫病）防治。"
        "请结合症状、农事管理与环境给出可执行的诊断与用药建议，"
        "信息不足时引导补充照片与关键症状，并遵守安全用药规范。"
    )