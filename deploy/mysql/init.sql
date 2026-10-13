-- ============================================================
-- ETL-Agent 演示源库初始化
-- 对应示例场景：MySQL(orders) → Doris(ods_order_wide)
-- ============================================================

CREATE DATABASE IF NOT EXISTS demo;
USE demo;

CREATE TABLE IF NOT EXISTS orders (
    order_id     BIGINT        NOT NULL COMMENT '订单ID',
    user_name    VARCHAR(64)   NULL     COMMENT '用户名',
    order_amount DECIMAL(18,2) NULL     COMMENT '订单金额',
    order_date   DATE          NOT NULL COMMENT '订单日期',
    PRIMARY KEY (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表（演示）';

INSERT INTO orders (order_id, user_name, order_amount, order_date) VALUES
    (1001, '张三',   199.00, '2026-10-01'),
    (1002, '李四',   358.50, '2026-10-01'),
    (1003, '王五',    88.00, '2026-10-02'),
    (1004, '赵六',  1280.00, '2026-10-02'),
    (1005, '钱七',   466.66, '2026-10-03'),
    (1006, '孙八',   299.99, '2026-10-03'),
    (1007, '周九',    15.00, '2026-10-04'),
    (1008, '吴十',   777.77, '2026-10-04')
ON DUPLICATE KEY UPDATE user_name = VALUES(user_name);
