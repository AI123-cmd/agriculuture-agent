"""Langfuse 私有化观测导出器（T6-07，技术方案 14.4 与 17.2）。

为什么私有化：LLM 观测天然要记录提示词与模型输出，用云端 SaaS 等于把农事数据送到境外
第三方，与"境内云 API 为主 + 私有化兜底"（17.2）直接冲突。本模块把 span 上报到本项目
自建的 Langfuse。

### 走 OTLP 而不是官方 Python SDK

Langfuse 原生支持 OTLP 接入（`{host}/api/public/otel/v1/traces`，Basic Auth 用
public/secret key 拼 base64）。选 OTLP 而非 `pip install langfuse`：后者会引入
`openai`/`httpx2` 等一串与本项目无关的运行时依赖，而本项目已为 T6-06 装了
`opentelemetry-exporter-otlp`。同一份 span 既能进自建 collector 又能进 Langfuse，
接入面更小。

### 弱依赖

Langfuse 停掉，业务照常跑，只是没有观测数据。因此：客户端构造失败（地址非法、密钥缺失）
一律返回不可用而不抛错；导出失败由批量导出器丢弃并记账。观测是旁路，不得成为可用性依赖。
"""

from __future__ import annotations

import base64
from typing import Any

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

# OTLP 接收端点路径。Langfuse 从 v3 起固定为 `/api/public/otel/v1/traces`。
OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
# 上报协议版本头。Langfuse 要求显式声明，缺失会被拒收。
INGESTION_VERSION_HEADER = "x-langfuse-ingestion-version"
INGESTION_VERSION = "4"

_MAX_RATIO = 1.0
_MIN_RATIO = 0.0


class LangfuseExporter(SpanExporter):
    """把 span 经 OTLP/HTTP 上报到私有 Langfuse。

    `trace_ratio()` 供 `configure_tracing` 装配抽样器；抽样比本身不在这里决定，因为它要
    作用于**根 span 的创建时刻**，而导出器只在 span 结束时被调用。
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        # 构造 OTLP 导出器不建连接，地址不可达只在导出时失败并被丢弃。
        self._exporter = _build_otlp_exporter(settings)

    def trace_ratio(self) -> float:
        """抽样比，越界值夹紧到 `[0, 1]`。

        配置写出 `2.0` 或 `-1` 是明显的笔误，静默夹紧比按原值送进抽样器要好：后者会
        让"全量上报"变成不可解释的行为（`TraceIdRatioBased(2.0)` 恒真但读起来像 bug）。
        """
        raw = getattr(self._settings, "LANGFUSE_TRACE_RATIO", _MAX_RATIO)
        try:
            ratio = float(raw)
        except (TypeError, ValueError):
            return _MAX_RATIO
        return max(_MIN_RATIO, min(_MAX_RATIO, ratio))

    def export(self, spans: Any) -> SpanExportResult:
        """导出失败不得影响问诊：任何异常都转成 `FAILURE`，由批量导出器丢弃。"""
        try:
            return self._exporter.export(spans)
        except Exception:  # noqa: BLE001  观测是旁路，导出失败不改变业务语义
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._exporter.shutdown()
        except Exception:  # noqa: BLE001  同上：收尾失败不阻断进程退出
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """把 Langfuse 内层导出器的缓冲刷出。供 lifespan 收尾与测试断言前调用。"""
        try:
            return bool(self._exporter.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001  同上
            return False


def build_langfuse_exporter(settings: Any) -> LangfuseExporter | None:
    """按配置构造导出器；不可用时返回 `None`（调用方据此退化为空操作 provider）。

    `LANGFUSE_ENABLED=false` 时直接返回 `None`：这是"上游零请求"的唯一保证。关掉开关
    却仍构造客户端，等于把"关闭观测"做成"照常上报只是没人看"，与合规意图相反。

    不抛错是刻意的：地址写错或密钥缺失属于配置问题，但"观测配错"不该让服务起不来——
    这与 T6-06 的 OTel 导出器同一原则。生产环境的"开了却没配地址"由启动检查拦下。
    """
    if not getattr(settings, "LANGFUSE_ENABLED", False):
        return None
    if not getattr(settings, "LANGFUSE_HOST", ""):
        return None
    try:
        return LangfuseExporter(settings)
    except Exception:  # noqa: BLE001  非法地址/密钥在此降级为"无观测"
        return None


def _build_otlp_exporter(settings: Any) -> Any:
    """构造指向 Langfuse 的 OTLP/HTTP 导出器。

    Langfuse 用 Basic Auth 鉴权，凭据是 `public_key:secret_key` 的 base64；用
    `opentelemetry-exporter-otlp-proto-http` 是因为 Langfuse 的接收端是 HTTP 而非 gRPC。
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    host = str(settings.LANGFUSE_HOST).rstrip("/")
    public_key = str(getattr(settings, "LANGFUSE_PUBLIC_KEY", "") or "")
    secret_key = str(getattr(settings, "LANGFUSE_SECRET_KEY", "") or "")
    if not public_key or not secret_key:
        # 无凭据的上报必然 401，与其每批都失败一次，不如直接判定不可用。
        raise ValueError("Langfuse 需要 LANGFUSE_PUBLIC_KEY 与 LANGFUSE_SECRET_KEY")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return OTLPSpanExporter(
        endpoint=f"{host}{OTLP_TRACES_PATH}",
        headers={
            "Authorization": f"Basic {token}",
            INGESTION_VERSION_HEADER: INGESTION_VERSION,
        },
    )


__all__ = ["INGESTION_VERSION", "OTLP_TRACES_PATH", "LangfuseExporter", "build_langfuse_exporter"]
