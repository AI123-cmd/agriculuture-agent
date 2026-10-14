"""图片识别端口的模型实现（T2-15 的 `ImageReader` 生产实现）。

链路：读图字节 → 多模态调用（VISION 档）→ 解析为候选 → 候选域二次过滤。放在
`app/integrations/` 而不是 `app/agent/`：它要读文件、要发网络请求，属 IO；智能体层
保持"只喂假数据就能单测"的性质（守 L2）。网关侧不改（守 L3），不碰 ORM（守 L4）。

三条刻意的设计：

- **只用图片的原生键，不拼绝对路径**：`imageUrl` 是外部输入。只接受 `/static/` 前缀，
  其余一律拒绝——否则 `http://…` 是 SSRF 入口、`../` 是目录穿越入口。
- **失败不抛异常，返回空候选**：现场识别不出来只是"这轮没有图片事实"，不该中断问诊。
  观察节点虽有 `return_exceptions=True` 兜底，这里再给一层更干净的出口。
- **候选域二次过滤**：模型输出后仍按 `allowed_labels` 过滤。智能体层还有一次同类过滤，
  两处都保留是刻意的双保险——不依赖图像模型自律。
"""

import base64
import json

from pydantic import BaseModel, ConfigDict

from app.agent.contracts import ImageCandidate
from app.core.validators import sniff_mime
from app.integrations.storage import Storage
from app.llm import AllProvidersFailedError, ModelGateway, TaskType
from app.observability import log

# 只接受站内静态资源路径。外部 URL 一律拒绝：模型端点若回连本服务，`http://…` 就成了
# SSRF 入口；`..` 则是目录穿越入口，两者都在 `_load` 里挡掉。
_STATIC_PREFIX = "/static/"

# 单次识别的图片上限。`ChatRequest.imageUrl` 当前是单张（`str | None`），留一个上限是为了
# 防止将来把多图塞进同一批调用时无界膨胀 token。
_MAX_IMAGES = 3


class _CandidateOut(BaseModel):
    """模型输出的一条候选。内部专用契约，不外泄到智能体层（那边有 `ImageCandidate`）。"""

    model_config = ConfigDict(extra="forbid")

    label: str
    score: float
    part: str | None = None


class _CandidatesOut(BaseModel):
    """模型输出的结构化外壳。`extra="forbid"` 让自创字段在解析阶段即被拒。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[_CandidateOut] = []


class ModelImageReader:
    """图片识别端口的生产实现：取图 → 多模态调用 → 解析为候选。"""

    def __init__(self, gateway: ModelGateway, storage: Storage) -> None:
        self._gateway = gateway
        self._storage = storage

    async def recognize(self, image_urls: list[str], *, allowed_labels: set[str]) -> list[ImageCandidate]:
        """识别图片并返回候选。

        `allowed_labels` 既是提示词里的候选闭集，也是输出后的过滤条件。识别失败（无图、
        网关全链路失败、解析不出）一律返回空列表，不抛异常。
        """
        images = [data for data in (self._load(url) for url in image_urls[:_MAX_IMAGES]) if data is not None]
        if not images:
            return []
        try:
            response = await self._gateway.chat(TaskType.OBSERVE, self._build_messages(images, allowed_labels), _CandidatesOut)
        except AllProvidersFailedError as exc:
            # 端点不是多模态、超时、配额耗尽都落在这里；对农户而言都是"这次照片没认出来"。
            log.warning("image recognition failed", reason=str(exc))
            return []
        parsed = response.parsed
        if not isinstance(parsed, _CandidatesOut):
            return []
        return [
            ImageCandidate(label=item.label, score=item.score, part=item.part)
            for item in parsed.candidates
            if item.label in allowed_labels  # 二次过滤：与智能体层同源，双保险
        ]

    # ---- 取图 ----

    def _load(self, url: str) -> bytes | None:
        """把 `imageUrl` 解析成图片字节；非法路径或读不到一律返回 `None`。"""
        if not url.startswith(_STATIC_PREFIX):
            log.warning("image url rejected", url=url)
            return None
        key = url[len(_STATIC_PREFIX) :]
        if not key or key.startswith("/") or ".." in key:
            log.warning("image key rejected", key=key)
            return None
        return self._storage.read_image(key)

    # ---- 提示词 ----

    def _build_messages(self, images: list[bytes], allowed_labels: set[str]) -> list[dict]:
        """拼多模态 messages：候选闭集与硬规则在 system，图片在 user。

        候选域直接用调用方注入的 `allowed_labels`（由流程配置汇总），不另起一份白名单——
        两份白名单迟早会分叉，而代码侧过滤只认这里注入的这份。
        """
        labels = "、".join(sorted(allowed_labels))
        system = [
            "你是农作物现场观察模块，负责看照片并给出候选判断，供农户参考。",
            "硬规则：",
            f"1. 只允许从以下标签中选择，不得自创新标签：{labels}",
            "2. 每个候选给出 0~1 的置信度分数；看不清或没把握就返回空列表，不要猜。",
            "3. 只描述看到的形态（部位、颜色、斑点等），不要下诊断结论，不要给药。",
            "你必须输出符合以下 JSON Schema 的内容：",
            json.dumps(_CandidatesOut.model_json_schema(), ensure_ascii=False),
        ]
        content: list[dict] = [{"type": "text", "text": "请看这张照片，按候选标签给出判断。"}]
        for data in images:
            content.append({"type": "image_url", "image_url": {"url": _data_url(data)}})
        return [
            {"role": "system", "content": "\n".join(system)},
            {"role": "user", "content": content},
        ]


def _data_url(data: bytes) -> str:
    """把图片字节编码成 base64 data URL。

    MIME 由文件头判定（`sniff_mime`）而不是由调用方声明：落盘时已按真实类型命名，这里
    再判一次是为了拿到与字节严格一致的 MIME；判不出来按 jpeg 处理（上传校验已保证只可能是
    jpeg/png/webp 三者之一）。
    """
    mime = sniff_mime(data[:16]) or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


__all__ = ["ModelImageReader"]
