"""OR-Tools 日历感知求解器测试"""
import sys, os
from datetime import datetime, timedelta, date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ortools_service import (
    ORToolsScheduler, SolverStrategy, TaskInput, WorkCenterInput,
    prepare_solver_inputs, build_fixed_intervals,
)
from src.services.calendar_service import WorkCalendar

NOW = datetime(2026, 8, 10, 8, 0)  # 周一 08:00


def cal_rows(cid, days=30, hours=15.2, start=None):
    start = start or NOW.date()
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append({"center_id": cid, "work_date": d.isoformat(),
                    "available_hours": 0 if d.weekday() >= 5 else hours,
                    "planned_hours": 0})
    return out


def wc(cid, name, hours=15.2, days=30):
    return WorkCenterInput(cid, name, 16.0, 0.95,
                           calendar=WorkCalendar.from_calendar_rows(cid, cal_rows(cid, days, hours), now=NOW))


def test_no_night_weekend():
    """核心场景：100小时任务不得跨越夜间与周末"""
    print("测试1: 长任务不排入非工作时间...")
    tasks = [TaskInput("TASK-001", "ORD-002", "ITEM-001", "PROD-001", 200, 100.0,
                       due_date=NOW + timedelta(days=15), priority=1, allowed_centers=["WC-001"])]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "生产线1")], start_date=NOW)
    assert r.success, r.status
    t = r.tasks[0]
    assert len(t["segments"]) == 7, f"期望7段，实际{len(t['segments'])}"
    for s in t["segments"]:
        a = datetime.fromisoformat(s["start_time"]); b = datetime.fromisoformat(s["end_time"])
        assert a.weekday() < 5, f"排到周末: {a}"
        assert a.date() == b.date(), f"段跨天: {a} ~ {b}"
        assert 8 <= a.hour < 24, f"排到夜间: {a}"
    total = sum(s["duration_hours"] for s in t["segments"])
    assert abs(total - 100.0) < 0.01, total
    print(f"   OK 100h -> {len(t['segments'])}段, {t['start_time'][:16]} ~ {t['end_time'][:16]}, 工时合计{total}h")


def test_no_overlap_same_center():
    """同一中心任务不得重叠"""
    print("测试2: 同中心资源独占...")
    tasks = [TaskInput(f"TASK-{i:03d}", "ORD-001", f"ITEM-{i}", "PROD-001", 20, 10.0,
                       priority=2, allowed_centers=["WC-001"]) for i in range(1, 6)]
    r = ORToolsScheduler(SolverStrategy.SHORTEST_JOB, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "生产线1")], start_date=NOW)
    assert r.success and len(r.tasks) == 5
    iv = sorted([(datetime.fromisoformat(t["start_time"]), datetime.fromisoformat(t["end_time"]))
                 for t in r.tasks])
    for i in range(len(iv) - 1):
        assert iv[i][1] <= iv[i + 1][0], f"重叠: {iv[i]} vs {iv[i+1]}"
    print(f"   OK 5个任务无重叠, 跨度 {iv[0][0]:%m-%d %H:%M} ~ {iv[-1][1]:%m-%d %H:%M}")


def test_unscheduled_when_over_capacity():
    """产能不足时应明确报告未排产任务，而非返回 INFEASIBLE"""
    print("测试3: 产能不足的可解释输出...")
    # 单中心 5 天窗口 = 4 个工作日 x 15.2h = 60.8h，投入 3 x 40h = 120h
    tasks = [TaskInput(f"TASK-{i:03d}", "ORD-001", f"ITEM-{i}", "PROD-001", 80, 40.0,
                       priority=2, allowed_centers=["WC-001"]) for i in range(1, 4)]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "生产线1", days=5)], start_date=NOW)
    assert r.success, f"应返回可行解而非{r.status}"
    assert len(r.unscheduled_tasks) >= 1, "应报告未排产任务"
    assert len(r.tasks) + len(r.unscheduled_tasks) == 3
    print(f"   OK 已排{len(r.tasks)}个, 未排{len(r.unscheduled_tasks)}个, 原因: {r.unscheduled_tasks[0]['reason']}")


def test_tardiness_reported():
    """交期不可满足时以超期量报告，而非求解失败"""
    print("测试4: 超期软约束...")
    tasks = [TaskInput("TASK-001", "ORD-003", "ITEM-001", "PROD-002", 40, 60.0,
                       due_date=NOW + timedelta(days=2), priority=1, allowed_centers=["WC-001"])]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "生产线1")], start_date=NOW)
    assert r.success, r.status
    t = r.tasks[0]
    assert t["tardiness_hours"] > 0, "应识别出超期"
    print(f"   OK 60h任务/2天交期 -> 超期 {t['tardiness_hours']}h, 完工 {t['end_time'][:16]}")


def test_load_balance():
    """负荷均衡应把任务分散到多个中心"""
    print("测试5: 负荷均衡...")
    tasks = [TaskInput(f"TASK-{i:03d}", "ORD-001", f"ITEM-{i}", "PROD-001", 20, 10.0,
                       priority=2, allowed_centers=["WC-001", "WC-002", "WC-003"]) for i in range(1, 7)]
    wcs = [wc("WC-001", "线1"), wc("WC-002", "线2"), wc("WC-003", "线3")]
    r = ORToolsScheduler(SolverStrategy.LOAD_BALANCE, max_time_seconds=20).solve(tasks, wcs, start_date=NOW)
    assert r.success
    dist = {}
    for t in r.tasks:
        dist[t["center_id"]] = dist.get(t["center_id"], 0) + 1
    assert len(dist) >= 2, f"应分散到多中心, 实际{dist}"
    print(f"   OK 任务分布 {dist}")


def test_precedence_same_center():
    """前置约束（同中心）"""
    print("测试6: 前置任务约束...")
    tasks = [
        TaskInput("TASK-001", "ORD-001", "ITEM-001", "PROD-001", 40, 20.0, priority=1, allowed_centers=["WC-001"]),
        TaskInput("TASK-002", "ORD-001", "ITEM-002", "PROD-002", 20, 10.0, priority=1,
                  predecessors=["TASK-001"], allowed_centers=["WC-001"]),
    ]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20).solve(
        tasks, [wc("WC-001", "生产线1")], start_date=NOW)
    assert r.success, r.status
    t1 = next(t for t in r.tasks if t["task_id"] == "TASK-001")
    t2 = next(t for t in r.tasks if t["task_id"] == "TASK-002")
    assert datetime.fromisoformat(t2["start_time"]) >= datetime.fromisoformat(t1["end_time"]), "前置约束违反"
    print(f"   OK T1完工{t1['end_time'][:16]} <= T2开工{t2['start_time'][:16]}")


def test_fixed_intervals():
    """插单场景：已排产任务的时间段不得被占用"""
    print("测试7: 已排产任务保护...")
    busy_start = datetime(2026, 8, 10, 8, 0)
    busy_end = datetime(2026, 8, 10, 16, 0)
    fixed = build_fixed_intervals([{"center_id": "WC-001", "task_id": "OLD-001",
                                    "start_time": busy_start.isoformat(),
                                    "end_time": busy_end.isoformat()}])
    tasks = [TaskInput("TASK-NEW", "ORD-009", "ITEM-001", "PROD-001", 10, 5.0,
                       priority=1, allowed_centers=["WC-001"])]
    r = ORToolsScheduler(SolverStrategy.PRIORITY_FIRST, max_time_seconds=20,
                         fixed_intervals=fixed).solve(tasks, [wc("WC-001", "生产线1")], start_date=NOW)
    assert r.success, r.status
    for s in r.tasks[0]["segments"]:
        a = datetime.fromisoformat(s["start_time"]); b = datetime.fromisoformat(s["end_time"])
        assert not (a < busy_end and b > busy_start), f"与已排任务重叠: {a}~{b}"
    print(f"   OK 新任务避开占用段, 排在 {r.tasks[0]['start_time'][:16]}")


def test_prepare_inputs_reuses_duration():
    """prepare_solver_inputs 应复用上游 duration_hours"""
    print("测试8: 工时口径统一...")
    orders = [{"order_id": "ORD-001", "priority": 1, "required_date": "2026-08-25",
               "items": [{"item_id": "IT-1", "product_id": "PROD-001", "quantity": 100,
                          "duration_hours": 7.5, "due_date": "2026-08-20"}]}]
    wcs = [{"center_id": "WC-001", "center_name": "线1", "capacity_per_day": 16,
            "efficiency_rate": 0.95, "status": "active"}]
    ti, wi = prepare_solver_inputs(orders, wcs, cal_rows("WC-001"), start_date=NOW)
    assert len(ti) == 1 and ti[0].duration_hours == 7.5, ti[0].duration_hours
    assert ti[0].due_date == datetime(2026, 8, 20, 23, 59), ti[0].due_date
    assert len(wi) == 1 and wi[0].calendar is not None
    print(f"   OK 复用工时{ti[0].duration_hours}h, 交期归一至{ti[0].due_date:%m-%d %H:%M}")


if __name__ == "__main__":
    tests = [test_no_night_weekend, test_no_overlap_same_center, test_unscheduled_when_over_capacity,
             test_tardiness_reported, test_load_balance, test_precedence_same_center,
             test_fixed_intervals, test_prepare_inputs_reuses_duration]
    passed = 0
    for t in tests:
        try:
            t(); passed += 1
        except AssertionError as e:
            print(f"   FAIL {t.__name__}: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"   ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果: {passed}/{len(tests)} 通过")
    sys.exit(0 if passed == len(tests) else 1)