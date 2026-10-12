"""统一响应包装：{code, message, data}。"""

from typing import Any

from fastapi.responses import JSONResponse

SUCCESS = 200
BAD_REQUEST = 400
UNAUTHORIZED = 401
SERVER_ERROR = 500


def ok(data: Any = None, message: str = "success") -> dict:
    return {"code": SUCCESS, "message": message, "data": data}


def fail(code: int = BAD_REQUEST, message: str = "请求参数错误", data: Any = None) -> dict:
    return {"code": code, "message": message, "data": data}


def error_response(code: int, message: str, status_code: int | None = None, detail: Any = None) -> JSONResponse:
    """构建错误 JSONResponse（用于异常 handler）。

    `detail` 放进 `data`：非空时是结构化的定位信息（如校验失败的字段与层级），为空时保持
    `data=None`，与既有错误响应的形状一致。
    """
    body = fail(code, message, data=detail)
    return JSONResponse(status_code=status_code or code, content=body)
