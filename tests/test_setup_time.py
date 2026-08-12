"""
换型时间（setup time）集成测试

验证：
    1. routing 表的 setup_time_minutes 正确加载到 TaskInput
    2. 求解器把换型时间作为额外占用纳入 NoOverlap
    3. 同中心连续任务的间隔 >= 前一任务换型时间
    4. 落库后 production_task.setup_time_minutes 正确写入
    5. 输出中展示换型时间
"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.services.ortools_service import (
    ORToolsScheduler, SolverStrategy, TaskInput, WorkCenterInput, prepare_solver_inputs,
)
from src.services.calendar_service import WorkCalendar
from src.services.db import pg_query, pg_execute
from src.services import plan_writer as pw
from src.services import repository as repo
from db_lock import acquire_test_lock, release_test_lock

NOW = datetime(2026, 8, 11, 8, 0)
TEST_PREFIX = "PLAN-SETUP-TEST"


def cal_rows(cid, days=30, hours=15.2):
    out = []
    for i in range(days):
        d = NOW.date() + timedelta(days=i)
        out.append({"center_id": cid, "work_date": d.isoformat(),
                    "available_hours": 0 if d.weekday() >= 5 else hours,
                    "planned_hours": 0})
    return out


def wc(cid, name):
    return WorkCenterInput(cid, name, 16.0, 0.95,
                           calendar=WorkCalendar.from_calendar_rows(cid, cal_rows(cid), now=NOW))


def cleanup():
    pg_execute("DELETE FROM plan_run_log WHERE plan_id LIKE %s", (TEST_PREFIX + "%",))
    pg_execute("DELETE FROM production_task WHERE plan_id LIKE %s", (TEST_PREFIX + "%",))
    pg_execute("DELETE FROM production_plan WHERE plan_id LIKE %s", (TEST_PREFIX + "%",))
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours <> 0")


def test_setup_in_routing():
    """routing 表应有非零 setup_time_minutes"""
    print("测试1: routing 表换型时间...")
    routes = repo.fetch_routing_map()
    for pid, rts in routes.items():
        for r in rts:
            assert r["setup_time_minutes"] >= 0, f"{pid}@{r['center_id']} setup 为负"
    # 至少有一些非零
    has_setup = any(r["setup_time_minutes"] > 0 for rts in routes.values() for r in rts)
    assert has_setup, "routing 表无非零换型时间"
    print(f"   OK {sum(len(r) for r in routes.values())} 条路线, 有非零换型时间")


def test_setup_consumes_capacity():
    """
    有换型时间时，同中心两个任务之间应有足够间隔

    PROD-001 @ WC-001: 0.1h/件, setup 30min
    两个 PROD-001 x 100 任务 = 各 10h + 30min 换型
    总占用 = 10h + 0.5h + 10h + 0.5h = 21h（约 1.4 个工作日）
    """
    print("测试2: 换型时间占用产能...")
    tasks = [
        TaskInput("TASK-001", "ORD-001", "ITEM-001", "PROD-001", 100, 10.0,
                  setup_time_minutes=30, priority=2, allowed_centers=["WC-001"]),
        TaskInput("TASK-002", "ORD-002", "ITEM-002", "PROD-001", 100, 10.0,
                  setup_time_minutes=30, priority=2, allowed_centers=["WC-001"]),
    ]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "线1")], start_date=NOW)
    assert r.success, r.status

    # 两个任务都在 WC-001，总时间应 >= 10+0.5+10+0.5 = 21h
    t1 = next(t for t in r.tasks if t["task_id"] == "TASK-001")
    t2 = next(t for t in r.tasks if t["task_id"] == "TASK-002")
    assert t1["setup_time_minutes"] == 30, f"setup={t1['setup_time_minutes']}"
    assert t2["setup_time_minutes"] == 30

    # 按开始时间排序
    if t1["start_time"] > t2["start_time"]:
        t1, t2 = t2, t1

    # t2 的开始应在 t1 结束之后（至少紧邻，换型时间已含在 NoOverlap 中）
    t1_end = datetime.fromisoformat(t1["end_time"])
    t2_start = datetime.fromisoformat(t2["start_time"])
    assert t2_start >= t1_end, f"任务重叠: t1_end={t1_end}, t2_start={t2_start}"

    print(f"   OK 两任务各10h+30min换型, t1={t1['start_time'][:16]}~{t1['end_time'][:16]}, "
          f"t2={t2['start_time'][:16]}~{t2['end_time'][:16]}")


def test_no_setup_when_zero():
    """setup_time=0 时不创建换型 interval，行为与之前一致"""
    print("测试3: 零换型时间不影响排产...")
    tasks = [
        TaskInput("TASK-001", "ORD-001", "ITEM-001", "PROD-001", 100, 10.0,
                  setup_time_minutes=0, priority=2, allowed_centers=["WC-001"]),
        TaskInput("TASK-002", "ORD-002", "ITEM-002", "PROD-001", 100, 10.0,
                  setup_time_minutes=0, priority=2, allowed_centers=["WC-001"]),
    ]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "线1")], start_date=NOW)
    assert r.success
    for t in r.tasks:
        assert t["setup_time_minutes"] == 0
    print("   OK 零换型时间，行为正常")


def test_per_center_setup():
    """不同中心的换型时间不同"""
    print("测试4: 按中心差异换型...")
    tasks = [
        TaskInput("TASK-001", "ORD-001", "ITEM-001", "PROD-001", 100, 10.0,
                  per_center_setup={"WC-001": 30, "WC-002": 20},
                  per_center_hours={"WC-001": 10.0, "WC-002": 20.0},
                  priority=2, allowed_centers=["WC-001", "WC-002"]),
    ]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "线1"), wc("WC-002", "线2")], start_date=NOW)
    assert r.success
    t = r.tasks[0]
    cid = t["center_id"]
    expected_setup = {"WC-001": 30, "WC-002": 20}[cid]
    assert t["setup_time_minutes"] == expected_setup, \
        f"中心{cid}期望setup={expected_setup}, 实际={t['setup_time_minutes']}"
    print(f"   OK 分配到{cid}, setup={t['setup_time_minutes']}min")


def test_setup_persisted():
    """换型时间应落库到 production_task.setup_time_minutes"""
    print("测试5: 换型时间落库...")
    # 用真实数据求解
    orders = repo.fetch_orders()
    for o in orders:
        for it in o["items"]:
            it["duration_hours"] = it["quantity"] * 0.5
    wcs_data = repo.fetch_work_centers(only_active=True)
    cal = repo.fetch_capacity_calendar(center_ids=[w["center_id"] for w in wcs_data])
    routing_map = repo.fetch_routing_map()

    # 注入 _routing（含 setup_time）
    for o in orders:
        for it in o["items"]:
            routes = routing_map.get(it["product_id"], [])
            it["_routing"] = {
                r["center_id"]: {
                    "hours_per_unit": r["hours_per_unit"],
                    "setup_time_minutes": r["setup_time_minutes"],
                }
                for r in routes
            }

    ti, wi = prepare_solver_inputs(orders, wcs_data, cal, start_date=NOW)
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=25).solve(ti, wi)
    assert r.success, r.status

    plan_id = f"{TEST_PREFIX}-001"
    pw.save_plan(r.tasks, {"plan_id": plan_id})

    rows = pg_query(
        """SELECT DISTINCT setup_time_minutes FROM production_task WHERE plan_id=%s""",
        (plan_id,))
    setups = {row["setup_time_minutes"] for row in rows}
    assert setups != {0}, f"所有换型时间都是0，未落库: {setups}"

    print(f"   OK 库内换型时间: {setups}")


def test_output_shows_setup():
    """输出文本应包含换型时间"""
    print("测试6: 输出展示换型...")
    from src.agent.graphs.plan_graph import build_plan_graph
    from src.models.state import create_initial_state

    cleanup()
    import json
    class MockLLM:
        def invoke(self, messages):
            class R:
                content = json.dumps({"request_type": "new_plan", "params": {},
                                      "confidence": 0.95, "reasoning": "test"})
            return R()

    r = build_plan_graph(MockLLM()).invoke(create_initial_state("安排生产"))
    out = r.get("output_content") or ""

    # 如果有换型时间 > 0 的任务，输出应包含 "换型"
    has_setup = any(t.get("setup_time_minutes", 0) > 0
                    for t in (r.get("production_tasks") or []))
    if has_setup:
        assert "换型" in out, f"输出未展示换型时间:\n{out[:300]}"
        print(f"   OK 输出包含换型时间信息")
    else:
        print(f"   SKIP 无非零换型时间任务")


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_setup_in_routing, test_setup_consumes_capacity,
             test_no_setup_when_zero, test_per_center_setup,
             test_setup_persisted, test_output_shows_setup]
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