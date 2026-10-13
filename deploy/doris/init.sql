-- ============================================================
-- ETL-Agent 演示库初始化（Doris 2.1.7）
-- 执行：mysql -h127.0.0.1 -P9030 -uroot < init.sql
--
-- 注意：本项目以 Doris 作为【目标库】，MySQL 为源库。
-- 影子表由控制面（SPEC §3 M10）在运行时用 CREATE TABLE ... LIKE 生成，
-- 本文件给出等价结构样例，便于手工验证 FR-34 的原子 Swap。
-- ============================================================

CREATE DATABASE IF NOT EXISTS ods;

-- 正式表（对外可见的发布目标）
CREATE TABLE IF NOT EXISTS ods.ods_order_wide (
    order_id     BIGINT        NOT NULL COMMENT '订单ID',
    user_name    VARCHAR(64)   NULL     COMMENT '用户名',
    order_amount DECIMAL(18,2) NULL     COMMENT '订单金额',
    order_date   DATE          NOT NULL COMMENT '订单日期'
)
ENGINE = OLAP
DUPLICATE KEY(order_id)
DISTRIBUTED BY HASH(order_id) BUCKETS 3
PROPERTIES (
    "replication_num" = "1"          -- 单 BE 环境必须为 1
);

-- 影子表：结构与正式表一致（运行时由控制面自动生成）
CREATE TABLE IF NOT EXISTS ods.ods_order_wide_shadow LIKE ods.ods_order_wide;

-- ============================================================
-- FR-34 / FR-38 验证用：原子替换（影子表 → 正式表）
--   swap = true  → 两表互换，旧数据保留在 _shadow 名下【可用于回滚】
--   swap = false → 旧表被直接删除【无法回滚，本项目禁止使用】
--
-- ALTER TABLE ods.ods_order_wide
--   REPLACE WITH TABLE ods.ods_order_wide_shadow
--   PROPERTIES('swap' = 'true');
--
-- ⚠️ 前置约束（Doris 不会校验，必须由应用保证）：
--   1. 两张表都必须是 OLAP 内部表（视图 / 外部表不可用于 REPLACE）
--   2. 两表 schema 必须兼容，否则替换后下游查询报错
--   3. 替换后权限随表名迁移，新表自动继承原表名上的权限
-- ============================================================
