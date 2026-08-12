-- 智能排产计划Agent - PostgreSQL 建表脚本
-- 注意：原 database/init.sql 为 MySQL 语法（TINYINT/内联 COMMENT/ON UPDATE），PG 不兼容，此文件为 PG 正式版本
-- 幂等：可重复执行

BEGIN;

-- 通用：updated_at 自动更新触发器函数（替代 MySQL 的 ON UPDATE CURRENT_TIMESTAMP）
CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS trigger AS $fn$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

-- ============ 订单 ============
CREATE TABLE IF NOT EXISTS sales_order (
    order_id      VARCHAR(32) PRIMARY KEY,
    customer_id   VARCHAR(32)  NOT NULL,
    order_date    DATE         NOT NULL,
    required_date DATE         NOT NULL,
    priority      SMALLINT     NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    status        VARCHAR(20)  NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','confirmed','in_progress','completed','cancelled')),
    total_amount  NUMERIC(15,2),
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE  sales_order IS '销售订单主表';
COMMENT ON COLUMN sales_order.required_date IS '要求交期（订单级）';
COMMENT ON COLUMN sales_order.priority IS '优先级(1-紧急 2-正常 3-低)';

DROP TRIGGER IF EXISTS tg_sales_order_updated_at ON sales_order;
CREATE TRIGGER tg_sales_order_updated_at BEFORE UPDATE ON sales_order
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

CREATE TABLE IF NOT EXISTS sales_order_item (
    item_id    VARCHAR(32) PRIMARY KEY,
    order_id   VARCHAR(32)  NOT NULL REFERENCES sales_order(order_id) ON DELETE CASCADE,
    product_id VARCHAR(32)  NOT NULL,
    quantity   NUMERIC(12,2) NOT NULL CHECK (quantity > 0),
    unit_price NUMERIC(12,2),
    due_date   DATE
);
COMMENT ON COLUMN sales_order_item.due_date IS '明细交期（优先于订单交期）';

-- ============ 物料 ============
CREATE TABLE IF NOT EXISTS material (
    material_id    VARCHAR(32) PRIMARY KEY,
    material_name  VARCHAR(100) NOT NULL,
    material_type  VARCHAR(20)  CHECK (material_type IN ('raw','semi','finished')),
    unit           VARCHAR(10),
    safety_stock   NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (safety_stock >= 0),
    lead_time_days INTEGER       NOT NULL DEFAULT 0 CHECK (lead_time_days >= 0),
    supplier_id    VARCHAR(32)
);
COMMENT ON COLUMN material.lead_time_days IS '采购提前期(天)';

CREATE TABLE IF NOT EXISTS inventory (
    inventory_id       VARCHAR(32) PRIMARY KEY,
    material_id        VARCHAR(32) NOT NULL REFERENCES material(material_id),
    warehouse_id       VARCHAR(32),
    quantity_on_hand   NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (quantity_on_hand   >= 0),
    quantity_reserved  NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (quantity_reserved  >= 0),
    quantity_on_order  NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (quantity_on_order  >= 0),
    last_updated       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uk_inventory_mat_wh UNIQUE (material_id, warehouse_id)
);

CREATE TABLE IF NOT EXISTS bill_of_material (
    bom_id            VARCHAR(32) PRIMARY KEY,
    product_id        VARCHAR(32)  NOT NULL,
    material_id       VARCHAR(32)  NOT NULL REFERENCES material(material_id),
    quantity_required NUMERIC(12,4) NOT NULL CHECK (quantity_required > 0),
    CONSTRAINT uk_bom_product_material UNIQUE (product_id, material_id)
);
COMMENT ON COLUMN bill_of_material.quantity_required IS '单位用量';

-- ============ 产能 ============
CREATE TABLE IF NOT EXISTS work_center (
    center_id        VARCHAR(32) PRIMARY KEY,
    center_name      VARCHAR(100) NOT NULL,
    capacity_per_day NUMERIC(10,2) CHECK (capacity_per_day > 0),
    efficiency_rate  NUMERIC(5,2) NOT NULL DEFAULT 1.0 CHECK (efficiency_rate > 0 AND efficiency_rate <= 1),
    status           VARCHAR(20)  NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','inactive','maintenance'))
);

-- available_hours = 班制工时 * 效率系数 - 计划停机；planned_hours 为已排产占用；actual_hours 为实际反馈
CREATE TABLE IF NOT EXISTS capacity_calendar (
    calendar_id     VARCHAR(32) PRIMARY KEY,
    center_id       VARCHAR(32) NOT NULL REFERENCES work_center(center_id),
    work_date       DATE        NOT NULL,
    available_hours NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (available_hours >= 0),
    planned_hours   NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (planned_hours  >= 0),
    actual_hours    NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (actual_hours   >= 0),
    CONSTRAINT uk_capacity_center_date UNIQUE (center_id, work_date)
);

-- ============ 排产计划 ============
CREATE TABLE IF NOT EXISTS production_plan (
    plan_id       VARCHAR(32) PRIMARY KEY,
    plan_name     VARCHAR(100),
    plan_date     DATE        NOT NULL,
    horizon_start DATE,
    horizon_end   DATE,
    status        VARCHAR(20) NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','confirmed','executing','completed','cancelled')),
    created_by    VARCHAR(32),
    created_at    TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_plan_horizon CHECK (horizon_end IS NULL OR horizon_start IS NULL OR horizon_end >= horizon_start)
);

DROP TRIGGER IF EXISTS tg_production_plan_updated_at ON production_plan;
CREATE TRIGGER tg_production_plan_updated_at BEFORE UPDATE ON production_plan
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

CREATE TABLE IF NOT EXISTS production_task (
    task_id      VARCHAR(32) PRIMARY KEY,
    plan_id      VARCHAR(32)  NOT NULL REFERENCES production_plan(plan_id) ON DELETE CASCADE,
    plan_version INTEGER      NOT NULL DEFAULT 1,
    order_id     VARCHAR(32)  REFERENCES sales_order(order_id),
    item_id      VARCHAR(32)  REFERENCES sales_order_item(item_id),
    product_id   VARCHAR(32)  NOT NULL,
    center_id    VARCHAR(32)  NOT NULL REFERENCES work_center(center_id),
    quantity     NUMERIC(12,2) NOT NULL CHECK (quantity > 0),
    start_time   TIMESTAMP    NOT NULL,
    end_time     TIMESTAMP    NOT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'planned'
                 CHECK (status IN ('planned','scheduled','in_progress','completed','cancelled')),
    priority     SMALLINT     NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_task_time CHECK (end_time > start_time)
);

-- 注意：此处原有全局唯一索引 uk_center_time（技术债 TD-001），
--       已由 004 删除，其职责被 003 的 excl_center_time_overlap_in_plan 取代。
--       请勿恢复：全局唯一会阻止多候选方案并存。

-- TD-001 兜底：EXCLUDE 约束防止同一工作中心时间段重叠（plan.md 方案B，本次一并落地）
CREATE EXTENSION IF NOT EXISTS btree_gist;
DO $blk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'excl_center_time_overlap') THEN
        ALTER TABLE production_task ADD CONSTRAINT excl_center_time_overlap
            EXCLUDE USING gist (center_id WITH =, tsrange(start_time, end_time) WITH &&);
    END IF;
END
$blk$;

-- ============ 插单记录（affected_tasks 改为关联表，见 plan.md 附录A）============
CREATE TABLE IF NOT EXISTS insert_order_log (
    log_id           VARCHAR(32) PRIMARY KEY,
    order_id         VARCHAR(32) NOT NULL REFERENCES sales_order(order_id),
    original_plan_id VARCHAR(32) REFERENCES production_plan(plan_id),
    new_plan_id      VARCHAR(32) REFERENCES production_plan(plan_id),
    disruption_score NUMERIC(5,2) CHECK (disruption_score BETWEEN 0 AND 100),
    disruption_level VARCHAR(10)  CHECK (disruption_level IN ('low','medium','high')),
    status           VARCHAR(20)  NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','processing','completed','failed')),
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS insert_order_affected_task (
    id             BIGSERIAL PRIMARY KEY,
    log_id         VARCHAR(32) NOT NULL REFERENCES insert_order_log(log_id) ON DELETE CASCADE,
    task_id        VARCHAR(32) NOT NULL,
    impact_type    VARCHAR(20) CHECK (impact_type IN ('delayed','moved','split','cancelled')),
    old_start_time TIMESTAMP,
    new_start_time TIMESTAMP,
    delay_minutes  INTEGER,
    CONSTRAINT uk_affected_log_task UNIQUE (log_id, task_id)
);

-- ============ 预警 ============
CREATE TABLE IF NOT EXISTS shortage_alert (
    alert_id           VARCHAR(32) PRIMARY KEY,
    material_id        VARCHAR(32) NOT NULL REFERENCES material(material_id),
    required_date      DATE        NOT NULL,
    required_quantity  NUMERIC(12,2) NOT NULL,
    available_quantity NUMERIC(12,2) NOT NULL,
    shortage_quantity  NUMERIC(12,2) NOT NULL,
    risk_level         VARCHAR(10) CHECK (risk_level IN ('low','medium','high','critical')),
    status             VARCHAR(20) NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','resolved','ignored')),
    created_at         TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS delivery_alert (
    alert_id      VARCHAR(32) PRIMARY KEY,
    order_id      VARCHAR(32) NOT NULL REFERENCES sales_order(order_id),
    item_id       VARCHAR(32) REFERENCES sales_order_item(item_id),
    expected_date DATE,
    required_date DATE        NOT NULL,
    delay_days    INTEGER,
    alert_type    VARCHAR(20) CHECK (alert_type IN ('at_risk','delayed','critical')),
    status        VARCHAR(20) NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','resolved','ignored')),
    created_at    TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============ 索引 ============
CREATE INDEX IF NOT EXISTS idx_order_status     ON sales_order(status);
CREATE INDEX IF NOT EXISTS idx_order_date       ON sales_order(order_date);
CREATE INDEX IF NOT EXISTS idx_order_priority   ON sales_order(priority);
CREATE INDEX IF NOT EXISTS idx_order_item_order ON sales_order_item(order_id);
CREATE INDEX IF NOT EXISTS idx_material_type    ON material(material_type);
CREATE INDEX IF NOT EXISTS idx_inventory_mat    ON inventory(material_id);
CREATE INDEX IF NOT EXISTS idx_bom_product      ON bill_of_material(product_id);
CREATE INDEX IF NOT EXISTS idx_task_plan        ON production_task(plan_id);
CREATE INDEX IF NOT EXISTS idx_task_center      ON production_task(center_id);
CREATE INDEX IF NOT EXISTS idx_task_time        ON production_task(start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_task_order       ON production_task(order_id);
CREATE INDEX IF NOT EXISTS idx_shortage_status  ON shortage_alert(status);
CREATE INDEX IF NOT EXISTS idx_delivery_status  ON delivery_alert(status);

COMMIT;
