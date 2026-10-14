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
