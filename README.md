# 生产级 ETL-Agent 数据集成平台

自然语言驱动 + 安全内核治理的 ETL 控制面平台。用户用自然语言描述集成需求，系统生成结构化作业方案；方案投产前经过确定性门禁、不可变版本冻结、双维度独立审批与单次授权；执行受预算监督，数据经质量分流后原子发布；全过程生成不可篡改的审计证据。

## 文档

| 文档 | 说明 |
|------|------|
| [`docs/PRD-ETL-Agent-v1.0.md`](docs/PRD-ETL-Agent-v1.0.md) | 需求规格说明书（基线） |
| [`docs/SPEC-ETL-Agent-v1.0.md`](docs/SPEC-ETL-Agent-v1.0.md) | 技术设计规格说明书（基线） |
| [`docs/DEV-GUIDE-ETL-Agent-v1.0.md`](docs/DEV-GUIDE-ETL-Agent-v1.0.md) | **开发与运维手册**：环境总览与端口、启动步骤、故障排查表、常用命令 |

## 目录结构

```
ETL-Agent/
├── docs/                 # PRD、SPEC 等文档
├── api/                  # 控制面后端（FastAPI）
│   ├── app/
│   │   ├── core/         # 配置、安全、统一错误码
│   │   ├── db/           # 引擎、会话、基类、跨方言类型
│   │   ├── models/       # ORM 模型（身份 / 数据源 / 版本 / 审批 / 执行 / 质量报告 / 审计账本）
│   │   ├── schemas/      # 请求/响应契约
│   │   ├── api/          # 依赖与路由
│   │   ├── services/     # 凭据、数据源探查、敏感分级
│   │   ├── registry/     # M4 算子能力注册表
│   │   ├── scripts/      # 运维脚本（演示种子、受管链路演示、预检、冒烟）
│   │   └── main.py       # 应用入口
│   ├── alembic/          # 数据库迁移
│   └── tests/            # pytest（内存 SQLite，不依赖外部服务）
├── deploy/               # 数据面环境（Doris / MySQL / SeaTunnel 配置）
├── seatunnel/            # Apache SeaTunnel 2.3.13 发行包
├── prompts/              # 原始需求提示词
└── docker-compose.yml    # 控制面基础设施（PG / Redis / Vault）
```

## 业务流程

```
M13 凭据服务
   │
M2 数据源与探查 ──► M3 需求生成 ──► M5 门禁与版本
                         │                  │
                         └── M4 算子注册表   │
                                            ▼
                                  M6 Prepare/审批/Commit
                                            │
                                            ▼
                                  M7 安全内核与能力令牌
                                            │
                                            ▼
                                  M8 调度 Worker/Outbox
                                            │
                                            ▼
                                  M9 SeaTunnel 数据面
                                            │
                                            ▼
                                  M10 质量/预算/发布/回滚
                                            │
                                            ▼
                                  M11 证据账本与审计
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

**5. 起异步执行层（T-13 起：Outbox 分发与任务认领）**

```bash
cd api
celery -A app.worker.celery_app:celery_app worker -l info
celery -A app.worker.celery_app:celery_app beat -l info   # 必须单实例，多实例会重复投递
```

Worker 与 API **必须连同一个 PostgreSQL**——否则 API 写下的 Outbox 事件
Worker 看不见（SPEC §4 部署约束）。Beat 按 `OUTBOX_DISPATCH_INTERVAL_SECONDS`
（默认 15 秒）扫描待投递事件。

**跑测试**

```bash
cd api && ./.venv/Scripts/python.exe -m pytest      # Windows
cd api && ./.venv/bin/python -m pytest              # Linux / macOS
```

测试使用内存 SQLite 与替身数据源，**不需要** PostgreSQL / Redis / Vault，
也不会消费大模型额度（SPEC §4 的环境差异约定）。

## 当前进度（截至 2026-10-16）

当前处于**控制面基础完成、数据面与质量发布收口、预算监督开发中**的阶段。
以下包含本地工作区尚未提交的实现，表示代码与验证进度，不等同于完整产品验收通过。

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
- ✅ **T-11 准备单与双维审批**：确定性风险评级、Prepare 准备单（零副作用）、数据/安全双维独立审批、职责分离硬拦截
- ✅ **T-12 Commit 收口**：审批与指纹双重复核 → 签发能力令牌 → 同事务落运行事实与 Outbox
- ✅ **T-13 Outbox 分发与任务认领**：Celery 分发任务、原子认领、运行事件时间线、快照脱敏
- ✅ **T-14 租约 fencing 与取消**：心跳续期、终态写入的租约守卫、协作式取消、幂等台账
- 🟨 **T-15 SeaTunnel REST 适配**：提交、状态/度量轮询、停止、运行时参数注入与影子表约束已实现；真实 MySQL → SeaTunnel → Doris 冒烟已验证，完整受管链路仍待稳定复验
- ✅ **T-16 质量与发布**：冻结前规则声明、错误分流、行数/拒绝率/Schema 校验与 Doris 原子换表，以及**独立的质量报告实体与查询接口**（`GET /runs/{id}` 内联返回，含按错误码的拒绝分布）；仅执行 `not_null` / `regex` 的 `reject` 规则，其余在编译期明确拒绝。**真实受管链路已验证 7/1 分流与原子发布**
- ✅ **T-17 预算监督**：行数、字节、时长、放大比、拒绝率五维判定，停止请求与失败快照，以及 **FR-37 可解释根因诊断**（按维度给出根因与可操作建议，并报告「声明了却判不了」的维度）；**真实引擎中断已验证**——行数与拒绝率两个维度均实测拦截且未发布
- ✅ **T-18 受管回滚**：回滚执行准备单里已批准的那份方案（`discard-shadow`），`POST /runs/{id}/rollback`（API-10）；换表后影子表名下已是被换下来的旧正式表，此时拒绝清理
- ✅ **T-19 维护任务**：超时回收（投递重试耗尽或事件缺失）、审批过期级联终止、租约过期回收重排（有副作用记录的一律收敛为失败）；`maintenance.sweep` 已入 Beat 调度
- ✅ **T-20 证据账本**：哈希链落库（`seq` 递增 + `prev_hash` 链接 + 创世哈希）、Ed25519 签名检查点（每 100 条或每 5 分钟锚定链头）、校验器（**启动时 + Beat 每 5 分钟巡检**，篡改可检出且能报出首个失败位置）、自包含审计报告（内嵌复算配方与公钥，可离线验证）与三个只读接口（API-11~13）。关键事件（Prepare / Approve / Commit / 执行 / 回滚）全覆盖，被拒的审批尝试同样留痕
- ⬜ **T-21 Benchmark**：版本化评测集与准确率大盘尚未实现
- 🟨 **T-22 前端**：已有 `.pen` 设计和 HTML 静态原型，Vue 业务前端与接口联调未完成
- ⬜ **T-23 全链路验收**：未完成；任务依赖与后续顺序见 [`SPEC §10`](docs/SPEC-ETL-Agent-v1.0.md#10-任务分解清单)

**已验证范围**：控制面流程在真实 PostgreSQL + LangGraph 断点持久化环境跑通：
登录 → 登记数据源 → 生成方案 → 落草稿版本 → 门禁冻结 → Prepare → 双维审批 → Commit。

**数据面受管链路已于 2026-10-16 首次完整跑通**（`api/app/scripts/run_demo_managed_flow.py`
示例：`ods_order_wide__formal_pipeline_demo_v9`）：MySQL 8 行 → 质量分流 7 行有效 / 1 行错误
→ 影子表原子换入正式表。核对结果为正式表 7 行且不含被拒的「吴十」、错误表 1 行并标注
`USER_NAME_REGEX`、影子表名下为换下来的旧表；质量报告经 `GET /runs/{id}` 可查。

失败面同样实测：`max_reject_ratio` 收紧到 0.05（1/8 越界）、`max_rows` 收紧到 5（源 8 行越界），
两次运行的正式表均保持 0 行**未被触碰**，越界快照含 FR-37 根因诊断与可操作建议。

此前一版记录把 7/1 不成立归因于「种子方案的 `quality_rules` 为空」——**那个判断是错的**。
规则一直在（v2/v3 版本里都有）。真正的成因是规则里的中文「吴」在提交时经 Windows GBK
转换被降级成 ASCII `?`：正则从「不以**吴**开头」变成「不以**问号**开头」，8 字节的
`^[^?].*$` 与 10 字节的 `^[^吴].*$` 同样是合法契约，控制面于是一路放行、8 行全部通过、
错误表 0 行，而运行状态仍是 `succeeded`。演示脚本现在带一道**提交后回读校验**，
把这条只发生过一次的教训变成每次都会执行的检查。

**测试记录（2026-10-16）**：最新完整复跑为 **458 个用例全部通过**（`ruff check app tests` 干净；
迁移经 `alembic upgrade`/`downgrade` 往返与 `alembic check` 零差异验证）。
此前预算中断时运行行 `read_rows` 未更新的问题已通过调整轮询顺序修复；T-18 回滚、T-19 维护回收、
质量报告与根因诊断的回归用例分别见 `api/tests/test_rollback.py`、`test_maintenance.py`、
`test_quality_report.py` 与 `test_diagnosis.py`。
单元测试使用替身，不证明真实模型准确率或真实集群验收达标。

**运行边界**：默认 `DATA_PLANE_BACKEND=unavailable`，未显式启用时运行以
`SYS_DATAPLANE_UNAVAILABLE` 失败。设置 `DATA_PLANE_BACKEND=seatunnel` 后，Worker
经 Tool Broker 提交冻结配置，SeaTunnel 搬运数据，质量校验通过后才允许发布。
不支持的质量规则会阻止执行；预算度量缺失时对应维度不可判定，不能据此宣称五维监督已验收。
固定方案种子与演示账号双审仅用于技术验证，不替代真实模型生成与独立人员审批。

**下一步**：推进 T-21 Benchmark 与 T-22 前端及后端接口的联调 → T-23 全链路验收。
证据账本（T-20）已完成；需注意 `tool_broker` 的留痕接口虽已补成真落库
（`LedgerInvocationSink`），但 **Broker 目前仍没有注册任何真实工具**，
所以 `tool.invoked` 是「接口就位」而非「已在产生记录」。

数据面环境（Doris / MySQL / SeaTunnel）的准备见 [`deploy/README.md`](deploy/README.md)。
