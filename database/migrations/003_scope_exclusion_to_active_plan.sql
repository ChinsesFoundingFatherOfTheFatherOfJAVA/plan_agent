-- ============================================================
-- 003_scope_exclusion_to_active_plan.sql
-- 目的：让「同中心时间不重叠」的排他约束只作用于计划内部，支持多方案并存
--
-- 问题：
--   001 建立的排他约束为全局生效：
--       EXCLUDE USING gist (center_id WITH =, tsrange(start_time,end_time) WITH &&)
--   它不区分 plan_id，导致：
--     1. 无法为同一时间窗口生成第二个候选方案（插单前后对比、A/B 方案评估）
--     2. insert_order_log 同时保留 original_plan_id 与 new_plan_id，
--        本就要求两版计划共存，与全局约束直接矛盾
--     3. 草稿计划会阻塞正式计划写入
--
-- 方案：
--   把 plan_id、plan_version 纳入相等比较维度，即
--   「同一计划同一版本内不得重叠」，跨计划/跨版本允许重叠（不同候选方案）。
--   已取消的任务（cancelled）不参与占用。
--   「同一时刻实际只执行一版」由 production_plan 的部分唯一索引保证。
-- ============================================================

-- 1. 去掉全局排他约束
ALTER TABLE production_task DROP CONSTRAINT IF EXISTS excl_center_time_overlap;

-- 2. 改为「计划版本内」排他
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'excl_center_time_overlap_in_plan') THEN
        ALTER TABLE production_task ADD CONSTRAINT excl_center_time_overlap_in_plan
            EXCLUDE USING gist (
                plan_id      WITH =,
                plan_version WITH =,
                center_id    WITH =,
                tsrange(start_time, end_time) WITH &&
            ) WHERE (status <> 'cancelled');
    END IF;
END $$;

-- 3. 业务上同一时刻只允许一个「生效」计划（confirmed / executing）
--    草稿（draft）与历史（completed/cancelled）可以有多个
CREATE UNIQUE INDEX IF NOT EXISTS uk_plan_active
    ON production_plan ((status IN ('confirmed','executing')))
    WHERE status IN ('confirmed', 'executing');

COMMENT ON CONSTRAINT excl_center_time_overlap_in_plan ON production_task IS
    '同一计划版本内同一工作中心的任务段不得时间重叠；跨计划允许重叠（不同候选方案）';