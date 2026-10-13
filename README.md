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
│   │   ├── core/         # 配置、安全
│   │   ├── db/           # 引擎、会话、基类
│   │   ├── models/       # ORM 模型
│   │   ├── schemas/      # 请求/响应契约
│   │   ├── api/          # 依赖与路由
│   │   └── main.py       # 应用入口
│   └── alembic/          # 数据库迁移
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
cp .env.example .env
uv venv && uv pip install -e ".[dev]"     # 或 python -m venv .venv && pip install -e ".[dev]"
alembic revision --autogenerate -m "init identity"
alembic upgrade head
```

**3. 起服务**

```bash
uvicorn app.main:app --reload --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>

## 当前进度

- ✅ **T-01 工程骨架**：Compose 基础设施、Alembic、认证与多租户隔离
- ⬜ T-02 ~ T-23 见 `docs/SPEC-ETL-Agent-v1.0.md` §10

数据面环境（Doris / MySQL / SeaTunnel）的准备见 [`deploy/README.md`](deploy/README.md)。
