-- 智能排产计划Agent - 测试种子数据（PostgreSQL）
-- 幂等：使用 ON CONFLICT DO NOTHING，可重复执行
-- TODO: 真实业务数据接入后（ERP/WMS/MES），此文件仅用于本地开发与联调

BEGIN;

-- ============ 工作中心 ============
INSERT INTO work_center (center_id, center_name, capacity_per_day, efficiency_rate, status) VALUES
    ('WC-001', '生产线1', 16.00, 0.95, 'active'),
    ('WC-002', '生产线2', 16.00, 0.90, 'active'),
    ('WC-003', '生产线3', 8.00,  0.85, 'maintenance')
ON CONFLICT (center_id) DO NOTHING;

-- ============ 物料 ============
INSERT INTO material (material_id, material_name, material_type, unit, safety_stock, lead_time_days, supplier_id) VALUES
    ('MAT-001', '原材料A', 'raw',  'kg', 100.00, 3, 'SUP-001'),
    ('MAT-002', '原材料B', 'raw',  'kg',  50.00, 5, 'SUP-001'),
    ('MAT-003', '半成品C', 'semi', '个', 200.00, 2, 'SUP-002')
ON CONFLICT (material_id) DO NOTHING;

-- ============ 库存 ============
INSERT INTO inventory (inventory_id, material_id, warehouse_id, quantity_on_hand, quantity_reserved, quantity_on_order) VALUES
    ('INV-001', 'MAT-001', 'WH-001',  500.00, 100.00, 200.00),
    ('INV-002', 'MAT-002', 'WH-001',  300.00,  50.00,   0.00),
    ('INV-003', 'MAT-003', 'WH-001', 1000.00, 200.00, 500.00)
ON CONFLICT (inventory_id) DO NOTHING;

-- ============ BOM（与 shortage_service 的默认假设保持一致）============
INSERT INTO bill_of_material (bom_id, product_id, material_id, quantity_required) VALUES
    ('BOM-001', 'PROD-001', 'MAT-001', 2.0000),
    ('BOM-002', 'PROD-001', 'MAT-002', 1.0000),
    ('BOM-003', 'PROD-002', 'MAT-001', 1.0000),
    ('BOM-004', 'PROD-002', 'MAT-003', 3.0000)
ON CONFLICT (bom_id) DO NOTHING;

-- ============ 产能日历（未来14天，周末不排产）============
INSERT INTO capacity_calendar (calendar_id, center_id, work_date, available_hours, planned_hours, actual_hours)
SELECT
    'CAL-' || wc.center_id || '-' || TO_CHAR(d::date, 'YYYYMMDD'),
    wc.center_id,
    d::date,
    CASE
        WHEN EXTRACT(ISODOW FROM d) >= 6 THEN 0
        WHEN wc.status <> 'active'       THEN 0
        ELSE ROUND(wc.capacity_per_day * wc.efficiency_rate, 2)
    END,
    0, 0
FROM work_center wc
CROSS JOIN generate_series(CURRENT_DATE, CURRENT_DATE + INTERVAL '13 day', INTERVAL '1 day') AS d
ON CONFLICT (center_id, work_date) DO NOTHING;

-- ============ 订单（交期基于 CURRENT_DATE，避免数据过期）============
INSERT INTO sales_order (order_id, customer_id, order_date, required_date, priority, status, total_amount) VALUES
    ('ORD-001', 'CUST-001', CURRENT_DATE, CURRENT_DATE + 10, 2, 'confirmed', 150000.00),
    ('ORD-002', 'CUST-002', CURRENT_DATE, CURRENT_DATE + 15, 1, 'confirmed', 320000.00),
    ('ORD-003', 'CUST-003', CURRENT_DATE, CURRENT_DATE +  4, 1, 'confirmed',  80000.00)
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO sales_order_item (item_id, order_id, product_id, quantity, unit_price, due_date) VALUES
    ('ITEM-001', 'ORD-001', 'PROD-001', 100.00, 1000.00, CURRENT_DATE + 10),
    ('ITEM-002', 'ORD-001', 'PROD-002',  50.00, 1000.00, CURRENT_DATE + 12),
    ('ITEM-003', 'ORD-002', 'PROD-001', 200.00, 1600.00, CURRENT_DATE + 15),
    ('ITEM-004', 'ORD-003', 'PROD-002',  40.00, 2000.00, CURRENT_DATE +  4)
ON CONFLICT (item_id) DO NOTHING;

COMMIT;
