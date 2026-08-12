-- Migration 008: 产能分配明细表
-- 解决多计划并存时 capacity_calendar.planned_hours 覆盖问题
-- 每条记录代表一个计划在某工作中心某天的占用工时（含换型时间）
-- capacity_calendar.planned_hours 改为从本表汇总计算

CREATE TABLE IF NOT EXISTS capacity_allocation (
    allocation_id   VARCHAR(64) PRIMARY KEY DEFAULT gen_random_uuid()::text,
    plan_id         VARCHAR(32) NOT NULL,
    center_id       VARCHAR(32) NOT NULL,
    work_date       DATE NOT NULL,
    planned_hours   NUMERIC(10,2) NOT NULL DEFAULT 0,
    setup_hours     NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_alloc_plan FOREIGN KEY (plan_id) REFERENCES production_plan(plan_id) ON DELETE CASCADE,
    CONSTRAINT fk_alloc_center FOREIGN KEY (center_id) REFERENCES work_center(center_id),
    CONSTRAINT uk_alloc_plan_center_date UNIQUE (plan_id, center_id, work_date)
);

-- 索引：按中心和日期查询（产能负荷页面用）
CREATE INDEX idx_alloc_center_date ON capacity_allocation (center_id, work_date);

COMMENT ON TABLE capacity_allocation IS '产能分配明细：每个计划在各工作中心各日期的占用工时，planned_hours 从此表汇总';