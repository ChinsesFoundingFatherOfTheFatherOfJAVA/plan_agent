-- ============================================================
-- 005_create_routing.sql
-- 目的：引入工艺路线表，替换 data_fetch_node 中 0.5h/件 的全局硬编码假设
--
-- 背景：
--   此前排产工时统一为 quantity * 0.5，所有产品在所有工作中心
--   花费相同时间，求解器无法做出有意义的工作中心分配。
--   routing 表记录每个产品在哪些工作中心可加工、每件标准工时多少，
--   求解器据此做差异化分配。
--
-- 设计说明：
--   - 一个产品可在多个工作中心加工（多机台兼容），每条记录的 hours_per_unit 不同
--   - allowed_centers 由 routing 表中存在该产品的记录自动推导
--   - setup_time_minutes 为换型时间（分钟），当前为 0，留待后续接入
--   - status='active' 的记录才参与排产，停用产线不选
--
-- 种子数据（用户指定）：
--   PROD-001: WC-001 0.1h/件, WC-002 0.2h/件
--   PROD-002: WC-001 0.3h/件, WC-002 0.4h/件
--   WC-003 当前 maintenance，不参与排产
-- ============================================================

CREATE TABLE IF NOT EXISTS routing (
    routing_id         VARCHAR(32) PRIMARY KEY,
    product_id         VARCHAR(32) NOT NULL,
    center_id          VARCHAR(32) NOT NULL REFERENCES work_center(center_id),
    hours_per_unit     NUMERIC(10,6) NOT NULL CHECK (hours_per_unit > 0),
    setup_time_minutes INTEGER NOT NULL DEFAULT 0 CHECK (setup_time_minutes >= 0),
    status             VARCHAR(20) NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'inactive')),
    -- 同一产品在同一工作中心只能有一条工艺路线
    CONSTRAINT uk_routing_product_center UNIQUE (product_id, center_id)
);

CREATE INDEX IF NOT EXISTS idx_routing_product ON routing (product_id);
CREATE INDEX IF NOT EXISTS idx_routing_center  ON routing (center_id);

COMMENT ON TABLE routing IS '工艺路线：定义产品在工作中心上的标准工时与换型时间';
COMMENT ON COLUMN routing.hours_per_unit IS '每件产品的标准加工工时（小时），已包含效率系数的影响';

-- ============================================================
-- 种子数据
-- ============================================================
INSERT INTO routing (routing_id, product_id, center_id, hours_per_unit, setup_time_minutes, status)
VALUES
    ('RT-001', 'PROD-001', 'WC-001', 0.1, 0, 'active'),
    ('RT-002', 'PROD-001', 'WC-002', 0.2, 0, 'active'),
    ('RT-003', 'PROD-002', 'WC-001', 0.3, 0, 'active'),
    ('RT-004', 'PROD-002', 'WC-002', 0.4, 0, 'active')
ON CONFLICT (product_id, center_id) DO UPDATE SET
    hours_per_unit = EXCLUDED.hours_per_unit,
    setup_time_minutes = EXCLUDED.setup_time_minutes,
    status = EXCLUDED.status;