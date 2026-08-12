"""
API 集成测试

使用 FastAPI TestClient（基于 httpx），不需要启动真实 HTTP 服务。
所有端点直接在进程内调用，验证请求/响应结构与库内数据一致性。
"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from src.api.app import app
from src.services.db import pg_query, pg_execute
from db_lock import acquire_test_lock, release_test_lock

RESTORE_CALENDAR = """UPDATE capacity_calendar SET available_hours =
    CASE WHEN EXTRACT(ISODOW FROM work_date) >= 6 THEN 0 ELSE 15.2 END"""

client = TestClient(app)


def cleanup():
    pg_execute("DELETE FROM insert_order_affected_task", None)
    pg_execute("DELETE FROM insert_order_log", None)
    pg_execute("DELETE FROM plan_run_log", None)
    pg_execute("DELETE FROM production_task", None)
    pg_execute("DELETE FROM production_plan", None)
    pg_execute("DELETE FROM delivery_alert", None)
    pg_execute("DELETE FROM shortage_alert", None)
    pg_execute("DELETE FROM sales_order_item WHERE order_id LIKE 'ORD-TEST%'", None)
    pg_execute("DELETE FROM sales_order WHERE order_id LIKE 'ORD-TEST%'", None)
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours <> 0")
    pg_execute(RESTORE_CALENDAR, None)


def test_knowledge_import_and_search():
    """知识库导入与检索"""
    print("测试13: knowledge import & search...")
    # 导入种子知识
    r = client.post("/api/knowledge/import")
    assert r.status_code == 200
    data = r.json()
    assert data["imported"] > 0, "未导入任何知识"

    # 搜索
    r2 = client.get("/api/knowledge/search", params={"q": "紧急订单优先处理", "top_k": 3})
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["count"] > 0, "搜索无结果"
    assert any("紧急" in r["content"] or "优先" in r["content"] for r in data2["results"]),         "top结果应与紧急订单相关"

    # 按类型过滤
    r3 = client.get("/api/knowledge/search",
                    params={"q": "产能", "top_k": 5, "doc_type": "capacity_rule"})
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["count"] > 0
    assert all(r["type"] == "capacity_rule" for r in data3["results"])

    # context
    r4 = client.get("/api/knowledge/context", params={"q": "插单影响", "top_k": 2})
    assert r4.status_code == 200
    data4 = r4.json()
    assert data4["length"] > 0

    # stats
    r5 = client.get("/api/knowledge/stats")
    assert r5.status_code == 200
    data5 = r5.json()
    assert data5["document_count"] > 0

    print(f"   OK 导入{data['imported']}篇, 搜索返回{data2['count']}条, "
          f"backend={data5['backend']}, 文档数={data5['document_count']}")


def test_health():
    """健康检查应返回 PG/Redis 状态"""
    print("测试1: health...")
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert data["checks"]["postgres"] == "ok"
    assert data["checks"]["redis"] == "ok"
    print("   OK PG + Redis 正常")


def test_stats():
    """统计概览应返回各表行数"""
    print("测试2: stats...")
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    assert "table_counts" in data
    assert data["table_counts"]["routing"] == 4
    assert "plan_status" in data
    assert "active_alerts" in data
    print(f"   OK routing={data['table_counts']['routing']}条, "
          f"订单={data['table_counts']['sales_order']}个")


def test_sync_scheduling():
    """同步排产应返回完整结果并落库"""
    print("测试3: sync scheduling...")
    r = client.post("/api/scheduling/sync", json={"user_input": "安排下周的生产计划", "request_type": "new_plan"})
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["plan_id"]
    assert data["persisted"] is True
    assert data["tasks_count"] > 0
    assert data["segments_count"] > 0
    assert data["total_hours"] > 0
    assert data["solver_status"] in ("OPTIMAL", "FEASIBLE")
    assert data["output"]
    print(f"   OK plan={data['plan_id']}, 任务{data['tasks_count']}, "
          f"段{data['segments_count']}, {data['total_hours']}h")


def test_get_latest_plan():
    """查最新计划应返回计划头与逻辑任务"""
    print("测试4: latest plan...")
    # 先排一版
    client.post("/api/scheduling/sync", json={"user_input": "安排生产", "request_type": "new_plan"})
    r = client.get("/api/plans/latest")
    assert r.status_code == 200
    data = r.json()
    assert data["plan"]["plan_id"]
    assert data["total_segments"] > 0
    assert len(data["logical_tasks"]) > 0
    task = data["logical_tasks"][0]
    assert task["segments"]
    assert task["start_time"]
    assert task["end_time"]
    assert task["total_hours"] > 0
    print(f"   OK plan={data['plan']['plan_id']}, "
          f"逻辑任务{len(data['logical_tasks'])}个, "
          f"总段{data['total_segments']}")


def test_get_plan_by_id():
    """按ID查计划"""
    print("测试5: plan by id...")
    # 先排一版
    sync = client.post("/api/scheduling/sync", json={"user_input": "安排生产", "request_type": "new_plan"})
    plan_id = sync.json()["plan_id"]
    r = client.get(f"/api/plans/{plan_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["plan"]["plan_id"] == plan_id
    print(f"   OK {plan_id}: {data['total_segments']}段")


def test_get_plan_not_found():
    """查不存在的计划应返回 404"""
    print("测试6: plan not found...")
    r = client.get("/api/plans/PLAN-NONEXIST")
    assert r.status_code == 404
    print("   OK 404")


def test_confirm_plan():
    """确认计划：draft -> confirmed"""
    print("测试7: confirm plan...")
    sync = client.post("/api/scheduling/sync", json={"user_input": "安排生产", "request_type": "new_plan"})
    plan_id = sync.json()["plan_id"]
    r = client.post(f"/api/plans/{plan_id}/confirm")
    assert r.status_code == 200
    data = r.json()
    assert data["confirmed"] == 1
    # 验证库内状态
    row = pg_query("SELECT status FROM production_plan WHERE plan_id=%s", (plan_id,))[0]
    assert row["status"] == "confirmed"
    tasks = pg_query("SELECT DISTINCT status FROM production_task WHERE plan_id=%s", (plan_id,))
    assert all(t["status"] == "scheduled" for t in tasks)
    print(f"   OK {plan_id} -> confirmed, tasks -> scheduled")


def test_confirm_nonexistent():
    """确认不存在的计划应返回 404"""
    print("测试8: confirm nonexistent...")
    r = client.post("/api/plans/PLAN-NONEXIST/confirm")
    assert r.status_code == 404
    print("   OK 404")


def test_insert_order():
    """插单应返回 task_id 且订单已创建"""
    print("测试9: insert order...")
    order_data = {
        "order_id": "ORD-TEST-API-001",
        "customer_id": "CUST-TEST",
        "required_date": (datetime.now() + timedelta(days=14)).date().isoformat(),
        "priority": 1,
        "items": [{
            "item_id": "ITEM-TEST-API-001",
            "product_id": "PROD-001",
            "quantity": 30,
            "due_date": (datetime.now() + timedelta(days=10)).date().isoformat(),
        }],
    }
    order_data["sync"] = True
    r = client.post("/api/orders/insert", json=order_data)
    assert r.status_code == 200
    result = r.json()
    assert result["success"] is True
    assert result["order_id"] == "ORD-TEST-API-001"
    assert result["new_plan_id"]
    print(f"   OK 订单已创建, plan={result['new_plan_id']}, "
          f"扰动={result['disruption_score']}/{result['disruption_level']}")


def test_active_tasks():
    """查活跃任务"""
    print("测试10: active tasks...")
    # 先排一版
    client.post("/api/scheduling/sync", json={"user_input": "安排生产", "request_type": "new_plan"})
    r = client.get("/api/tasks/active")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] > 0
    assert len(data["tasks"]) == data["count"]
    # 按中心过滤
    r2 = client.get("/api/tasks/active?center_id=WC-001")
    assert r2.status_code == 200
    data2 = r2.json()
    assert all(t["center_id"] == "WC-001" for t in data2["tasks"])
    print(f"   OK {data['count']}个活跃任务, WC-001有{data2['count']}个")


def test_shortage_check():
    """缺料检查应返回 task_id"""
    print("测试11: shortage check...")
    r = client.post("/api/shortage/check?sync=true")
    assert r.status_code == 200
    result = r.json()
    assert result["success"] is True
    print(f"   OK 缺口{result['shortage_count']}项")


def test_delivery_check():
    """交付检查应返回 task_id"""
    print("测试12: delivery check...")
    r = client.post("/api/delivery/check?sync=true")
    assert r.status_code == 200
    result = r.json()
    assert result["success"] is True
    print(f"   OK 风险{result['at_risk_count']}项")


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_health, test_stats, test_sync_scheduling, test_get_latest_plan,
             test_get_plan_by_id, test_get_plan_not_found, test_confirm_plan,
             test_confirm_nonexistent, test_insert_order, test_active_tasks,
             test_shortage_check, test_delivery_check, test_knowledge_import_and_search]
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