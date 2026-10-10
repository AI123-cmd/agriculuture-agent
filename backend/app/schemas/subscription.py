"""订阅授权接口契约（T3-11，技术方案 11.7 / 13）。

三个模型服务 `GET /subscription/quota` 与 `POST /subscription/grant`：

- `QuotaView`：额度视图 `{remaining, grantedTotal, consumedTotal, weakened}`。`weakened`
  让前端知道该显示强索要还是弱提示，判定集中在后端（`should_weaken_prompt`），避免前端各自
  实现导致策略不一致。
- `GrantRequest`：前端回传的授权结果。`granted=true` 表示农户同意订阅，`false` 表示拒绝
  （或点了"以后再说"）。
- `GrantResponse`：授权处理后的最新额度视图，使前端不需要再发一次查询。

**刻意不设 `user_id` 字段**：额度是每户的资产，`user_id` 只能从令牌取（路由层
`get_current_user`）。允许客户端在请求体或响应里指定 `user_id` 会造成额度篡改，故两个模型的
字段集合都不含它（验收标准显式断言）。

字段名用小驼峰直写（与 `app/schemas/consult.py` 的 `ChatResponse` 同一风格）：这些字段本身就是
接口契约的一部分，视图模型不再额外套一层别名转换。
"""

from pydantic import BaseModel


class QuotaView(BaseModel):
    """额度视图：剩余额度与累计计数，外加"是否降级索要"的判定结果。"""

    remaining: int
    grantedTotal: int
    consumedTotal: int
    weakened: bool


class GrantRequest(BaseModel):
    """前端回传的一次授权结果。

    `scene` 记录授权发生的场景（生成提示后 / 生成处方卡后 / 登记回访后），仅供埋点复盘；
    授权时机的选择不在本项实现，本模型只承载"这一次授权是否被同意"。
    """

    granted: bool
    scene: str | None = None


class GrantResponse(QuotaView):
    """授权处理后的最新额度视图（字段与 `QuotaView` 一致）。"""


__all__ = ["GrantRequest", "GrantResponse", "QuotaView"]
