"""配置中心：读取 .env，暴露全局环境变量。"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。

    环境差异一律进环境变量（技术方案 15.3），全部阈值型配置显式命名。
    """

    # 服务
    APP_NAME: str = "话农点金"
    VERSION: str = "2.0.0"
    # ENV 用 Literal 而非 str：枚举封闭，启动时拒绝 prod 这类拼写
    ENV: Literal["development", "staging", "production"] = "development"
    # DEBUG 默认 False：忘记配置的生产环境不应暴露接口文档（fail-closed）
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 数据库（PostgreSQL 统一栈）
    # DATABASE_URL 给 SQLAlchemy 异步引擎，driver 必须是 asyncpg
    DATABASE_URL: str = "postgresql+asyncpg://huanong:huanong@localhost:5432/huanong"
    # CHECKPOINT_DB_URL 给 psycopg 连接池（LangGraph 检查点），driver 为裸 postgresql
    CHECKPOINT_DB_URL: str = "postgresql://huanong:huanong@localhost:5432/huanong"
    CHECKPOINT_SCHEMA: str = "checkpoint"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    CHECKPOINT_POOL_MAX: int = 10
    # 知识条目向量维度，必须与 embedding 模型输出维度一致（T2-56 决定）
    EMBEDDING_DIM: int = 1024

    # Redis（生产为核心依赖）
    REDIS_URL: str = ""
    REDIS_REQUIRED: bool = False

    # 认证
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    TOKEN_EXPIRE_SECONDS: int = 7 * 24 * 3600

    # 安全闸门
    GATE_MODE: Literal["shadow", "enforce"] = "shadow"

    # CORS
    CORS_ORIGINS: list[str] = []
    UPLOAD_MAX_BYTES: int = 5 * 1024 * 1024
    UPLOAD_ALLOWED_TYPES: set[str] = {"image/jpeg", "image/png", "image/webp"}

    # 微信（真实凭证只放在 .env，勿写进代码或提交到仓库）
    WECHAT_APP_ID: str = ""
    WECHAT_APP_SECRET: str = ""

    # 农业大模型
    FARM_LLM_API_URL: str = ""
    FARM_API_KEY: str = ""
    FARM_LLM_TIMEOUT: int = 30
    USE_MOCK_LLM: bool = True

    # AI 多轮诊断编排（LangGraph）
    LLM_DEFAULT_MODEL: str = "deepseek-v4-flash"
    CONFIDENCE_HIGH_THRESHOLD: int = 3
    CONFIDENCE_MEDIUM_THRESHOLD: int = 2
    MAX_COLLECT_ROUNDS: int = 3
    MCP_ENABLED: bool = True

    # 地图 / 天气
    GEOCODING_API_URL: str = ""
    GEOCODING_API_KEY: str = ""
    WEATHER_API_URL: str = ""
    WEATHER_API_KEY: str = ""

    # 农事播报规则阈值
    BROADCAST_TEMP_HIGH: float = 35.0
    BROADCAST_TEMP_LOW: float = 5.0
    BROADCAST_SOIL_DRY: float = 20.0
    BROADCAST_HUMIDITY_HIGH: float = 85.0
    BROADCAST_RAINFALL_MM: float = 25.0
    BROADCAST_WIND_MS: float = 8.0

    # 对象存储
    STORAGE_TYPE: str = "local"
    STORAGE_LOCAL_DIR: str = "./uploads"

    # 质量评估
    HARNESS_PASS_RATE_THRESHOLD: float = 0.90

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """pydantic-settings 读列表型环境变量时用 JSON 数组，这里额外兼容逗号分隔。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
