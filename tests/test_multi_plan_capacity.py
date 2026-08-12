# -*- coding: utf-8 -*-
"""多计划产能回写测试：验证多计划并存时 planned_hours 从 allocation 汇总"""
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


def cleanup():
    """全量清理"""
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
    orders = repo.fetch_orders()
    for o in orders:
        for it in o["items"]:
            it["duration_hours"] = it["quantity"] * 0.5
    wcs = repo.fetch_work_centers(only_active=True)
    cal = repo.fetch_capacity_calendar(center_ids=[w["center_id"] for w in wcs])
    ti, wi = prepare_solver_inputs(orders, wcs, cal)
    return ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=25).solve(ti, wi)


def test_multi_plan_allocation():
    """两个计划并存时，capacity_allocation 应各有记录，planned_hours 应为两者之和"""
    print("测试1: 多计划产能分配...")

    r = solve_real()
    assert r.success and r.tasks

    # 保存计划 A
    plan_a = "PLAN-MULTI-A"
    pw.save_plan(r.tasks, {"plan_id": plan_a}, plan_name="plan A")

    # 查询计划 A 的 allocation
    alloc_a = pg_query(
        "SELECT center_id, work_date, planned_hours FROM capacity_allocation WHERE plan_id=%s ORDER BY center_id, work_date",
        (plan_a,))
    assert alloc_a, "plan A 无 allocation 记录"

    # 查询 capacity_calendar 的 planned_hours（应等于 plan A 的 allocation 汇总）
    cal_a = pg_query(
        """SELECT c.center_id, c.work_date, c.planned_hours
           FROM capacity_calendar c
           WHERE c.planned_hours > 0
           ORDER BY c.center_id, c.work_date""")

    # 保存计划 B（不同 plan_id，相同数据）
    plan_b = "PLAN-MULTI-B"
    pw.save_plan(r.tasks, {"plan_id": plan_b}, plan_name="plan B")

    # 查询计划 B 的 allocation
    alloc_b = pg_query(
        "SELECT center_id, work_date, planned_hours FROM capacity_allocation WHERE plan_id=%s ORDER BY center_id, work_date",
        (plan_b,))
    assert alloc_b, "plan B 无 allocation 记录"

    # 验证：capacity_calendar.planned_hours 应等于两个计划 allocation 之和
    cal_b = pg_query(
        """SELECT c.center_id, c.work_date, c.planned_hours,
                  COALESCE(SUM(a.planned_hours), 0) AS alloc_sum
           FROM capacity_calendar c
           LEFT JOIN capacity_allocation a
             ON a.center_id = c.center_id AND a.work_date = c.work_date
           WHERE c.planned_hours > 0
           GROUP BY c.center_id, c.work_date, c.planned_hours
           ORDER BY c.center_id, c.work_date""")

    assert cal_b, "无 planned_hours > 0 的记录"
    for row in cal_b:
        expected = float(row["alloc_sum"])
        actual = float(row["planned_hours"])
        assert abs(actual - expected) < 0.05, \
            f"center={row['center_id']} date={row['work_date']} planned={actual} expected={expected}"

    print(f"   OK plan A: {len(alloc_a)} 条 allocation, plan B: {len(alloc_b)} 条")
    print(f"   OK {len(cal_b)} 天 planned_hours = 两计划 allocation 之和")

    # 验证：删除计划 A 后，planned_hours 应只剩计划 B 的
    pg_execute("DELETE FROM capacity_allocation WHERE plan_id=%s", (plan_a,))

    # 需要手动触发重算（删除 allocation 不会自动更新 calendar）
    # 这里验证 save_plan 重新保存 B 时会正确重算
    pw.save_plan(r.tasks, {"plan_id": plan_b}, plan_name="plan B re-save")

    cal_after = pg_query(
        """SELECT c.center_id, c.work_date, c.planned_hours,
                  COALESCE(SUM(a.planned_hours), 0) AS alloc_sum
           FROM capacity_calendar c
           LEFT JOIN capacity_allocation a
             ON a.center_id = c.center_id AND a.work_date = c.work_date
           WHERE a.plan_id = %s
           GROUP BY c.center_id, c.work_date, c.planned_hours
           ORDER BY c.center_id, c.work_date""",
        (plan_b,))

    for row in cal_after:
        expected = float(row["alloc_sum"])
        actual = float(row["planned_hours"])
        assert abs(actual - expected) < 0.05, \
            f"after delete A: center={row['center_id']} planned={actual} expected={expected}"

    print(f"   OK 删除计划 A 后，planned_hours 仅剩计划 B")


def test_idempotent_allocation():
    """同一计划重复保存，allocation 不应翻倍"""
    print("测试2: allocation 幂等...")
    r = solve_real()
    plan_id = "PLAN-MULTI-IDEM"
    pw.save_plan(r.tasks, {"plan_id": plan_id})
    count1 = pg_query("SELECT COUNT(*) c FROM capacity_allocation WHERE plan_id=%s", (plan_id,))[0]["c"]
    pw.save_plan(r.tasks, {"plan_id": plan_id})
    count2 = pg_query("SELECT COUNT(*) c FROM capacity_allocation WHERE plan_id=%s", (plan_id,))[0]["c"]
    assert count1 == count2, f"重复保存后 allocation 翻倍: {count1} -> {count2}"
    print(f"   OK 重复保存 allocation 不变 ({count1} 条)")


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_multi_plan_allocation, test_idempotent_allocation]
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
    finally:
        release_test_lock(_lock)
    sys.exit(0 if passed == len(tests) else 1)