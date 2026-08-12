-- ============================================================
-- 002_add_task_segments.sql
-- 目的：支持「跨天任务分段落库」
--
-- 背景：
--   排产求解器已改为日历感知模式，一个逻辑任务（如 200 件 = 100 工时）
--   会被拆成多段真实工作时间（每天一段，跳过夜间与周末）。
--   原 production_task 表一行代表一个任务，且存在排他约束
--       EXCLUDE USING gist (center_id WITH =, tsrange(start_time,end_time) WITH &&)
--   若把跨天任务写成 [首段开始, 末段结束] 的单行区间，
--   该区间会覆盖夜间与周末，与同中心其他任务必然误判重叠。
--
-- 方案：一段一行。
--   segment_index  段序号（0 起）
--   parent_task_id 逻辑任务号，同一任务的各段共享
--   segment_count  该逻辑任务的总段数
--   task_id 变为「段主键」（形如 TASK-001-S0）
--
-- 这样排他约束天然成立，且甘特图可精确渲染每段真实生产时间。
-- ============================================================

ALTER TABLE production_task
    ADD COLUMN IF NOT EXISTS parent_task_id  VARCHAR(32),
    ADD COLUMN IF NOT EXISTS segment_index   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS segment_count   INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS duration_hours  NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS due_date        DATE,
    ADD COLUMN IF NOT EXISTS tardiness_hours NUMERIC(10,2) NOT NULL DEFAULT 0;

-- 历史数据回填：原有单段任务的 parent_task_id 即自身
UPDATE production_task SET parent_task_id = task_id WHERE parent_task_id IS NULL;

ALTER TABLE production_task
    ALTER COLUMN parent_task_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_segment_index') THEN
        ALTER TABLE production_task ADD CONSTRAINT ck_task_segment_index
            CHECK (segment_index >= 0 AND segment_count >= 1 AND segment_index < segment_count);
    END IF;

    -- 同一计划内，同一逻辑任务的段序号唯一
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uk_task_parent_segment') THEN
        ALTER TABLE production_task ADD CONSTRAINT uk_task_parent_segment
            UNIQUE (plan_id, parent_task_id, segment_index);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_task_parent   ON production_task (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_plan_ver ON production_task (plan_id, plan_version);

COMMENT ON COLUMN production_task.parent_task_id  IS '逻辑任务号，跨天任务的各段共享此值';
COMMENT ON COLUMN production_task.segment_index   IS '段序号，0 起';
COMMENT ON COLUMN production_task.segment_count   IS '该逻辑任务被拆分的总段数';
COMMENT ON COLUMN production_task.duration_hours  IS '该段的实际生产工时';
COMMENT ON COLUMN production_task.tardiness_hours IS '该逻辑任务的超期工时，0 表示按期';

-- ============================================================
-- 排产运行审计表：记录每次求解的输入规模、策略与结果
-- 用途：追溯「为什么这版计划是这样排的」，以及求解性能监控
-- ============================================================
CREATE TABLE IF NOT EXISTS plan_run_log (
    run_id            VARCHAR(32) PRIMARY KEY,
    plan_id           VARCHAR(32) REFERENCES production_plan(plan_id) ON DELETE SET NULL,
    request_type      VARCHAR(20)  NOT NULL,
    user_input        TEXT,
    strategy          VARCHAR(20),
    solver_status     VARCHAR(20),
    tasks_total       INTEGER NOT NULL DEFAULT 0,
    tasks_scheduled   INTEGER NOT NULL DEFAULT 0,
    tasks_unscheduled INTEGER NOT NULL DEFAULT 0,
    tardy_tasks       INTEGER NOT NULL DEFAULT 0,
    solve_seconds     NUMERIC(10,3),
    validation_passed BOOLEAN,
    conflicts_count   INTEGER NOT NULL DEFAULT 0,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_run_log_plan    ON plan_run_log (plan_id);
CREATE INDEX IF NOT EXISTS idx_run_log_created ON plan_run_log (created_at DESC);

COMMENT ON TABLE plan_run_log IS '排产运行审计日志，每次 Agent 求解一条';