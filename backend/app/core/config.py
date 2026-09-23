"""配置中心：读取 .env，暴露全局环境变量。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。"""

    # 服务
    APP_NAME: str = "话农点金"
    VERSION: str = "1.1.0"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # 数据库
    DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/huanong"

    # Redis（可选）
    REDIS_URL: str = ""
    TOKEN_EXPIRE_SECONDS: int = 7 * 24 * 3600

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
    CHECKPOINT_DB_URL: str = "sqlite:///./agent_checkpoints.db"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()