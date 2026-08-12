-- ============================================================
-- 006_seed_setup_time.sql
-- 目的：为 routing 表填充换型时间种子数据
--
-- 换型时间（setup time）含义：
--   在某个工作中心上，从上一个任务切换到新任务时需要的准备时间
--   （换模具、调机、首件检验等）。该时间内工作中心被占用但不产出。
--
-- 当前模型：固定换型时间（不依赖前序产品）
--   每个产品在每个工作中心有一个固定的换型时间
--   后续可升级为 sequence-dependent setup（依赖前后产品组合）
--
-- 种子数据（模拟值）：
--   PROD-001 @ WC-001: 30min  PROD-001 @ WC-002: 20min
--   PROD-002 @ WC-001: 45min  PROD-002 @ WC-002: 25min
--   逻辑：PROD-002 结构更复杂，换型时间更长；WC-001 是主力产线，模具更重
-- ============================================================

UPDATE routing SET setup_time_minutes = 30 WHERE product_id = 'PROD-001' AND center_id = 'WC-001';
UPDATE routing SET setup_time_minutes = 20 WHERE product_id = 'PROD-001' AND center_id = 'WC-002';
UPDATE routing SET setup_time_minutes = 45 WHERE product_id = 'PROD-002' AND center_id = 'WC-001';
UPDATE routing SET setup_time_minutes = 25 WHERE product_id = 'PROD-002' AND center_id = 'WC-002';

-- 验证
SELECT product_id, center_id, hours_per_unit, setup_time_minutes FROM routing ORDER BY product_id, center_id;