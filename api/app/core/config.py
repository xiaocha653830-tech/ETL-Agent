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

    # ---- 维护任务（M8，T-19）----
    # 维护扫描的触发间隔。它要扫的三类行都带状态索引，扫描本身很便宜；
    # 60 秒的粒度对「租约 300 秒」这个量级足够，短于一秒的滞后没有意义。
    maintenance_sweep_interval_seconds: int = 60
    # 单次扫描每类处理的运行数上限，避免积压时一次扫描把队列压满
    maintenance_batch_size: int = 100
    # 「超时回收」的宽限期：投递已确认不可达的运行再滞留这么久才判死。
    # 留这个窗口是为了让「运行被宣告不可能执行」这件事有一个可预期的时刻，
    # 而不是在重试耗尽的瞬间就发生——那一刻运维往往正在看这条事件。
    run_stalled_grace_seconds: int = 900

    # ---- SeaTunnel Zeta REST V2（T-15）----
    # 显式启用真实数据面；开发环境默认保持不可用，以免误把测试作业提交到集群。
    data_plane_backend: Literal["unavailable", "seatunnel"] = "unavailable"
    seatunnel_base_url: str = "http://127.0.0.1:8080"
    seatunnel_http_timeout_seconds: float = 10.0
    seatunnel_poll_interval_seconds: float = 5.0
    seatunnel_job_timeout_seconds: int = 1800

    # ---- 证据账本（M11，T-20 / FR-39 / FR-40）----
    # 签名检查点的触发条件（SPEC Q5 定案：哈希链逐条 + 签名按批）。两者取「或」：
    # 按条数锚定保证高流量下的粒度，按时间锚定保证**低频系统也一定被锚定**——
    # 只有「每 100 条」的话，一个总共只有几条记录的系统永远等不到第一个检查点，
    # 而「链上从无签名」与「链被整条重写」在事后是分不出来的。
    audit_checkpoint_every: int = 100
    audit_checkpoint_seconds: int = 300
    # 追加冲突的重试次数。PostgreSQL 上链头行锁让分叉几乎不可能发生；
    # SQLite 下 `FOR UPDATE` 被静默忽略，靠 seq 的 UNIQUE 约束与重试收敛。
    audit_append_max_attempts: int = 3

    # 校验器（SPEC §8：启动时与定期巡检）。**启动校验失败不阻断启动**：
    # 把「检测到篡改」变成一次可用性故障，等于给攻击者一个 DoS 杠杆，
    # 而且运维反而失去了通过接口查看证据的路径。
    audit_verify_on_startup: bool = True
    audit_verify_interval_seconds: int = 300
    # 分批大小；失败明细上限只是收集上限，扫描始终跑完（否则报不出准确的首个失败位置）
    audit_verify_batch_size: int = 500
    audit_verify_max_failures: int = 50

    # 链破损时是否硬阻断「授权类动作」（Commit / 回滚）。这两种动作一个签发票据、
    # 一个删数据，都必须以可信的证据链为前提；只读接口与普通写不受影响。
    audit_breach_blocks_authorization: bool = True

    # 审计报告单次导出的记录上限。超出时在报告里**显式标注截断**，绝不静默——
    # 一份看不出自己不全的报告，比没有报告更危险。
    audit_report_max_records: int = 200_000
    # 「谁批量取走了证据」本身是否入链。这是对「查询与导出不限角色」的补偿控制：
    # 分页读得到的数据却禁止导出只是安全剧场，把导出变成可追责的事实才是。
    audit_export_records_event: bool = True

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
