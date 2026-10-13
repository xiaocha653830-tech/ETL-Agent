"""应用配置 —— 统一从环境变量 / .env 读取（SPEC §2 技术栈）。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    app_env: str = "dev"
    app_name: str = "ETL-Agent"
    app_secret_key: str = "dev-only-change-me"
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

    # ---- 大模型（DeepSeek，OpenAI 兼容）----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
