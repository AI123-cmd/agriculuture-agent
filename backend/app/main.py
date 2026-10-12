"""FastAPI 应用入口：装配配置、CORS、异常处理器、路由与静态资源。"""

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

# psycopg 的异步连接池不支持 Windows 默认的 ProactorEventLoop，会直接抛
# "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"。
# 检查点器依赖 psycopg，故必须在事件循环创建之前切到 SelectorEventLoop。
# uvicorn 在 asyncio.run() 之前导入本模块，因此这里设置是生效的。
# 生产按全局约定跑 Linux 容器，该分支不会执行。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent import get_agent
from app.agent.checkpointer import close_checkpointer
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.response import error_response
from app.core.startup_check import run_startup_checks, verify_checkpoint_schema, verify_schema_revision
from app.db.session import engine
from app.observability import (
    bind_context,
    clear_context,
    configure_logging,
    configure_tracing,
    force_flush,
    log,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 日志最早配置：之后的所有启动步骤（自检、迁移校验）都要走统一脱敏入口。
    configure_logging(settings)
    # 配置自检必须排在最前：配置不安全时不应先建连接再退出
    await run_startup_checks()
    # 追踪器早于图构建装配：图节点的 span 包装在导入期已绑定 `_tracer()`，provider 需在
    # 首次执行前就位，否则本轮 trace 会落到空操作 provider 上。
    configure_tracing(settings)
    # 迁移版本校验必须在预热之前：结构漂移时不应先建连接再退出
    await verify_schema_revision(engine)
    # 预热：初始化 checkpointer 与诊断图单例（顺带由 setup() 建出检查点表）
    await get_agent()
    # 检查点表由 setup() 自建，故数量断言必须排在预热之后
    await verify_checkpoint_schema(engine)
    yield
    # 收尾：把批量导出器缓冲里的 span 刷出，再关检查点连接池与业务引擎
    force_flush()
    await close_checkpointer()
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # 白名单而非 `*`：`Access-Control-Allow-Origin: *` 与 `allow_credentials=True`
    # 按 CORS 规范不能同时生效，浏览器会拒绝带凭据的响应——原来的组合是"宽松且失效"。
    # 空列表时中间件不放行任何跨域来源，同源请求不受影响。
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    # 不用 `*`：本项目没有 PATCH，HEAD 由框架处理；显式列出能让"新增了方法却忘了加
    # CORS"在预检阶段暴露，而不是等到线上才发现。
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id", "X-Device-Secret"],
    expose_headers=["X-Request-Id"],
    max_age=600,
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """给每个请求绑定 `request_id` 并回写响应头，请求结束清理上下文。

    `clear_context` 必须在 `finally` 里：`contextvars` 在 asyncio 任务间继承，不清理会让
    下一个请求的日志带上本请求的 `request_id`——串号是这类实现的经典缺陷。问诊链路再叠加
    `consultation_id`（由 `AIService` 绑定），使"某次问诊的全部日志"可一次取齐。
    """
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    bind_context(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        clear_context()
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return error_response(exc.code, exc.message, status_code=exc.code, detail=exc.detail)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("未捕获异常", error=str(exc))
    return error_response(500, "服务器内部错误", status_code=500)


app.include_router(api_router, prefix="/api")


# 本地对象存储：挂载上传目录以提供头像等静态资源
if settings.STORAGE_TYPE == "local":
    static_dir = os.path.abspath(settings.STORAGE_LOCAL_DIR)
    os.makedirs(static_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
