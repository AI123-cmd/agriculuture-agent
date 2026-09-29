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

    # 事件发件箱（T1-13）
    # 单轮抢占条数：一次 cron 要处理完积压，又不能把批次开得太大而长时间占用
    # 连接与内存。100 条对应"每 5 秒一轮"，足够消化常规积压。
    OUTBOX_BATCH_SIZE: int = 100

    # 历史列表预览的取数策略（T1-11）
    # `lateral`：对页内每条会话按 conversation_id 索引取最后一条，不扫全表；
    #           代价与页大小成正比、与消息表大小无关，是多租户下的正确默认。
    # `window`：窗口函数一次扫描，写法更短，但子查询没有 conversation_id 过滤条件，
    #           必然全表参与分区（`Seq Scan on ai_message`），会扫到所有租户的消息。
    #           仅在单租户或消息表很小时才有优势。
    HISTORY_QUERY_STRATEGY: Literal["window", "lateral"] = "lateral"
    # 单页条数上限：前端传再大的 size 也在此截断，避免一次拉走整张会话表
    HISTORY_MAX_PAGE_SIZE: int = 50

    # 微信（真实凭证只放在 .env，勿写进代码或提交到仓库）
    WECHAT_APP_ID: str = ""
    WECHAT_APP_SECRET: str = ""

    # 农业大模型
    FARM_LLM_API_URL: str = ""
    FARM_API_KEY: str = ""
    FARM_LLM_TIMEOUT: int = 30
    USE_MOCK_LLM: bool = True

    # 模型网关装配（T2-55）。档位参数与熔断阈值一律进环境变量：开发环境用轻量模型
    # 跑通链路、生产环境换正式模型，硬编码会让"改模型"变成"改代码 + 重新发版"。
    LLM_MAX_TOKENS: int = 2048
    LLM_MAX_HISTORY_ROUNDS: int = 6
    # 预算低于此值时主模型档位降一档（T2-57 的 `min_budget_for_primary`）
    LLM_MIN_BUDGET_FOR_PRIMARY: int = 8000
    # 熔断阈值与冷却时长（T2-58）。冷却期取 30 秒：短于它供应商刚恢复就被再次打满，
    # 长于它一次偶发故障会让可用供应商长时间缺席。
    CIRCUIT_FAILURE_THRESHOLD: int = 5
    CIRCUIT_COOLDOWN_SECONDS: int = 30
    CIRCUIT_HALF_OPEN_PROBES: int = 2

    # AI 多轮诊断编排（LangGraph）
    LLM_DEFAULT_MODEL: str = "deepseek-v4-flash"
    CONFIDENCE_HIGH_THRESHOLD: int = 3
    CONFIDENCE_MEDIUM_THRESHOLD: int = 2
    MCP_ENABLED: bool = True

    # 流程配置热加载（T2-05）
    # 默认关闭：热加载是为「配置更新不必重启服务」准备的运维手段，开发与测试环境
    # 打开它反而会让「读到的到底是哪一版」变得不确定，不利于复现问题。
    PROCESS_CONFIG_HOT_RELOAD: bool = False
    # 检查间隔（秒）。取 60 是因为配置变更是低频人工操作，更短的间隔只是徒增
    # 数据库查询；而运维期望的生效延迟也在这个量级。
    PROCESS_CONFIG_CHECK_INTERVAL: int = 60

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
