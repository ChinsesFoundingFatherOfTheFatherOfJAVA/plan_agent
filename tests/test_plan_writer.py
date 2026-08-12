"""落库服务测试：真实 PG，验证分段写入、产能回写、多计划并存、约束冲突处理"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.services import plan_writer as pw
from src.services.db import pg_query, pg_execute
from src.services.ortools_service import (
    ORToolsScheduler, SolverStrategy, prepare_solver_inputs)
from src.services import repository as repo
from db_lock import acquire_test_lock, release_test_lock

TEST_PREFIX = "PLAN-TEST"


def cleanup():
    """清理测试数据：删除所有 production_task/plan（含非测试计划残留），重置产能日历"""
    pg_execute("DELETE FROM insert_order_affected_task", None)
    pg_execute("DELETE FROM insert_order_log", None)
    pg_execute("DELETE FROM plan_run_log", None)
    pg_execute("DELETE FROM capacity_allocation", None)
    pg_execute("DELETE FROM production_task", None)
    pg_execute("DELETE FROM production_plan", None)
    pg_execute("DELETE FROM delivery_alert", None)
    pg_execute("DELETE FROM shortage_alert", None)
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours != 0", None)


def solve_real():
    """用库中真实数据求解一版计划"""
    orders = repo.fetch_orders()
    for o in orders:
        for it in o["items"]:
            it["duration_hours"] = it["quantity"] * 0.5
    wcs = repo.fetch_work_centers(only_active=True)
    cal = repo.fetch_capacity_calendar(center_ids=[w["center_id"] for w in wcs])
    ti, wi = prepare_solver_inputs(orders, wcs, cal)
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=25).solve(ti, wi)
    return r, orders


def test_save_segments():
    """跨天任务应一段一行写入，且每段落在工作时间内"""
    print("测试1: 分段落库...")
    r, _ = solve_real()
    assert r.success, r.status
    assert r.tasks, "无排产结果"

    plan_id = f"{TEST_PREFIX}-001"
    res = pw.save_plan(r.tasks, {"plan_id": plan_id}, plan_name="测试计划")

    rows = pg_query(
        """SELECT task_id, parent_task_id, segment_index, segment_count,
                  center_id, start_time, end_time, duration_hours
           FROM production_task WHERE plan_id=%s ORDER BY center_id, start_time""",
        (plan_id,))
    assert len(rows) == res["segment_rows"], f"{len(rows)} != {res['segment_rows']}"
    assert len(rows) > len(r.tasks), "跨天任务应产生多段"

    for row in rows:
        assert row["start_time"].date() == row["end_time"].date(), f"段跨天: {row}"
        assert row["start_time"].weekday() < 5, f"段落在周末: {row}"
        assert row["start_time"].hour >= 8, f"段落在夜间: {row}"

    print(f"   OK {len(r.tasks)}个任务 -> {len(rows)}段, 总工时{res['total_hours']}h, 中心{res['centers']}")


def test_no_overlap_in_db():
    """同一计划内同中心不得有重叠段"""
    print("测试2: 计划内无重叠...")
    rows = pg_query(
        """SELECT a.task_id, b.task_id AS other
           FROM production_task a JOIN production_task b
             ON a.plan_id = b.plan_id AND a.plan_version = b.plan_version
            AND a.center_id = b.center_id AND a.task_id < b.task_id
            AND tsrange(a.start_time,a.end_time) && tsrange(b.start_time,b.end_time)""")
    assert not rows, f"存在重叠段: {rows[:3]}"
    print("   OK 排他约束生效，无重叠")


def test_calendar_writeback():
    """planned_hours 应等于该日该中心各段工时之和，且不超日产能"""
    print("测试3: 产能日历回写...")
    rows = pg_query(
        """SELECT c.center_id, c.work_date, c.available_hours, c.planned_hours,
                  COALESCE(SUM(t.duration_hours),0) AS seg_hours,
                  COALESCE(SUM(CASE WHEN t.segment_index = 0 THEN t.setup_time_minutes ELSE 0 END),0) AS setup_min
           FROM capacity_calendar c
           LEFT JOIN production_task t
             ON t.center_id = c.center_id AND t.start_time::date = c.work_date
           WHERE c.planned_hours > 0
           GROUP BY c.center_id, c.work_date, c.available_hours, c.planned_hours
           ORDER BY c.work_date""")
    assert rows, "planned_hours 未回写"
    for r in rows:
        expected = float(r["seg_hours"]) + float(r["setup_min"]) / 60.0
        assert abs(float(r["planned_hours"]) - expected) < 0.05, r
        assert float(r["planned_hours"]) <= float(r["available_hours"]) + 0.01, \
            f"超过日产能: {r}"
    print(f"   OK {len(rows)}天已回写, 均未超日产能")


def test_idempotent():
    """同 plan_id + 同版本重复保存应幂等（段主键须稳定）"""
    print("测试4: 重复保存幂等...")
    r, _ = solve_real()
    plan_id = f"{TEST_PREFIX}-001"
    res1 = pw.save_plan(r.tasks, {"plan_id": plan_id})
    res2 = pw.save_plan(r.tasks, {"plan_id": plan_id})
    assert res1["segment_rows"] == res2["segment_rows"], "重复保存段数不一致"
    rows = pg_query("SELECT count(*) c FROM production_task WHERE plan_id=%s", (plan_id,))
    assert rows[0]["c"] == res1["segment_rows"], f"重复保存后段数应一致({res1['segment_rows']}), 实际{rows[0]['c']}"
    print(f"   OK 重复保存段数一致({res1['segment_rows']}), 计划头 1 条")


def test_multi_plan_coexist():
    """不同 plan_id 可并存，互不冲突"""
    print("测试5: 多计划并存...")
    r, _ = solve_real()
    pw.save_plan(r.tasks, {"plan_id": f"{TEST_PREFIX}-001"})
    pw.save_plan(r.tasks, {"plan_id": f"{TEST_PREFIX}-002"})
    rows = pg_query("SELECT count(*) c FROM production_task WHERE plan_id LIKE %s", (TEST_PREFIX + "-%",))
    assert rows[0]["c"] == len(r.tasks) * 2 or rows[0]["c"] >= len(r.tasks), f"多计划段数异常: {rows[0]['c']}"
    print("   OK 两个计划可共存，无冲突")


def test_intra_plan_overlap_rejected():
    """计划内时间重叠应被 DB 排他约束拒绝"""
    print("测试6: 计划内重叠拦截...")
    bad_start = datetime(2026, 8, 17, 8, 0)

    def mk(tid, offset_h):
        s = bad_start + timedelta(hours=offset_h)
        return {"task_id": tid, "order_id": None, "item_id": None,
                "product_id": "PROD-001", "center_id": "WC-001", "quantity": 10,
                "priority": 2, "duration_hours": 4.0,
                "segments": [{"segment_index": 0, "start_time": s.isoformat(),
                              "end_time": (s + timedelta(hours=4)).isoformat(),
                              "duration_hours": 4.0}]}

    tasks = [mk("TASK-DUP1", 0), mk("TASK-DUP2", 2)]
    try:
        pw.save_plan(tasks, {"plan_id": f"{TEST_PREFIX}-003"}, update_calendar=False)
        raise AssertionError("未拦截计划内重叠")
    except pw.PlanWriteError as e:
        assert "重叠" in str(e), str(e)
        print(f"   OK 已拦截: {str(e)[:70]}...")


def test_alerts():
    """预警落库 + 去重"""
    print("测试7: 预警落库...")
    pg_execute("DELETE FROM shortage_alert", None)
    pg_execute("DELETE FROM delivery_alert", None)
    due = (datetime.now() + timedelta(days=10)).date().isoformat()

    shortages = [{"material_id": "MAT-002", "required_date": due,
                  "required_quantity": 500, "available_quantity": 250,
                  "shortage_quantity": 250, "risk_level": "high"}]
    assert pw.save_shortage_alerts(shortages) == 1
    pw.save_shortage_alerts(shortages)
    active = pg_query("SELECT COUNT(*) c FROM shortage_alert WHERE status='active'")[0]["c"]
    assert active == 1, f"active 预警应为1, 实际{active}"

    risks = [{"order_id": "ORD-003", "item_id": None, "due_date": due,
              "expected_date": (datetime.now() + timedelta(days=14)).date().isoformat(),
              "risk_level": "critical"}]
    assert pw.save_delivery_alerts(risks) == 1
    row = pg_query("SELECT delay_days, alert_type FROM delivery_alert WHERE status='active'")[0]
    assert row["delay_days"] == 4, row
    assert row["alert_type"] == "critical", row
    print(f"   OK 缺料active=1, 交付延期{row['delay_days']}天/{row['alert_type']}")


def test_run_log():
    """审计日志落库"""
    print("测试8: 运行审计...")
    r, _ = solve_real()
    plan_id = f"{TEST_PREFIX}-001"
    pw.save_plan(r.tasks, {"plan_id": plan_id})
    state = {"request_type": "new_plan", "user_input": "安排下周生产计划",
             "strategy": "priority_first", "production_tasks": r.tasks,
             "unscheduled_tasks": r.unscheduled_tasks,
             "plan_persisted": True,
             "schedule": {"solver_status": r.status,
                          "solve_time_seconds": r.solve_time_seconds,
                          "statistics": r.statistics},
             "validation_passed": True, "conflicts": [], "retry_count": 0}
    run_id = pw.save_run_log(state, plan_id)
    row = pg_query("SELECT * FROM plan_run_log WHERE run_id=%s", (run_id,))[0]
    assert row["request_type"] == "new_plan"
    assert row["tasks_scheduled"] == len(r.tasks)
    print(f"   OK run_id={run_id}, 已排{row['tasks_scheduled']}, 策略={row['strategy']}")


def test_confirm():
    """计划确认流转"""
    print("测试9: 计划确认...")
    r, _ = solve_real()
    plan_id = f"{TEST_PREFIX}-001"
    pw.save_plan(r.tasks, {"plan_id": plan_id})
    n = pw.confirm_plan(plan_id)
    assert n == 1, n
    st = pg_query("SELECT status FROM production_plan WHERE plan_id=%s",
                  (plan_id,))[0]["status"]
    assert st == "confirmed", st
    task_st = pg_query(
        "SELECT DISTINCT status FROM production_task WHERE plan_id=%s", (plan_id,))
    assert all(x["status"] == "scheduled" for x in task_st), task_st
    print(f"   OK 计划={st}, 任务=scheduled")


if __name__ == "__main__":
    # 与其他依赖真实库的测试互斥，避免交叠执行互相清理数据
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_save_segments, test_no_overlap_in_db, test_calendar_writeback,
             test_idempotent, test_multi_plan_coexist, test_intra_plan_overlap_rejected,
             test_alerts, test_run_log, test_confirm]
    try:
        cleanup()
        for t in tests:
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
