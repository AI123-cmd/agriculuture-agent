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
    # 向量化服务（T2-48）。`USE_MOCK_LLM=true` 时不发起真实请求，改用确定性伪向量。
    EMBEDDING_API_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_TIMEOUT: int = 30

    # 重排序（T2-51）。默认关闭意味着走规则实现（零远程调用）：重排序在高频路径上
    # （每次证据核查都调用），cross-encoder 的远程调用会把证据核查延迟抬高一个量级。
    # 先跑通规则实现，再按评测结果决定是否启用交叉编码器。
    RERANK_ENABLED: bool = False
    RERANK_API_URL: str = ""
    RERANK_API_KEY: str = ""
    # cross-encoder 的兜底超时。用 `asyncio.wait_for` 包住主实现，避免客户端超时不覆盖
    # 连接建立阶段。
    RERANK_TIMEOUT_SECONDS: float = 3.0

    # Redis（生产为核心依赖）
    REDIS_URL: str = ""
    REDIS_REQUIRED: bool = False

    # 认证
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    TOKEN_EXPIRE_SECONDS: int = 7 * 24 * 3600

    # 知识条目核准接口的后台运营账号白名单（T2-54，技术方案 9.2 与 15.6）。
    # 核准是"人工审核后方可上架"这条红线的执行口，必须与农户侧接口分离：农户令牌
    # 验签通过也不得调用，故用显式白名单而非"登录即可"。逗号分隔的账号 id；空串 =
    # 谁都不能调用，避免"忘了配置就默认放行"（fail-closed）。
    KNOWLEDGE_REVIEW_OPERATOR_IDS: str = ""

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
    # 一次性订阅消息模板 id（T3-12）。模板在微信公众平台申请，属于环境相关配置；
    # 留空时发送链路抛 `ConfigError`（fail-closed），不会静默发一条没有模板的消息。
    WECHAT_SUBSCRIBE_TEMPLATE_ID: str = ""
    # 订阅消息点击后跳转的小程序页面（T3-12）。留空则用微信默认落地页。
    WECHAT_SUBSCRIBE_PAGE: str = "pages/index/index"

    # 农业大模型
    FARM_LLM_API_URL: str = ""
    FARM_API_KEY: str = ""
    FARM_LLM_TIMEOUT: int = 30
    USE_MOCK_LLM: bool = True

    # 供应商适配层（T2-56）。三类适配器各有端点与密钥：`openai_compat` 走原生结构化
    # 输出（主供应商），`private` 走提示词约束（私有化降档候选）。密钥只放 .env，代码
    # 里留空值；空端点即视为该供应商未装配，网关按失败处理并继续候选链。
    OPENAI_COMPAT_BASE_URL: str = ""
    OPENAI_COMPAT_API_KEY: str = ""
    PRIVATE_MODEL_BASE_URL: str = ""
    PRIVATE_MODEL_API_KEY: str = ""
    # 单次模型调用的超时（秒）。适配器按它施加 httpx 超时，覆盖连接与读取全程。
    MODEL_REQUEST_TIMEOUT: float = 30.0

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

    # 零模型模式开关（T2-58）。为真时规则层能力（阈值提示、农事历提示）继续提供，全部
    # 文案直接用模板原文，不发起任何模型调用。维护期主动关闭模型或外部服务全局中断时使用。
    ZERO_MODEL_MODE: bool = False

    # 模型单价表（T2-59）。键是模型名，值是 `[输入单价, 输出单价]`，单位"每千 token 的
    # 微元"（1e-6 元）。输入与输出分开计价，因为多数供应商的输出单价远高于输入。未配置
    # 单价的模型记录 token 但成本记 0，而不是报错：单价更新滞后于模型上线是常态。
    # 环境变量用 JSON 对象传入，如 `{"deepseek-v4-flash": [1000, 2000]}`。
    LLM_MODEL_PRICING: dict[str, list[int]] = {}

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

    # 设备接入（T5-03）
    # 设备密钥的加盐摘要盐值。明文密钥不落库，落库的是 `sha256(salt + secret)`，故盐值本身
    # 不进代码。开发环境留空仍能跑通（摘要照常计算），生产环境由 `.env` 提供。
    DEVICE_SECRET_SALT: str = ""
    # 上报地址的基址（如 `https://api.example.com`）。用配置项而非请求 Host 头推导：反向代理
    # 后 Host 头是内网地址，设备拿到后无法回连；且产品说明书 7.4 要求域名必须预先备案，
    # 这个值必须是运维确认过的已备案域名，不能由请求随意决定。
    DEVICE_REPORT_BASE_URL: str = ""

    # 对象存储
    STORAGE_TYPE: str = "local"
    STORAGE_LOCAL_DIR: str = "./uploads"

    # 质量评估
    HARNESS_PASS_RATE_THRESHOLD: float = 0.90

    # 链路追踪（T6-06，技术方案 14.4）。
    # OTEL_ENABLED 默认 False：关闭时不注册 provider，`start_as_current_span` 退化为
    # 空操作，开销可忽略；由运维按环境显式打开，避免开发环境默认把 span 打到控制台。
    OTEL_ENABLED: bool = False
    # 导出器四档：otlp（生产，指向自建 collector）、langfuse（T6-07，指向私有 Langfuse
    # 的 OTLP 接收端）、console（本地）、none（CI）。默认 none 与 OTEL_ENABLED=false
    # 一致：都不产出 span。
    OTEL_EXPORTER: Literal["otlp", "langfuse", "console", "none"] = "none"
    # OTLP collector 的 gRPC 端点（如 `http://otel-collector:4317`）。生产开启追踪时必填，
    # 留空会让追踪静默走别处，故由启动检查拦下（见 startup_check.collect_violations）。
    OTEL_ENDPOINT: str = ""
    # 资源属性 `service.name`，用于后端区分服务。
    OTEL_SERVICE_NAME: str = "huanong-agent"

    # Langfuse 私有化观测（T6-07，技术方案 14.4 与 17.2）。
    # 选私有化而非云端 SaaS 是本项目的合规底线：LLM 观测要记录提示词与模型输出，
    # 送出境外的第三方等于农事数据外流。`LANGFUSE_ENABLED=false` 时不构造任何客户端，
    # 上游零请求；为 true 但地址/密钥缺失或不可达时静默降级，观测不得成为可用性依赖。
    LANGFUSE_ENABLED: bool = False
    # Langfuse 的 web 基址（如 `http://langfuse-web:3000`）。上报端点为
    # `{host}/api/public/otel/v1/traces`，由 app/observability/langfuse.py 拼接。
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    # 抽样比。本地默认 1.0 全量；生产建议 0.2——LLM 观测数据体量远大于普通 trace，
    # 全量上报会把小内存机器上的 Langfuse 自己拖垮。抽样在根 span 创建时决定，
    # 子 span 按父决定继承（`ParentBased`），不会出现"父在子不在"的残缺 trace。
    LANGFUSE_TRACE_RATIO: float = 1.0

    # 结构化日志（T6-08，技术方案 14.4）。
    # 生产用 json（便于审计检索），非生产用 console（便于本地阅读）。
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    # 额外纳入脱敏的字段名（在默认清单之外）。逗号分隔，供环境变量覆盖。
    MASK_EXTRA_FIELDS: list[str] = []
    # 坐标（latitude/longitude）是否脱敏。`None` 表示按 `ENV` 派生（非生产不脱敏，便于本地
    # 调试看真实坐标；生产脱敏）。显式设为 false 在生产环境会被启动检查拒绝（T6-08）。
    MASK_GEO: bool | None = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """pydantic-settings 读列表型环境变量时用 JSON 数组，这里额外兼容逗号分隔。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("MASK_EXTRA_FIELDS", mode="before")
    @classmethod
    def _split_mask_fields(cls, v: object) -> object:
        """同 CORS_ORIGINS：兼容逗号分隔的环境变量写法。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("KNOWLEDGE_REVIEW_OPERATOR_IDS", mode="after")
    @classmethod
    def _normalize_operator_ids(cls, v: str) -> str:
        """规范化白名单串：去掉空白项，避免 "a, ,b" 里混入空 id。"""
        return ",".join(item.strip() for item in v.split(",") if item.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
