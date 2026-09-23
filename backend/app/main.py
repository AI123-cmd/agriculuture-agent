"""FastAPI 应用入口：装配配置、CORS、异常处理器、路由与静态资源。"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agent import get_agent
from app.agent.checkpointer import close_checkpointer
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.response import error_response
from app.db.session import engine

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 预热：初始化 checkpointer 与诊断图单例
    await get_agent()
    yield
    # 收尾：先关检查点连接池，再释放业务引擎
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return error_response(exc.code, exc.message, status_code=exc.code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未捕获异常：%s", exc)
    return error_response(500, "服务器内部错误", status_code=500)


app.include_router(api_router, prefix="/api")


# 本地对象存储：挂载上传目录以提供头像等静态资源
if settings.STORAGE_TYPE == "local":
    static_dir = os.path.abspath(settings.STORAGE_LOCAL_DIR)
    os.makedirs(static_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
