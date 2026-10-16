# ETL-Agent 开发与运维手册

面向**搭环境、起服务、排查故障**的人。不讲业务流程（那是
[`USER-GUIDE-ETL-Agent-v1.0.md`](USER-GUIDE-ETL-Agent-v1.0.md) 的事），
也不讲代码结构（那是 [`SPEC-ETL-Agent-v1.0.md`](SPEC-ETL-Agent-v1.0.md) 的事）。

本文所有命令与端口都在 Windows 11 + Docker Desktop (WSL2) 上**实测过**，
踩到的坑一并写在正文与第七节。

---

## 一、环境总览

### 组件与端口（实测）

| 组件 | 容器名 | 宿主端口 | 用途 |
|---|---|---|---|
| PostgreSQL 16 | `etl-postgres` | **5432** | 控制面业务库 + LangGraph 断点 |
| Redis 7 | `etl-redis` | **6379** | Celery broker / backend |
| Vault 1.17 | `etl-vault` | **8200** | 凭据后端（见下方说明） |
| Doris FE | `etl-doris-fe` | **8030**（HTTP）/ **8050**（MySQL 协议） | 目标库 |
| Doris BE | `etl-doris-be` | **8040**（仅调试） | 目标库存储 |
| SeaTunnel Zeta | `etl-seatunnel-zeta` | **8080** | 数据搬运引擎（REST V2） |
| MySQL 8.0 | `etl-mysql-source` | **3307** | 演示**源**库 |
| 后端 API | （本地进程） | **8000** | |
| 前端 dev server | （本地进程） | **5173** | |

**两个端口不是"标准"的，别改回去**：

- **Doris FE 用 8050 而不是 9030**：Windows 保留端口段 `8978-9077` 罩住了 9030，绑定会报
  `forbidden by access permissions`。容器**内部**仍用 9030，不影响集群。
- **演示 MySQL 用 3307 而不是 3306**：你本机可能已有一个独立安装的 MySQL 占着 3306。

### 依赖关系

```
前端 5173 ──/api 代理──► 后端 API 8000 ──► PostgreSQL 5432
                              │              Redis 6379（Celery 队列）
                              │              Vault 8200（凭据）
                              ▼
                        Worker（消费队列）
                              │ 经 Tool Broker 提交作业
                              ▼
                     SeaTunnel Zeta 8080 ──搬运──► Doris FE 8030 / BE
                              ▲                          ▲
                              └─── 读 ─── MySQL 3307 ─────┘
```

**要跑通数据面，这四样必须同时在场**：Worker、SeaTunnel Zeta、MySQL 源库、Doris。少一样，
运行会以明确的错误码失败（不是静默卡住）。

### 关于 Vault

`etl-vault` 常显示 `unhealthy`，**这不影响使用**。当前数据源凭据走的是本地 Fernet
加密库（`.secrets.enc`，`local://` 引用），Vault 只是备用路径。要真正启用 Vault 需另外初始化并写入 KV。

---

## 二、前置条件

| 需要 | 版本（实测） | 说明 |
|---|---|---|
| Docker Desktop | 任意近期版本 | 必须已启动；WSL2 后端 |
| Python | **3.12.6** | 后端运行环境 |
| Node.js | **24.19.0** / npm 11.17.0 | 前端 |
| WSL 内核参数 | `vm.max_map_count >= 2000000`、`swap = 0` | **Doris BE 的硬性前置** |

**WSL 参数怎么设**：

```powershell
# 1) swap：在 C:\Users\<你>\.wslconfig 里加
[wsl2]
swap=0
# 然后 wsl --shutdown 使其生效

# 2) max_map_count：在 WSL 里执行
wsl -e sudo sysctl -w vm.max_map_count=2000000
# 想持久化就写进 /etc/sysctl.d/99-etl-doris.conf，再 sysctl -p
```

> **每次重启 WSL 都要重设 `max_map_count`**（`swap=0` 已能持久）。不设的后果不是报错，
> 而是 Doris BE **静默 `Exited (0)`** —— 它的 entrypoint 校验不通过就主动退出，
> 日志里只有一行，很容易被当成"起得慢"。

---

## 三、启动

### 3.1 容器（四份独立的 compose）

```bash
cd <项目根目录>

docker compose up -d                                      # PostgreSQL / Redis / Vault
docker compose -f deploy/mysql/docker-compose.yml up -d    # 演示源库
docker compose -f deploy/doris/docker-compose.yml up -d    # Doris FE + BE
docker compose -f deploy/seatunnel-zeta.compose.yml up -d  # SeaTunnel Zeta
```

四份互相独立，可以单独起停。**顺序有讲究**：Doris 先起，SeaTunnel 的 compose 依赖
Doris 创建的网络 `doris_doris-net`（external network），Doris 没起时它会报网络不存在。

**验证**：

```bash
docker ps --filter "name=etl-" --format "{{.Names}}\t{{.Status}}"
```

Doris FE 要等到 `(healthy)`（首次约 30–60 秒），BE 要确认已注册：

```bash
docker exec etl-doris-fe mysql -h127.0.0.1 -P9030 -uroot -e "SHOW BACKENDS\G"
# 关注 Alive: true 与 ErrMsg 为空
```

### 3.2 后端（三层）

```bash
cd api

# API
DATA_PLANE_BACKEND=seatunnel LLM_BACKEND=deepseek \
  ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Worker（Windows 下 -P solo 是必须的，见第七节）
DATA_PLANE_BACKEND=seatunnel \
  ./.venv/Scripts/python.exe -m celery -A app.worker.celery_app:celery_app worker -l info -P solo

# Beat（必须单实例，多实例会重复投递）
./.venv/Scripts/python.exe -m celery -A app.worker.celery_app:celery_app beat -l info
```

Linux/macOS 把 `./.venv/Scripts/python.exe` 换成 `./.venv/bin/python` 即可。

**三层缺一不可**：

- 没有 **Beat**：Outbox 不会被分发，Commit 之后的运行永远是 `pending`
- 没有 **Worker**：任务堆在队列里没人认领，运行卡在 `running` 直到被维护任务回收

**两个环境变量的作用**（都**没有**写在 `.env` 里）：

| 变量 | 默认 | 不设的后果 |
|---|---|---|
| `DATA_PLANE_BACKEND` | `unavailable` | 运行必然以 `SYS_DATAPLANE_UNAVAILABLE` 失败 |
| `LLM_BACKEND` | `fake` | 走替身模型，**不真实调用大模型** |

想让它们长期生效，往 `api/.env` 加这两行。

首次跑还要建表与灌演示数据：

```bash
cd api
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m app.scripts.seed             # 幂等：演示项目 + 五个角色账号
```

### 3.3 前端

```bash
cd web
npm install      # 首次
npm run dev      # 5173
```

后端地址默认 `http://127.0.0.1:8000`，要改就设 `ETL_API_TARGET`（见 `vite.config.ts`）。

---

## 四、验证清单

按顺序验证，从下往上出错就往上找：

```bash
# 1. 容器都在
docker ps --filter "name=etl-" --format "{{.Names}}\t{{.Status}}"

# 2. 后端活着
curl http://127.0.0.1:8000/health                              # {"status":"ok"}

# 3. 前端能代理到后端（401 = 通了且鉴权生效）
curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5173/api/v1/projects

# 4. 登录能拿到令牌
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"maker@example.com","password":"Demo@12345"}' | head -c 80

# 5. SeaTunnel 集群就绪
curl http://127.0.0.1:8080/overview
```

浏览器打开 <http://127.0.0.1:5173>，用 `maker@example.com / Demo@12345` 登录。

---

## 五、环境变量速查

**实际值在 `api/.env`**（不入库），**全部可配项与默认值在 `api/app/core/config.py`**。
`.env` 只覆盖其中一部分，其余取代码默认值。

`.env` 里当前有的键：

```
APP_ENV / APP_NAME / APP_SECRET_KEY / ACCESS_TOKEN_EXPIRE_MINUTES
POSTGRES_HOST · PORT · USER · PASSWORD · DB
REDIS_HOST / REDIS_PORT / REDIS_DB
VAULT_ADDR / VAULT_TOKEN / VAULT_MOUNT_POINT
DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
```

**常用但不在 `.env` 里、需要用环境变量临时注入的**：

| 变量 | 什么时候用 |
|---|---|
| `DATA_PLANE_BACKEND=seatunnel` | 跑真实数据面（默认不可用） |
| `LLM_BACKEND=deepseek` | 走真实大模型（默认替身） |
| `BUDGET_MAX_ROWS` / `BUDGET_MAX_REJECT_RATIO` … | 收紧预算做越界演练 |
| `ETL_API_TARGET` | 前端代理指向别的后端 |

**演示凭据**：

| 用途 | 值 |
|---|---|
| 平台账号（5 个） | 口令统一 `Demo@12345` |
| MySQL 演示源库 | `root` / `etl_demo_pwd` |
| PostgreSQL | `etl` / `etl_dev_pwd` |
| Doris | `root` / 空口令 |

---

## 六、常用运维命令

```bash
# ---- 看日志 ----
docker logs -f etl-doris-be                    # BE 起不来时第一个该看的
docker logs -f etl-seatunnel-zeta              # 作业提交失败看这里
docker compose logs -f postgres

# ---- 查控制面库 ----
docker exec etl-postgres psql -U etl -d etl_agent -c "SELECT status, count(*) FROM execution_runs GROUP BY status;"

# ---- 查数据面 ----
docker exec etl-doris-fe mysql -h127.0.0.1 -P9030 -uroot -e "SHOW TABLES FROM ods;"
docker exec etl-mysql-source mysql -uroot -petl_demo_pwd -e "SELECT COUNT(*) FROM demo.orders;"

# ---- 校验证据链（T-20）----
cd api && ./.venv/Scripts/python.exe -m app.scripts.verify_audit_chain --record
# 退出码非 0 = 链校验不通过

# ---- 后端测试与静态检查 ----
cd api
./.venv/Scripts/python.exe -m pytest                     # 459 passed
./.venv/Scripts/python.exe -m ruff check app tests

# ---- 数据库迁移 ----
./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m alembic check               # 模型与库是否有差异

# ---- 前端 ----
cd web
npm test          # vitest 单元测试
npm run build     # vue-tsc 类型检查 + 打包
npx playwright test   # 端到端（需要后端在跑）
```

**重建演示数据**：`python -m app.scripts.seed`（幂等，重复跑无害）。

**预检一次受管运行**（不签令牌、不提交作业）：

```bash
cd api && ./.venv/Scripts/python.exe -m app.scripts.preflight_demo_run <preparation_id>
```

---

## 七、故障排查

按「现象 → 原因 → 处置」组织。这些都是**实测踩过的**。

### 容器

| 现象 | 原因 | 处置 |
|---|---|---|
| `etl-doris-be` 显示 `Exited (0)`，不报错 | WSL `max_map_count` 或 swap 不满足，entrypoint 主动退出 | 见第二节重设参数，然后 `docker start etl-doris-be` |
| `docker compose up` 报 `Pool overlaps` | 自定义子网撞上别的 Docker 网络 | 改 `deploy/doris/docker-compose.yml` 的 `ipam.config.subnet` 与两处 `ipv4_address` |
| FE 端口绑定失败 `forbidden by access permissions` | Windows 保留端口段（含 9030） | 保持用 8050 映射 |
| SeaTunnel compose 报 network 不存在 | 它依赖 Doris 创建的 `doris_doris-net` | 先起 Doris |
| `etl-vault` 一直 `unhealthy` | dev 模式的健康检查判定 | **可忽略**，当前凭据走本地 Fernet |

### 后端

| 现象 | 原因 | 处置 |
|---|---|---|
| Worker 反复抛 `PermissionError: [WinError 5]` | Windows 下默认 prefork 池的共享内存问题 | 加 `-P solo` |
| Commit 之后运行一直 `pending` | Beat 没起，Outbox 没被分发 | 起 Beat，或手工触发 `outbox.dispatch` |
| 运行卡在 `running` 不动 | Worker 没起或已死 | 查 Worker 日志；维护任务会按租约回收重排 |
| 运行失败 `SYS_DATAPLANE_UNAVAILABLE` | 没设 `DATA_PLANE_BACKEND=seatunnel` | 带上这个环境变量重启 API 与 Worker |
| 数据源探查报「驱动不可用」 | 该类型数据源的 DBAPI 未安装 | 装对应驱动 |

### 数据面

| 现象 | 原因 | 处置 |
|---|---|---|
| 作业失败「Label already exists」 | Doris 的导入 label 重复 | 系统按 `run_id` 生成已避免；手工跑作业时给唯一前缀 |
| 搬运失败，但探查一切正常 | 源/目标地址用了宿主视角的 `127.0.0.1`，而 SeaTunnel 在**容器内** | 数据源 config 里给 SeaTunnel 用的地址必须是 `host.docker.internal:3307`（源）、`etl-doris-fe:8030`（目标） |
| 宿主机连不上 BE 的容器 IP | WSL2 网络隔离，容器 IP 对宿主不可路由 | 这是架构性限制；数据面组件必须与 Doris 同网络 |

### 前端

| 现象 | 原因 | 处置 |
|---|---|---|
| 构建报 `Cannot find module './router'` | 路由文件缺失 | 本项目已补；确认 `web/src/router/index.ts` 存在 |
| 登录后停在「创建你的第一个项目」 | 会话恢复没被触发（历史缺陷，已修） | 确认 `App.vue` 里调用了 `restore()` |
| `npm run test:e2e` 报 `Executable doesn't exist` | Playwright 浏览器未下载 | `npx playwright install chromium`；国内可加 `PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright` |
| 页面空白、控制台 401 | 令牌过期 | 正常行为，会自动跳回登录页 |

---

## 八、停机与重启

```bash
# 只停后端：Ctrl+C 三个终端即可；前端同理

# 停容器（保留数据卷）
docker compose -f deploy/seatunnel-zeta.compose.yml down
docker compose -f deploy/doris/docker-compose.yml down
docker compose -f deploy/mysql/docker-compose.yml down
docker compose down

# 连数据卷一起删（会清空所有数据与证据账本，慎用）
docker compose -f deploy/doris/docker-compose.yml down -v
docker compose down -v
```

**重启 WSL 后必须做的事**（按顺序）：

1. 重设 `vm.max_map_count`（第二节）
2. 重启 Docker Desktop，等它把容器拉起来
3. 检查 Doris BE 是否 `Up`；不在就 `docker start etl-doris-be`
4. 重新起后端三层与前端

---

## 九、这套环境里"看起来像坏了、其实是设计如此"的地方

避免把正常行为当故障排查：

- **空链的校验状态是 `尚无记录` 而不是 `校验通过`** —— 「没有证据」与「证据可信」是两回事。
- **审计台显示 `无法完整验证`** —— 哈希链完好，只是有检查点用的密钥不在手边（通常是轮换）。这不等于被篡改，也不阻断操作。
- **运行显示 `succeeded` 但「未发布」** —— 「跑完了」与「换表了」是两件事。
- **质量结论显示「本次运行没有质量契约」** —— 这个版本没声明质量规则，**不等于数据检查过了**。
- **取消之后状态还是「正在停止」** —— 取消是请求不是强杀，执行层要在安全点才停。
- **同一目标表第二次跑会拒绝建版本** —— 防止对已发布目标重复写入；换一个新的目标表名。
