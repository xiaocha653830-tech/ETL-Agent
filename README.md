# 生产级 ETL-Agent 数据集成平台

自然语言驱动 + 安全内核治理的 ETL 控制面平台。用户用自然语言描述集成需求，系统生成结构化作业方案；方案投产前经过确定性门禁、不可变版本冻结、双维度独立审批与单次授权；执行受预算监督，数据经质量分流后原子发布；全过程生成不可篡改的审计证据。

## 文档

| 文档 | 说明 |
|------|------|
| [`docs/PRD-ETL-Agent-v1.0.md`](docs/PRD-ETL-Agent-v1.0.md) | 需求规格说明书（基线） |
| [`docs/SPEC-ETL-Agent-v1.0.md`](docs/SPEC-ETL-Agent-v1.0.md) | 技术设计规格说明书（基线） |

## 目录结构

```
ETL-Agent/
├── docs/                 # PRD、SPEC 等文档
├── api/                  # 控制面后端（FastAPI）
│   ├── app/
│   │   ├── core/         # 配置、安全、统一错误码
│   │   ├── db/           # 引擎、会话、基类、跨方言类型
│   │   ├── models/       # ORM 模型（身份 / 数据源）
│   │   ├── schemas/      # 请求/响应契约
│   │   ├── api/          # 依赖与路由
│   │   ├── services/     # 凭据、数据源探查、敏感分级
│   │   ├── registry/     # M4 算子能力注册表
│   │   ├── scripts/      # 运维脚本（演示数据种子等）
│   │   └── main.py       # 应用入口
│   ├── alembic/          # 数据库迁移
│   └── tests/            # pytest（内存 SQLite，不依赖外部服务）
├── deploy/               # 数据面环境（Doris / MySQL / SeaTunnel 配置）
├── seatunnel/            # Apache SeaTunnel 2.3.13 发行包
├── prompts/              # 原始需求提示词
└── docker-compose.yml    # 控制面基础设施（PG / Redis / Vault）
```

## 快速开始

**1. 起基础设施**

```bash
docker compose up -d          # PostgreSQL / Redis / Vault
```

**2. 装依赖并建表**

```bash
cd api
cp .env.example .env          # 按需修改；.env 不入库
uv venv && uv pip install -e ".[dev]"     # 或 python -m venv .venv && pip install -e ".[dev]"
alembic upgrade head          # 迁移已随仓库提供，无需 autogenerate
```

**3. 灌演示数据（可选，但接口联调需要）**

```bash
python -m app.scripts.seed    # 幂等；创建「演示项目」与五角色账号
```

统一口令 `Demo@12345`，账号为 `maker@example.com` / `checker.data@example.com` /
`checker.security@example.com` / `operator@example.com` / `auditor@example.com`。

**4. 起服务**

```bash
uvicorn app.main:app --reload --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>

**跑测试**

```bash
cd api && ./.venv/Scripts/python.exe -m pytest      # Windows
cd api && ./.venv/bin/python -m pytest              # Linux / macOS
```

测试使用内存 SQLite 与替身数据源，**不需要** PostgreSQL / Redis / Vault，
也不会消费大模型额度（SPEC §4 的环境差异约定）。

## 当前进度

- ✅ **T-01 工程骨架**：Compose 基础设施、Alembic、认证与多租户隔离、统一错误码体系、项目与成员管理
- ✅ **T-02 凭据服务**：Vault KV v2 适配 + 本地 Fernet 降级，明文凭据不出服务端
- ✅ **T-03 数据源接入**：登记、连通性测试、只读元数据探查（API-01 / API-02）
- ✅ **T-04 敏感分级**：字段名 + 样本值双路匹配、四级定级与越级规则、样本脱敏
- ✅ **T-05 算子能力注册表**：13 个 P0 算子及属性清单与方言约束
- ✅ **T-06 需求生成**：LangGraph 状态机、EtlPlan 与配置候选生成（API-03）
- ✅ **T-07 澄清中断与恢复**：原生 interrupt + 断点续跑，不重跑已完成阶段（API-04）
- ✅ **T-08 确定性静态门禁**：配置语法 / 算子在册 / Schema 对齐三项检查
- ✅ **T-09 版本冻结与 Diff**：SHA256 指纹、不可变版本、版本间差异（API-05）
- ✅ **T-10 安全内核**：Ed25519 单次能力令牌、Tool Broker、PDP、防重放
- ⬜ T-11 ~ T-23 见 [`docs/SPEC-ETL-Agent-v1.0.md`](docs/SPEC-ETL-Agent-v1.0.md) §10

**端到端已跑通**（真实 PostgreSQL + LangGraph 断点持久化）：
登录 → 登记数据源 → 生成方案 → 落草稿版本 → 门禁冻结。当前 168 个测试全部通过。

数据面环境（Doris / MySQL / SeaTunnel）的准备见 [`deploy/README.md`](deploy/README.md)。
