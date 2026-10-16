# 部署说明（deploy/）

ETL-Agent 数据面依赖的本地环境：**Apache Doris 2.1.7**（目标库）+ **Apache SeaTunnel 2.3.13**（搬运引擎）。

来源依据：`docs/SPEC-ETL-Agent-v1.0.md` §11.1 的 V1 / V2 结论。

```
deploy/
├── doris/
│   ├── docker-compose.yml      # Doris 2.1.7 单机（1 FE + 1 BE）
│   └── init.sql                # 建库、正式表、影子表样例
└── seatunnel/
    └── mysql-to-doris.conf     # MySQL → Doris 影子表 示例作业
```

---

## 一、启动 Doris

**前置：先启动 Docker Desktop 守护进程**（当前环境尚未运行）。

```bash
cd deploy/doris
docker compose up -d
docker compose logs -f doris-fe      # 等到 FE 输出 "FE is ready"，约 30–60 秒
```

启动后验证：

```bash
mysql -h127.0.0.1 -P8050 -uroot < init.sql          # 初始化库表
mysql -h127.0.0.1 -P8050 -uroot -e "SHOW BACKENDS\G"  # 确认 BE 已注册（Alive: true）
```

**Windows / macOS 说明**：本 compose 使用 bridge 网络 + 固定容器 IP（`172.20.80.3/5`），
因为 `network_mode: host` 只在 Linux 可用。若与本机其他 Docker 网络网段冲突，改 `ipam.config.subnet` 与两处 `ipv4_address` 即可。

**镜像拉取慢**：`apache/doris:doris-fe-2.1.7` 体积较大，国内建议在 Docker Desktop
→ Settings → Docker Engine 配置 registry mirror（如 `https://docker.mirrors.ustc.edu.cn`）。

---

## 二、SeaTunnel 连接器（✅ **已安装**，2026-10-14）

**现状**：`seatunnel/apache-seatunnel-2.3.13/connectors/` 已含 5 个 jar——`connector-doris`、`connector-jdbc`、`connector-cdc-base`、`connector-console`、`connector-fake`（均为 2.3.13 版本）；`config/plugin_config` 已同步追加 `connector-doris` 与 `connector-jdbc`。

**本项目 P0 场景使用 JDBC 源**（见 `mysql-to-doris.conf` 的 `source { Jdbc { ... } }`），链路为「全量搬运 → 影子表 → 原子 Swap」，不需要 CDC 增量，因此**未安装 `connector-cdc-mysql`**。

验证：

```bash
ls seatunnel/apache-seatunnel-2.3.13/connectors/ | grep -E "doris|jdbc"
# 应输出 connector-doris-2.3.13.jar 与 connector-jdbc-2.3.13.jar
```

<details>
<summary>重装 / 升级时参考：连接器安装步骤</summary>

1. 编辑 `seatunnel/apache-seatunnel-2.3.13/config/plugin_config`：

   ```
   --seatunnel-connectors--
   connector-fake
   connector-console
   connector-doris          # 目标：Doris Sink
   connector-jdbc           # 源：MySQL JDBC
   # connector-cdc-mysql     # 如需 CDC 增量同步再启用
   --end--
   ```

2. 配置国内 Maven 镜像后执行下载（脚本内部走 `mvnw dependency:get`）：

   ```bash
   cd seatunnel/apache-seatunnel-2.3.13
   ./bin/install-plugin.sh          # Linux/macOS
   .\bin\install-plugin.ps1         # Windows PowerShell（如无则用 install-plugin.cmd）
   ```

3. 验证：`ls connectors/ | grep doris` 应出现 `connector-doris-2.3.13.jar`

</details>

---

## 三、Doris Sink 参数标准（注册表条目，对应 SPEC §3 M4）

| 参数 | 必填 | 本项目标准取值 | 说明 |
|------|:----:|---------------|------|
| `fenodes` | ✅ | `doris-fe:8030` | FE HTTP 地址；容器内用服务名，宿主机用 `127.0.0.1:8030` |
| `username` | ✅ | `${DORIS_USER}` | 由 Vault 注入（FR-03） |
| `password` | ✅ | `${DORIS_PASSWORD}` | 由 Vault 注入，配置文件不落明文 |
| `database` | ✅ | `ods` | **不要用已废弃的 `table.identifier`** |
| `table` | ✅ | `${DORIS_TABLE}` | 指向**影子表**，替换后由控制面执行 Swap |
| `sink.label-prefix` | ✅ | `etl-${RUN_ID}` | **必须按作业全局唯一**，用 run_id 即满足 FR-27 幂等 |
| `sink.enable-2pc` | ⬜ | `true` | 开启 Exactly-Once |
| `sink.enable-delete` | ⬜ | `false` | 本示例不做删除同步 |

---

## 四、运行模式

T-15 控制面使用 **Zeta 集群模式 + REST V2**，启动与接入方法见第七节。
现有 `run-in-docker.sh` 是独立的 Local 模式连接器诊断脚本；其作业无法由控制面
通过 REST 查询或取消，不能作为 T-15 验收结果。

---

## 五、已知约束（来自 SPEC §11.1）

1. **影子表与被替换表都必须是 OLAP 内部表**——视图、外部表不能用于 `REPLACE WITH TABLE`。
2. **Doris 不校验两表 schema 一致性**，替换前的一致性由本项目自行保证（否则下游查询会报错）。
3. **必须使用 `swap = true`**——`swap = false` 会直接删除旧表，导致 FR-38 无法回滚。
4. 单 BE 环境下建表必须带 `"replication_num" = "1"`。

---

## 六、Windows 环境适配记录

本机为 **Windows 11 + Docker Desktop (WSL2)**，部署中踩到 5 个平台特有坑，均已解决，记录备查：

| # | 现象 | 根因 | 解决 |
|---|------|------|------|
| 1 | `docker compose` 建网络报 `Pool overlaps` | 自定义子网 `172.20.80.0/24` 落在已有 `etl-agent_default`（`172.20.0.0/16`）内 | Doris 子网改用 `172.30.80.0/24` |
| 2 | FE 端口 9030 绑定失败 `forbidden by access permissions` | **Windows 保留端口段 `8978-9077` 罩住 9030** | 宿主映射改为 **8050**（`8050:9030`）；集群内部仍用 9030，不受影响 |
| 3 | BE 容器 `Exited (0)` | entrypoint 校验 `vm.max_map_count` 与 swap，二者不满足即主动退出 | ① WSL2 各发行版**共享内核**，在 Ubuntu 里 `sysctl -w vm.max_map_count=2000000`；② `swapoff -a` |
| 4 | 9010 等端口也被拒 | 除静态保留段外，winnat 还会**动态保留**端口 | 内部通信端口（9010/9050/9060）不再暴露到宿主 |
| 5 | **宿主无法访问容器 IP `172.30.80.5`** | WSL2 网络隔离，容器 IP 对宿主机不可路由 | **见下方说明** |

### 第 5 点是架构性限制（重要）

Doris 的 Stream Load 流程是「客户端 → FE 拿 BE 地址 → **直连 BE** 写入」。
FE 返回的是 BE 的**容器 IP**，而宿主机路由不到它 —— 因此**跑在宿主机的 SeaTunnel 无法完成对 Doris 的写入**。

| 路径 | 可达性 |
|------|--------|
| 宿主 → FE `127.0.0.1:8030` | ✅ |
| 宿主 → BE `127.0.0.1:8040`（映射端口） | ✅ |
| **宿主 → BE 容器 IP `172.30.80.5:8040`** | ❌ 不可达 |
| 容器 → 容器（FE ↔ BE） | ✅ |

**结论**：数据面组件（SeaTunnel）必须与 Doris **同处一个 Docker 网络**，不能跑在宿主机上。落地方式见 `deploy/seatunnel/run-in-docker.sh`（以 `--network doris_doris-net` 加入 Doris 所在网络）。

## 七、T-15 REST 适配器联调

`run-in-docker.sh` 是一次性 **Local 模式**作业，仅用于连接器/网络诊断；控制面
T-15 使用常驻 Zeta 集群和 REST V2。先启动 Doris，再启动集群：

```bash
docker compose -f deploy/doris/docker-compose.yml up -d
docker compose -f deploy/seatunnel-zeta.compose.yml up -d
curl http://127.0.0.1:8080/overview
```

Worker 的 `.env` 设 `DATA_PLANE_BACKEND=seatunnel`。Worker 在宿主机运行时设置
`SEATUNNEL_BASE_URL=http://127.0.0.1:8080`；若 Worker 也在 `doris_doris-net`，
设置为 `http://etl-seatunnel-zeta:8080`。登记的源端地址必须可从 SeaTunnel **容器**
访问（本机 MySQL 可用 `host.docker.internal`），Doris FE 地址应使用同网络中的
`etl-doris-fe:8030`。本适配器只提交到准备单指定的影子表，T-16 才负责质量校验与发布。
非 `dev`/`demo` 环境须将 SeaTunnel REST 配为 HTTPS；提交请求体包含运行时注入的凭据。
API 与 Worker 还须共用同一凭据后端：使用 Vault，或共享 `LOCAL_SECRET_FILE` 指向的加密文件。
T-16 前 `rejected_rows=0` 只是接口占位值，不代表数据质量校验通过。
早期冻结版本若没有 `${TARGET_TABLE}` 与 `etl-${RUN_ID}` 运行时占位符，必须重新生成、
冻结并审批；适配器会拒绝可能直写正式表的旧配置，不会修改已冻结版本。

当前测试使用假 REST 传输；真实 Zeta + MySQL + Doris 的控制面端到端联调尚待执行。
