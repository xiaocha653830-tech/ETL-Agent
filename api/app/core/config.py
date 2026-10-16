"""应用配置 —— 统一从环境变量 / .env 读取（SPEC §2 技术栈）。"""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    app_env: str = "dev"
    app_name: str = "ETL-Agent"
    # 会话签名密钥：HS256 要求密钥 ≥ 32 字节（RFC 7518 §3.2）。
    # 此处仅为开发占位值，生产环境必须替换（见下方 _validate_secret_key）。
    app_secret_key: str = "dev-only-change-me-please-32bytes-min"
    access_token_expire_minutes: int = 60

    # ---- PostgreSQL ----
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "etl"
    postgres_password: str = "etl_dev_pwd"
    postgres_db: str = "etl_agent"

    # ---- Redis ----
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0

    # ---- Vault（凭据受管，FR-03）----
    vault_addr: str = "http://127.0.0.1:8200"
    vault_token: str = "dev-root-token"
    vault_mount_point: str = "secret"

    # ---- 凭据服务（SPEC §3 M13）----
    # local：Fernet 加密落盘，开发/演示环境默认（SPEC §4：dev 不依赖 Vault）
    # vault：Vault KV v2，生产环境使用
    secret_backend: Literal["local", "vault"] = "local"
    local_secret_file: str = ".secrets.enc"

    # ---- 大模型（DeepSeek，OpenAI 兼容）----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---- 模型后端选择（SPEC §4：开发环境不消费真实额度）----
    # fake：确定性替身，开发 / 单元测试默认
    # deepseek：真实调用，演示 / 生产环境使用
    llm_backend: Literal["fake", "deepseek"] = "fake"
    llm_timeout_seconds: int = 60

    # ---- 审批与执行预算（M6 / M10，FR-22 / FR-35）----
    # 审批时限：准备单生成即开始计时，超时后审批不再有效（SPEC §5「expires_at 非空」）
    approval_ttl_hours: int = 24
    # 准备单声明的默认执行预算。P0 场景由数据工程师声明的质量契约决定，
    # 这组值是可配置的兜底；T-17 的运行时监督以此为超预算判据。
    budget_max_rows: int = 5_000_000
    budget_max_bytes: int = 2_147_483_648  # 2 GiB
    budget_max_duration_seconds: int = 1800  # 30 min
    budget_max_reject_ratio: float = 0.2
    # 放大比 = 写入字节 / 读取字节（FR-35）。兜底默认取 3.0：多出来的两倍余量
    # 留给 explode 一类合法的行扩张算子。真正危险的是「声明里没有的连接」——
    # 误写的笛卡尔积会把放大比推到几十上百倍，这才是硬中断要拦的东西。
    budget_max_amplification_ratio: float = 3.0

    # ---- 异步执行层（M8，FR-28 / FR-29）----
    # Outbox 投递失败后的指数退避基数与上限（秒）
    outbox_backoff_base_seconds: int = 5
    outbox_backoff_max_seconds: int = 300
    # 单次分发扫描的事件条数上限，避免一次扫描把队列压满
    outbox_dispatch_batch_size: int = 50
    # 执行租约时长与心跳间隔（FR-29「Worker 心跳失败不伪造成功」）
    run_lease_seconds: int = 300
    run_heartbeat_seconds: int = 30
    # Outbox 分发任务的触发间隔（Celery beat）
    outbox_dispatch_interval_seconds: int = 15

    # ---- SeaTunnel Zeta REST V2（T-15）----
    # 显式启用真实数据面；开发环境默认保持不可用，以免误把测试作业提交到集群。
    data_plane_backend: Literal["unavailable", "seatunnel"] = "unavailable"
    seatunnel_base_url: str = "http://127.0.0.1:8080"
    seatunnel_http_timeout_seconds: float = 10.0
    seatunnel_poll_interval_seconds: float = 5.0
    seatunnel_job_timeout_seconds: int = 1800

    @model_validator(mode="after")
    def _validate_secret_key(self) -> "Settings":
        """非开发环境必须使用足够强的会话签名密钥。

        开发环境允许使用占位密钥（便于零配置启动），其余环境一律拒绝弱密钥，
        避免把 dev 值带上生产。
        """
        if self.app_env != "dev" and len(self.app_secret_key.encode("utf-8")) < 32:
            raise ValueError("APP_SECRET_KEY 长度不足 32 字节；生产环境必须替换为随机强密钥")
        return self

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def langgraph_dsn(self) -> str:
        """LangGraph 的 PostgresSaver 走裸 psycopg，不认 SQLAlchemy 的方言前缀。"""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
