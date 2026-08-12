"""
Celery 任务 + 插单端到端集成测试

测试策略：
    不启动真实 Celery worker（需要额外进程），而是直接同步调用任务函数，
    验证业务逻辑正确性。Celery 的序列化/异步机制由框架保证，不在此测试范围。

覆盖：
    1. run_scheduling    真实排产，结果与库内一致
    2. check_shortage    缺料检查，预警落库
    3. check_delivery_progress 交付检查
    4. generate_weekly_report  周报数据汇总
    5. insert_order_task 插单全链路：写单 -> 重排(固定占用) -> 扰动评分 -> 日志
"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.services.db import pg_query, pg_execute
from db_lock import acquire_test_lock, release_test_lock

RESTORE_CALENDAR = """UPDATE capacity_calendar SET available_hours =
    CASE WHEN EXTRACT(ISODOW FROM work_date) >= 6 THEN 0 ELSE 15.2 END"""


def cleanup():
    # 按外键依赖顺序：先删子表再删父表
    pg_execute("DELETE FROM insert_order_affected_task", None)
    pg_execute("DELETE FROM insert_order_log", None)
    pg_execute("DELETE FROM plan_run_log", None)
    pg_execute("DELETE FROM capacity_allocation", None)
    pg_execute("DELETE FROM production_task", None)
    pg_execute("DELETE FROM production_plan", None)
    pg_execute("DELETE FROM shortage_alert", None)
    pg_execute("DELETE FROM delivery_alert", None)
    pg_execute("DELETE FROM sales_order_item WHERE order_id LIKE 'ORD-TEST%'", None)
    pg_execute("DELETE FROM sales_order WHERE order_id LIKE 'ORD-TEST%'", None)
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours <> 0")
    pg_execute(RESTORE_CALENDAR, None)


def test_run_scheduling():
    """异步排产任务应走真实图并落库"""
    print("测试1: run_scheduling...")
    from src.services.celery_app import run_scheduling

    result = run_scheduling.apply(args=[{"user_input": "安排下周的生产计划", "request_type": "new_plan"}]).get()

    assert result["success"], f"排产失败: {result.get('error')}"
    assert result["plan_id"], "无计划ID"
    assert result["persisted"], "未落库"
    assert result["tasks_count"] > 0, "无排产任务"
    assert result["segments_count"] > 0, "无分段"
    assert result["total_hours"] > 0, "总工时为0"

    rows = pg_query("SELECT COUNT(*) c FROM production_task WHERE plan_id=%s",
                    (result["plan_id"],))
    assert rows[0]["c"] == result["segment_rows"], "库内段数与报告不一致"

    print(f"   OK plan={result['plan_id']}, 任务{result['tasks_count']}, "
          f"段{result['segments_count']}, {result['total_hours']}h, 已落库")


def test_check_shortage():
    """缺料检查任务应走图并落库预警"""
    print("测试2: check_shortage...")
    from src.services.celery_app import check_shortage

    result = check_shortage.apply().get()

    assert result["success"], f"缺料检查失败: {result.get('error')}"
    assert "shortage_count" in result
    assert result["run_id"], "无审计记录"

    print(f"   OK 缺口{result['shortage_count']}项, 预警{result['alerts_count']}条")


def test_check_delivery():
    """交付检查任务应走图并识别风险"""
    print("测试3: check_delivery_progress...")
    from src.services.celery_app import check_delivery_progress

    result = check_delivery_progress.apply().get()

    assert result["success"], f"交付检查失败: {result.get('error')}"
    assert "at_risk_count" in result
    assert result["run_id"], "无审计记录"

    print(f"   OK 风险{result['at_risk_count']}项, 严重{result['critical_count']}项")


def test_weekly_report():
    """周报应正常生成且结构完整"""
    print("测试4: generate_weekly_report...")
    from src.services.celery_app import run_scheduling, generate_weekly_report

    # 先排一版，确保库内有数据
    run_scheduling.apply(args=[{"user_input": "安排生产", "request_type": "new_plan"}]).get()

    result = generate_weekly_report.apply().get()

    assert result["success"], f"周报失败: {result.get('error')}"
    assert "period" in result
    assert "total_tasks" in result
    assert "total_hours" in result
    assert "plans" in result
    assert "tasks" in result
    # 周报默认查上周，测试环境上周可能无数据，验证结构完整即可

    print(f"   OK 周期={result['period']}, 结构完整, "
          f"总任务={result['total_tasks']}, 工时={result['total_hours']}h")


def test_insert_order_creates_order():
    """插单应先创建订单再重排"""
    print("测试5: 插单创建订单...")
    from src.services.celery_app import insert_order_task

    order_data = {
        "order_id": "ORD-TEST-001",
        "customer_id": "CUST-TEST",
        "required_date": (datetime.now() + timedelta(days=14)).date().isoformat(),
        "priority": 1,
        "items": [{
            "item_id": "ITEM-TEST-001",
            "product_id": "PROD-001",
            "quantity": 30,
            "due_date": (datetime.now() + timedelta(days=10)).date().isoformat(),
        }],
    }

    result = insert_order_task.apply(args=[{**order_data, "sync": True}]).get()

    assert result["success"], f"插单失败: {result.get('error')}"
    assert result["order_id"] == "ORD-TEST-001"

    rows = pg_query("SELECT * FROM sales_order WHERE order_id=%s", ("ORD-TEST-001",))
    assert rows, "订单未写入"
    assert rows[0]["status"] == "confirmed"

    items = pg_query("SELECT * FROM sales_order_item WHERE order_id=%s", ("ORD-TEST-001",))
    assert items, "订单明细未写入"
    assert float(items[0]["quantity"]) == 30

    print(f"   OK 订单已创建, 新计划={result['new_plan_id']}, "
          f"扰动={result['disruption_score']}/{result['disruption_level']}")


def test_insert_order_disruption():
    """插单扰动评分应反映实际影响"""
    print("测试6: 扰动评分...")
    from src.services.celery_app import insert_order_task, run_scheduling

    # 先排一版基准计划
    base = run_scheduling.apply(args=[{"user_input": "安排生产", "request_type": "new_plan"}]).get()
    assert base["success"]

    # 插入紧急大单
    order_data = {
        "order_id": "ORD-TEST-002",
        "customer_id": "CUST-TEST",
        "required_date": (datetime.now() + timedelta(days=7)).date().isoformat(),
        "priority": 1,
        "items": [{
            "item_id": "ITEM-TEST-002",
            "product_id": "PROD-001",
            "quantity": 150,
            "due_date": (datetime.now() + timedelta(days=5)).date().isoformat(),
        }],
    }

    result = insert_order_task.apply(args=[{**order_data, "sync": True}]).get()

    assert result["success"], f"插单失败: {result.get('error')}"
    assert result["disruption_score"] > 0, "扰动评分应为正"
    assert result["disruption_level"] in ("low", "medium", "high")
    assert result["original_plan_id"], "无原始计划ID"
    assert result["new_plan_id"], "无新计划ID"

    print(f"   OK 扰动={result['disruption_score']}/{result['disruption_level']}, "
          f"原={result['original_plan_id']}, 新={result['new_plan_id']}")


def test_insert_order_log_written():
    """插单日志及受影响任务应落库"""
    print("测试7: 插单日志落库...")
    from src.services.celery_app import insert_order_task, run_scheduling

    # 先有基准计划
    run_scheduling.apply(args=[{"user_input": "安排生产", "request_type": "new_plan"}]).get()

    order_data = {
        "order_id": "ORD-TEST-003",
        "customer_id": "CUST-TEST",
        "required_date": (datetime.now() + timedelta(days=10)).date().isoformat(),
        "priority": 1,
        "items": [{
            "item_id": "ITEM-TEST-003",
            "product_id": "PROD-002",
            "quantity": 80,
            "due_date": (datetime.now() + timedelta(days=8)).date().isoformat(),
        }],
    }

    result = insert_order_task.apply(args=[{**order_data, "sync": True}]).get()

    assert result["success"]
    assert result["log_id"], "插单日志未落库"

    logs = pg_query("SELECT * FROM insert_order_log WHERE log_id=%s", (result["log_id"],))
    assert logs, "日志未找到"
    log = logs[0]
    assert log["order_id"] == "ORD-TEST-003"
    assert log["disruption_score"] is not None
    assert log["disruption_level"] in ("low", "medium", "high")
    assert log["status"] == "completed"

    affected = pg_query(
        "SELECT * FROM insert_order_affected_task WHERE log_id=%s", (result["log_id"],))

    print(f"   OK 日志={result['log_id']}, 扰动={log['disruption_score']}, "
          f"受影响={len(affected)}条")


def test_insert_order_existing_order():
    """对已存在的订单插单不应重复创建"""
    print("测试8: 已存在订单插单...")
    from src.services.celery_app import insert_order_task

    order_data = {
        "order_id": "ORD-TEST-004",
        "customer_id": "CUST-TEST",
        "required_date": (datetime.now() + timedelta(days=14)).date().isoformat(),
        "priority": 1,
        "items": [{
            "item_id": "ITEM-TEST-004",
            "product_id": "PROD-001",
            "quantity": 20,
            "due_date": (datetime.now() + timedelta(days=10)).date().isoformat(),
        }],
    }
    r1 = insert_order_task.apply(args=[{**order_data, "sync": True}]).get()
    assert r1["success"]

    r2 = insert_order_task.apply(args=[{**order_data, "sync": True}]).get()
    assert r2["success"], f"二次插单失败: {r2.get('error')}"

    count = pg_query("SELECT COUNT(*) c FROM sales_order WHERE order_id=%s",
                     ("ORD-TEST-004",))[0]["c"]
    assert count == 1, f"订单被重复创建: {count}"

    print(f"   OK 二次插单未重复创建, 计划 {r1['new_plan_id']} / {r2['new_plan_id']}")


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_run_scheduling, test_check_shortage, test_check_delivery,
             test_weekly_report, test_insert_order_creates_order,
             test_insert_order_disruption, test_insert_order_log_written,
             test_insert_order_existing_order]
    try:
        cleanup()
        for t in tests:
            cleanup()
            try:
                t()
                passed += 1
            except AssertionError as e:
                print(f"   FAIL {t.__name__}: {e}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"   ERROR {t.__name__}: {type(e).__name__}: {e}")
        print(f"\n结果: {passed}/{len(tests)} 通过")
        cleanup()
        print("测试数据已清理")
    finally:
        release_test_lock(_lock)
    sys.exit(0 if passed == len(tests) else 1)