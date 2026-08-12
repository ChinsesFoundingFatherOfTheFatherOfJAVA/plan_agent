"""
工艺路线（routing）集成测试

验证：
    1. data_fetch 从 routing 表读取工时，不再用 0.5h/件 硬编码
    2. 求解器按工作中心差异化工时排产（PROD-001 在 WC-001 更快）
    3. 求解器倾向于把任务分配到工时更短的工作中心
    4. allowed_centers 只包含有 routing 的中心
"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agent.graphs.plan_graph import build_plan_graph
from src.models.state import create_initial_state
from src.services.db import pg_query, pg_execute
from db_lock import acquire_test_lock, release_test_lock

RESTORE_CALENDAR = """UPDATE capacity_calendar SET available_hours =
    CASE WHEN EXTRACT(ISODOW FROM work_date) >= 6 THEN 0 ELSE 15.2 END"""


class MockLLM:
    def __init__(self, request_type="new_plan"):
        self.request_type = request_type

    def invoke(self, messages):
        import json

        class R:
            content = json.dumps({
                "request_type": self.request_type,
                "params": {},
                "confidence": 0.95,
                "reasoning": "routing test",
            }, ensure_ascii=False)

        return R()


def cleanup():
    pg_execute("DELETE FROM plan_run_log", None)
    pg_execute("DELETE FROM capacity_allocation", None)
    pg_execute("DELETE FROM production_task", None)
    pg_execute("DELETE FROM production_plan", None)
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours <> 0")
    pg_execute(RESTORE_CALENDAR, None)


def run():
    graph = build_plan_graph(MockLLM("new_plan"))
    return graph.invoke(create_initial_state("安排下周的生产计划"))


def test_routing_loaded():
    """data_fetch 应从 routing 表加载工艺路线"""
    print("测试1: routing 数据加载...")
    r = run()
    routing = r.get("routing") or {}
    assert routing, "routing 未加载"
    assert "PROD-001" in routing, "缺少 PROD-001 路线"
    assert "PROD-002" in routing, "缺少 PROD-002 路线"
    # PROD-001 应有 WC-001(0.1) 和 WC-002(0.2) 两条路线
    p1 = {x["center_id"]: x["hours_per_unit"] for x in routing["PROD-001"]}
    assert p1 == {"WC-001": 0.1, "WC-002": 0.2}, f"PROD-001 路线不符: {p1}"
    p2 = {x["center_id"]: x["hours_per_unit"] for x in routing["PROD-002"]}
    assert p2 == {"WC-001": 0.3, "WC-002": 0.4}, f"PROD-002 路线不符: {p2}"
    print(f"   OK PROD-001: {p1}, PROD-002: {p2}")


def test_no_hardcoded_half():
    """工时不应再是 0.5h/件 的倍数"""
    print("测试2: 工时不再硬编码...")
    r = run()
    tasks = r.get("production_tasks") or []
    assert tasks, "无排产结果"
    for t in tasks:
        # 0.5h/件 时：100件=50h, 200件=100h, 40件=20h, 50件=25h
        # routing 后：100件最快=10h, 200件最快=20h, 40件最快=12h, 50件最快=15h
        old_val = t["quantity"] * 0.5
        assert t["duration_hours"] != old_val, \
            f"{t['task_id']} 工时仍为 0.5h/件: {t['duration_hours']}"
    print(f"   OK {len(tasks)}个任务工时均已按 routing 计算")


def test_prefers_faster_center():
    """
    求解器应倾向于把任务分配到工时更短的工作中心

    PROD-001 在 WC-001(0.1h) 比 WC-002(0.2h) 快一倍，
    shortest_job 策略下应优先选 WC-001。
    """
    print("测试3: 偏好更快工作中心...")
    r = run()
    tasks = r.get("production_tasks") or []
    # 统计 PROD-001 的任务分配
    p1_tasks = [t for t in tasks if t["product_id"] == "PROD-001"]
    assert p1_tasks, "无 PROD-001 任务"
    # 大多数 PROD-001 应在 WC-001（除非 WC-001 满了）
    wc001_count = sum(1 for t in p1_tasks if t["center_id"] == "WC-001")
    print(f"   PROD-001 任务: {len(p1_tasks)}个, 其中 {wc001_count}个在 WC-001(0.1h/件)")
    # 至少有一个在 WC-001
    assert wc001_count >= 1, f"PROD-001 应至少有一个在 WC-001, 实际分配: {[(t['task_id'],t['center_id']) for t in p1_tasks]}"


def test_per_center_duration_correct():
    """落库的 duration_hours 应等于分配中心的 routing 工时 x 数量"""
    print("测试4: 按中心工时精确计算...")
    r = run()
    tasks = r.get("production_tasks") or []
    routing = r.get("routing") or {}
    for t in tasks:
        routes = {x["center_id"]: x["hours_per_unit"] for x in routing.get(t["product_id"], [])}
        expected = routes.get(t["center_id"]) * t["quantity"]
        # segments 的工时之和应等于该任务的总工时
        seg_hours = sum(s["duration_hours"] for s in t.get("segments", []))
        assert abs(seg_hours - expected) < 0.1, \
            f"{t['task_id']} 段工时{seg_hours} != 期望{expected} " \
            f"(中心{t['center_id']} rate={routes.get(t['center_id'])} qty={t['quantity']})"
    print(f"   OK {len(tasks)}个任务的段工时均按 routing 精确计算")


def test_db_duration_matches():
    """库内的 duration_hours 应与求解结果一致"""
    print("测试5: 库内工时一致...")
    r = run()
    plan_id = r["plan_id"]
    tasks = r.get("production_tasks") or []
    for t in tasks:
        segs = t.get("segments") or []
        for seg in segs:
            rows = pg_query(
                """SELECT duration_hours FROM production_task
                   WHERE plan_id=%s AND parent_task_id=%s AND segment_index=%s""",
                (plan_id, t["task_id"], seg["segment_index"]))
            assert rows, f"库内未找到段 {t['task_id']}-S{seg['segment_index']}"
            db_h = float(rows[0]["duration_hours"])
            assert abs(db_h - seg["duration_hours"]) < 0.01, \
                f"段{t['task_id']}-S{seg['segment_index']}: 库内{db_h} != 输出{seg['duration_hours']}"
    print(f"   OK {len(tasks)}个任务所有段工时与库内一致")


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_routing_loaded, test_no_hardcoded_half, test_prefers_faster_center,
             test_per_center_duration_correct, test_db_duration_matches]
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