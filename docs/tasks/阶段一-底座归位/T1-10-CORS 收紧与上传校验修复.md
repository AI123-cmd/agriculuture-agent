# T1-10 CORS 收紧与上传校验修复

## 这一项要做什么

现有 `app/main.py` 的 CORS 配置是 `allow_origins=["*"]` 配 `allow_credentials=True`，这是浏览器会拒绝、代理会放行的组合，既拦不住恶意站点也说不清实际策略；上传侧 `app/integrations/storage.py` 只做 `os.path.splitext` 取扩展名，不校验内容类型、大小与文件名，农户可上传任意文件并得到一个可公开访问的 `/static/<hex>.<ext>` 地址。本项把 CORS 收敛为显式域名白名单，并为头像上传补类型、大小、魔数三重校验。

对应产品说明书 9.2 第 9 条（跨域配置宽松、头像上传缺少校验，必须收紧），并为第 4 条（图片上传链路）在第二步的改造预留正确的存储接口。

## 前置依赖

- 依赖 T1-08 已提供：`ENV` 配置项与启动自检，使 CORS 白名单可按环境区分且生产环境有校验；依赖 T1-09 已提供事务边界，上传落库失败时文件与记录不会半途分离。
- 外部环境：小程序端不在浏览器同源模型内，CORS 只影响 Web 调试页与后台管理页；生产环境需确定实际域名清单（含小程序 `servicewechat.com` 来源与后台管理域名）。本项按"从配置读白名单、生产环境为空则拒绝启动"实现，具体域名值由部署方填写。

## 参考章节

- 技术方案 第13章 13.1 接口清单（`POST /api/v1/ai/chat/upload` 上传图片返回可用地址）与 13.2 问诊接口详细设计（`imageUrl` 字段）
- 技术方案 第15章 15.1 部署拓扑（阶段一含 caddy/nginx 做 TLS 终止与反向代理，CORS 与应用层策略分工）与 15.3 配置管理（环境差异进环境变量）
- 产品说明书 9.2 第 9 条、第 4 条

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 修改 | `backend/app/main.py` | CORS 改为按 `Settings.CORS_ORIGINS` 白名单，生产环境加校验 |
| 修改 | `backend/app/core/config.py` | 新增 `CORS_ORIGINS`、`UPLOAD_MAX_BYTES`、`UPLOAD_ALLOWED_TYPES` |
| 修改 | `backend/app/integrations/storage.py` | 新增 `save_image()`，按内容哈希命名并原子落盘 |
| 修改 | `backend/app/core/startup_check.py` | 生产环境 `CORS_ORIGINS` 为空或含 `*` 时拒绝启动 |
| 修改 | `backend/app/services/user_service.py`、`app/api/v1/user.py` | 上传前校验，落库与文件写入同事务；错误映射 |
| 新增 | `backend/app/core/validators.py` | 图片魔数校验与大小校验的纯函数 |
| 新增 | `backend/tests/test_upload_validation.py`、`test_cors.py` | 类型/大小/魔数/伪造扩展名；白名单外域名被拒与预检行为 |

## 实现要点

### 一、CORS 现状为什么是错的

现状 `app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])`。问题不在"宽"，而在"自相矛盾"：按 CORS 规范，`Access-Control-Allow-Origin: *` 与 `Access-Control-Allow-Credentials: true` 不能同时生效，浏览器会拒绝带凭据的响应。这意味着当前的 `allow_credentials=True` 是无效配置——要么实际没带凭据（那么 `allow_credentials` 无意义），要么带了凭据但浏览器端被拒（那么前端会看到莫名的跨域错误）。9.2 第 9 条把它记为"宽松"，实际是"宽松且失效"。改为：

```python
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    expose_headers=["X-Request-Id"],
    max_age=600,
)
```

四处收紧的含义：第一，`allow_origins` 用白名单，空列表时 `CORSMiddleware` 不放行任何跨域来源，同源请求不受影响；第二，`allow_methods` 不再用 `*`，本项目不存在 `PATCH`，`HEAD` 由框架自动处理，显式列出可让"新增了方法但忘了加 CORS"在预检阶段暴露；第三，`allow_headers` 显式列出，`Authorization` 必需（Bearer token），`X-Request-Id` 为 T6-06 链路追踪预留；第四，`max_age=600` 让预检结果缓存 10 分钟。

### 二、Settings 中的 CORS 白名单

```python
CORS_ORIGINS: list[str] = []
UPLOAD_MAX_BYTES: int = 5 * 1024 * 1024          # 5MB
UPLOAD_ALLOWED_TYPES: set[str] = {"image/jpeg", "image/png", "image/webp"}
from pydantic import field_validator

@field_validator("CORS_ORIGINS", mode="before")
@classmethod
def _split_origins(cls, v):
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v
```

`pydantic-settings` 读取列表型环境变量时 `.env` 里用 JSON 数组，校验器额外接受逗号分隔以兼容运维习惯。生产环境的校验加进 T1-08 自检：`if settings.ENV == "production": if not settings.CORS_ORIGINS: violations.append(Check("cors", "生产环境 CORS_ORIGINS 不能为空")); if "*" in settings.CORS_ORIGINS: violations.append(Check("cors", "生产环境不允许通配来源 *"))`。

### 三、上传校验的三重防护

`app/core/validators.py`：
```python
"""上传校验纯函数：不依赖框架与存储，便于单测。"""
from dataclasses import dataclass

# 魔数表：文件头 → MIME。扩展名可伪造，魔数不可（除非刻意构造文件）
MAGIC: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"RIFF": "image/webp",          # WebP 需进一步校验偏移 8 处的 WEBP
}

@dataclass(frozen=True)
class ImageCheck:
    ok: bool
    mime: str | None
    reason: str | None

def sniff_mime(head: bytes) -> str | None:
    """按文件头判定 MIME；识别不出返回 None。"""
    for magic, mime in MAGIC.items():
        if head.startswith(magic):
            if mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return mime
    return None

def validate_image(filename: str, content_type: str | None, data: bytes,
                   *, max_bytes: int, allowed: set[str]) -> ImageCheck:
    """三重校验：大小 → 声明类型 → 真实魔数，并要求声明与真实一致。"""
    if len(data) == 0:
        return ImageCheck(False, None, "文件为空")
    if len(data) > max_bytes:
        return ImageCheck(False, None, f"文件超过上限 {max_bytes // 1024 // 1024}MB")
    real = sniff_mime(data[:16])
    if real is None:
        return ImageCheck(False, None, "文件头不是支持的图片格式")
    if real not in allowed:
        return ImageCheck(False, None, f"不支持的图片类型：{real}")
    if content_type and content_type != real:
        return ImageCheck(False, None, f"声明类型 {content_type} 与真实类型 {real} 不一致")
    return ImageCheck(True, real, None)
```
三条校验的顺序与理由。**先大小后类型。** 大小校验只需读长度，类型校验需读头部，先做便宜的检查能更早拒绝大文件攻击（避免把 50MB 恶意文件读进内存）；接口层要先按 `UploadFile.size`（Starlette 提供）或流式读取前 N 字节拒绝，再读全文。**必须比对"声明类型"与"真实魔数"。** 只查魔数不够：攻击者可上传一个 PNG 头部的可执行文件；只查 `content_type` 更不够：`Content-Type` 完全由客户端伪造；要求两者一致，可挡住"把 .exe 改名成 .jpg 并声明 image/jpeg"（其文件头仍是 `MZ`，`sniff_mime` 返回 None）。**`filename` 参数保留但只用于日志。** 现有 `LocalStorage.save` 用 `os.path.splitext(filename)[1]` 决定扩展名，这是路径与类型污染的入口（`filename="x.php"` 会得到 `.php` 后缀），改为由 `sniff_mime` 的真实结果决定扩展名。

### 四、LocalStorage 的改写

```python
import hashlib
import os

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
    def save_image(self, data: bytes, mime: str) -> str:
        """按内容哈希命名，天然去重且不复用用户文件名。"""
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:2]}/{digest}{_EXT[mime]}"
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)          # 原子落盘，避免半写文件被访问
        return f"/static/{key}"
```

三点改进：第一，按 SHA-256 内容哈希命名，同一张图重复上传只存一份（农户反复重试上传同一照片很常见），且永不出现用户可控的文件名；第二，两级目录（前 2 位哈希做子目录），单目录存几十万文件在部分文件系统上会退化，分目录是廉价保险；第三，`os.replace` 保证原子性，现有实现直接 `open(path, "wb")` 写入，若写入过程中进程被杀会留下可被访问的截断文件。现有 `save(filename, data)` 要保留为 `save_image` 的兼容包装并标记 `@deprecated`，或直接删除并同步改 `user_service`；本项选择直接删除，因为调用点只有 `user_service.upload_avatar` 一处。

### 五、上传接口的处理顺序

```python
async def upload_avatar(self, db: AsyncSession, user_id: str, file: UploadFile) -> str:
    settings = get_settings()
    data = await file.read()
    check = validate_image(file.filename or "", file.content_type, data,
                           max_bytes=settings.UPLOAD_MAX_BYTES,
                           allowed=settings.UPLOAD_ALLOWED_TYPES)
    if not check.ok:
        raise BadRequest(check.reason or "图片校验失败")
    url = get_storage().save_image(data, check.mime or "image/jpeg")
    async with unit_of_work(db, name="user.upload_avatar"):
        await self.user_repo.update_fields(db, user_id, user_id, avatar_url=url)
    return url
```

校验失败抛 400 而非 500，且不落盘：现有实现是先 `save` 再落库，校验必须插在 `save` 之前。

### 六、与验收项的对应

- 直接对应产品说明书 9.2 第 9 条（跨域宽松、上传缺少校验）。
- 为产品说明书 9.2 第 4 条（图片上传后地址恒为空）打底：本项保证地址由服务端生成且可访问；第二步 T2-33 的问诊上传链路复用同一 `save_image`，无需再修校验。
- 对应技术方案 13.1 的 `POST /api/v1/ai/chat/upload`：本项把校验抽成纯函数 `validate_image`，使该接口在 T2 阶段接入时可直接复用；并为产品说明书 6.4 内容安全红线提供间接保障（上传内容不可执行）。

## 验收标准

- `curl -H "Origin: https://evil.example" -i http://localhost:8000/api/farmer/profile` 的响应头不含 `Access-Control-Allow-Origin`。
- `curl -H "Origin: <白名单中的域名>" -i ...` 的响应头含 `Access-Control-Allow-Origin: <该域名>` 且含 `Access-Control-Allow-Credentials: true`；`curl -X OPTIONS -H "Origin: <白名单域名>" -H "Access-Control-Request-Method: POST" -i ...` 返回 200 且 `Access-Control-Allow-Methods` 为显式列表（不含 `*`）。
- 设置 `ENV=production` 且 `CORS_ORIGINS` 为空启动，进程非 0 退出且日志含 `[cors]`。
- 上传 6MB 的 `.jpg` 返回 HTTP 400 与 `code=400`，且 `ls uploads/**` 无新增文件。
- 上传改名为 `.jpg` 的文本文件（内容 `hello`）返回 400，原因为"文件头不是支持的图片格式"；上传 `content_type=image/png` 但实为 JPEG 的文件返回 400，原因为"声明类型与真实类型不一致"。
- 上传合法 PNG 后返回的 URL 形如 `/static/ab/ab12...cd.png`，`curl -s -o /dev/null -w "%{http_code}"` 返回 200；同一张图上传两次返回相同 URL，且 `uploads/` 下文件数只增加 1。
- `pytest backend/tests/test_upload_validation.py` 与 `pytest backend/tests/test_cors.py` 全部通过。
- `grep -rn 'allow_origins=\["\*"\]' backend/app` 无输出；`ruff check backend/app`、`mypy backend/app` 通过。
