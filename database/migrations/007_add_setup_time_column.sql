-- ============================================================
-- 007_add_setup_time_column.sql
-- 目的：production_task 增加换型时间列
--
-- 换型时间（setup time）是工作中心从上一个任务切换到当前任务时
-- 需要的准备时间（换模具、调机等）。该时间内工作中心被占用但不产出。
--
-- 当前模型：固定换型时间，每个逻辑任务有一个值，各段共享。
-- 后续升级为 sequence-dependent setup 时，可能需要按段记录不同的换型时间。
-- ============================================================

ALTER TABLE production_task
    ADD COLUMN IF NOT EXISTS setup_time_minutes INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN production_task.setup_time_minutes IS '换型时间（分钟），工作中心切换到该任务时的准备时间';