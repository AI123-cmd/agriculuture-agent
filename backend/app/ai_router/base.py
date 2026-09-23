"""农业垂类大模型抽象基类。"""

import json

import httpx

from app.core.config import get_settings


class CropModel:
    """作物专家模型基类：统一封装 mock / 真实 LLM 调用。"""

    name: str = "通用农业"
    model_id: str | None = None
    system_role: str = "你是资深的农业种植专家，请结合农户档案用通俗中文给出可执行的农事建议。"

    async def chat(self, message: str, context: dict) -> str:
        settings = get_settings()
        if settings.USE_MOCK_LLM or not settings.FARM_LLM_API_URL:
            return self._mock_reply(message, context)
        return await self._call_real(message, context)

    def _mock_reply(self, message: str, context: dict) -> str:
        crop = (context.get("crop") or {}).get("name") or self.name
        return (
            f"关于「{message}」，结合您的{crop}档案与近期农事情况，建议如下：\n"
            "1. 先查看土壤肥力与近一周天气，排除环境诱因；\n"
            "2. 合理调整水肥，避免过量追施；\n"
            "3. 加强田间巡查，重点排查常见病虫害。\n"
            "如需更精准判断，请补充田间照片或描述植株具体症状。"
        )

    async def _call_real(self, message: str, context: dict) -> str:
        settings = get_settings()
        system = f"{self.system_role}\n农户档案：{json.dumps(context, ensure_ascii=False)}"
        model = self.model_id or settings.LLM_DEFAULT_MODEL
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {settings.FARM_API_KEY}"}

        # 超时重试，失败兜底到安全提示
        last_error: Exception | None = None
        for _ in range(2):
            try:
                async with httpx.AsyncClient(timeout=settings.FARM_LLM_TIMEOUT) as client:
                    resp = await client.post(settings.FARM_LLM_API_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                content = (
                    data.get("answer") or (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                ).strip()
                if content:
                    return content
                last_error = ValueError("模型返回空内容")
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        return self._fallback_reply(message, context, last_error)

    def _fallback_reply(self, message: str, context: dict, error: Exception | None) -> str:
        crop = (context.get("crop") or {}).get("name") or self.name
        return (
            f"很抱歉，关于「{message}」的智能诊断暂时无法获取（{type(error).__name__ if error else '服务繁忙'}）。\n"
            f"建议：先观察{crop}植株整体长势与近期天气，保持田间通风、排湿，"
            "如有突发大面积异常请及时联系当地农技站或植保部门。"
        )
