#!/bin/bash
# ============================================================
# ETL-Agent 端到端测试：MySQL(orders) → Doris(ods_order_wide_shadow)
# 对应 SPEC 主链路 T-13 ~ T-16 的最小验证
#
# 前置（全部须已就绪）：
#   1. Doris 容器 running 且 BE 已注册   → deploy/doris/docker-compose.yml
#   2. MySQL 源容器 running              → deploy/mysql/docker-compose.yml
#   3. SeaTunnel 连接器已安装            → connector-doris / connector-jdbc / mysql-connector-j
#   4. Zeta 集群已启动                    → ./bin/seatunnel-cluster.sh -d
#
# 用法：bash deploy/run-e2e-test.sh
# ============================================================
set -e

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
SEATUNNEL_HOME="$DEPLOY_DIR/../seatunnel/apache-seatunnel-2.3.13"

# ---- 源端（MySQL 演示库）----
export MYSQL_URL="jdbc:mysql://127.0.0.1:3307/demo?useSSL=false&serverTimezone=Asia/Shanghai&allowPublicKeyRetrieval=true"
export MYSQL_USER="root"
export MYSQL_PASSWORD="etl_demo_pwd"

# ---- 目标端（Doris 影子表）----
export DORIS_FENODES="127.0.0.1:8030"
export DORIS_USER="root"
export DORIS_PASSWORD=""
export DORIS_TABLE="ods_order_wide_shadow"

# ---- 作业唯一标识：用于 sink.label-prefix（FR-27 幂等）----
export RUN_ID="e2e-$(date +%Y%m%d-%H%M%S)"

echo "=========================================="
echo " RUN_ID      : $RUN_ID"
echo " 源          : $MYSQL_URL"
echo " 目标        : $DORIS_FENODES / $DORIS_TABLE"
echo "=========================================="

cd "$SEATUNNEL_HOME"
./bin/seatunnel.sh --config "$DEPLOY_DIR/seatunnel/mysql-to-doris.conf" -m cluster

echo ""
echo "=========================================="
echo " 作业已提交。验证影子表数据："
echo "   mysql -h127.0.0.1 -P9030 -uroot -e 'SELECT COUNT(*) FROM ods.ods_order_wide_shadow;'"
echo ""
echo " 验证原子替换（FR-34）："
echo "   mysql -h127.0.0.1 -P9030 -uroot -e \"ALTER TABLE ods.ods_order_wide REPLACE WITH TABLE ods.ods_order_wide_shadow PROPERTIES('swap'='true');\""
echo "=========================================="
