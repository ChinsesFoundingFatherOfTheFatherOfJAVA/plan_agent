"""
端到端集成测试：真实 PG 数据走完整图，校验输出与库内数据一致

覆盖：
    新建排产   数据获取 -> 分析 -> 求解 -> 落库 -> 输出
    缺料检查   BOM 展开与缺口计算
    产能不足   未排产任务的可解释输出
    库内一致   输出所述段数/工时与 production_task 实际一致
"""
import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agent.graphs.plan_graph import build_plan_graph
from src.models.state import create_initial_state
from src.services.db import pg_query, pg_execute
from db_lock import acquire_test_lock, release_test_lock

# 产能日历基线：工作日 15.2h（16h x 0.95 效率），周末 0
RESTORE_CALENDAR = """UPDATE capacity_calendar SET available_hours =
    CASE WHEN EXTRACT(ISODOW FROM work_date) >= 6 THEN 0 ELSE 15.2 END"""


class MockLLM:
    """按指定请求类型返回结构化结果，替代真实 LLM"""

    def __init__(self, request_type="new_plan", params=None):
        self.request_type = request_type
        self.params = params or {}

    def invoke(self, messages):
        import json

        class R:
            content = json.dumps({
                "request_type": self.request_type,
                "params": self.params,
                "confidence": 0.95,
                "reasoning": "integration test",
            }, ensure_ascii=False)

        return R()


def cleanup():
    """清理计划与预警数据，并恢复产能日历基线"""
    pg_execute("DELETE FROM plan_run_log", None)
    pg_execute("DELETE FROM capacity_allocation", None)
    pg_execute("DELETE FROM production_task", None)
    pg_execute("DELETE FROM production_plan", None)
    pg_execute("DELETE FROM shortage_alert", None)
    pg_execute("DELETE FROM delivery_alert", None)
    pg_execute("UPDATE capacity_calendar SET planned_hours = 0 WHERE planned_hours <> 0")
    pg_execute(RESTORE_CALENDAR, None)


def run(request_type="new_plan", user_input="安排下周的生产计划", params=None):
    graph = build_plan_graph(MockLLM(request_type, params))
    return graph.invoke(create_initial_state(user_input))


def test_new_plan_end_to_end():
    """新建排产全链路"""
    print("测试1: 新建排产端到端...")
    r = run("new_plan")

    assert r.get("request_type") == "new_plan"
    assert r.get("orders"), "未取到订单"
    assert r.get("work_centers"), "未取到工作中心"
    assert r.get("production_tasks"), "未生成任务"
    assert r.get("plan_id"), "无计划ID"
    assert r.get("plan_persisted") is True, f"未落库: {r.get('error')}"

    nodes = [e["node"] for e in r.get("execution_log") or []]
    for n in ("input", "data_fetch", "analyze", "plan", "validate", "output"):
        assert n in nodes, f"节点 {n} 未执行, 实际: {nodes}"

    print(f"   OK 计划{r['plan_id']}, 任务{len(r['production_tasks'])}, "
          f"策略{r.get('strategy')}, 节点链{'->'.join(nodes)}")


def test_db_matches_output():
    """输出所述与库内实际数据一致"""
    print("测试2: 输出与库内一致...")
    r = run("new_plan")
    plan_id = r["plan_id"]
    persist = r.get("persist_result") or {}

    rows = pg_query(
        """SELECT task_id, parent_task_id, segment_index, center_id,
                  start_time, end_time, duration_hours, status
           FROM production_task WHERE plan_id=%s""", (plan_id,))
    assert len(rows) == persist.get("segment_rows"), \
        f"库内{len(rows)}段 != 报告{persist.get('segment_rows')}段"

    db_hours = sum(float(x["duration_hours"] or 0) for x in rows)
    assert abs(db_hours - persist["total_hours"]) < 0.05, \
        f"库内工时{db_hours} != 报告{persist['total_hours']}"

    parents = {x["parent_task_id"] for x in rows}
    assert len(parents) == len(r["production_tasks"]), \
        f"逻辑任务数不一致 {len(parents)} vs {len(r['production_tasks'])}"

    for x in rows:
        assert x["start_time"].weekday() < 5, f"段落在周末: {x}"
        assert x["start_time"].hour >= 8, f"段落在夜间: {x}"

    print(f"   OK 库内{len(rows)}段/{db_hours:.1f}h 与输出一致, {len(parents)}个逻辑任务")


def test_run_log_written():
    """审计日志已记录"""
    print("测试3: 运行审计落库...")
    r = run("new_plan")
    assert r.get("run_id"), "无 run_id"
    rows = pg_query("SELECT * FROM plan_run_log WHERE run_id=%s", (r["run_id"],))
    assert rows, "审计未落库"
    row = rows[0]
    assert row["plan_id"] == r["plan_id"]
    assert row["tasks_scheduled"] == len(r["production_tasks"])
    assert row["strategy"] == r["strategy"]
    print(f"   OK run_id={r['run_id']}, 策略={row['strategy']}, "
          f"已排{row['tasks_scheduled']}, 状态={row['solver_status']}")


def test_shortage_check():
    """缺料检查链路"""
    print("测试4: 缺料检查...")
    r = run("check_shortage", "检查下周的物料缺口情况")
    assert r.get("request_type") == "check_shortage"
    assert r.get("boms"), "未取到BOM"
    assert r.get("materials"), "未取到物料"
    out = r.get("output_content") or ""
    assert "物料缺料检查完成" in out, out[:120]
    shortages = r.get("shortages") or []
    print(f"   OK 缺口{len(shortages)}项, BOM覆盖{len(r['boms'])}个产品, "
          f"预警{len(r.get('alerts') or [])}条")


def test_capacity_shortfall_explained():
    """产能不足时必须显式报告未排产任务，而非静默丢弃"""
    print("测试5: 产能不足可解释...")
    pg_execute("UPDATE capacity_calendar SET available_hours = 0.01", None)
    try:
        r = run("new_plan")
        unsched = r.get("unscheduled_tasks") or []
        assert unsched, "产能极低却无未排产任务，说明缺口被静默吞掉"
        out = r.get("output_content") or ""
        assert "未能排产" in out, "输出未告知未排产情况"
        types = {a["type"] for a in r.get("alerts") or []}
        assert "unscheduled" in types, f"缺少未排产预警: {types}"
        recs = " ".join(r.get("recommendations") or [])
        assert "加班" in recs or "外协" in recs or "延长" in recs, recs
        print(f"   OK 未排产{len(unsched)}个已显式报告, 预警{len(r.get('alerts') or [])}条")
    finally:
        pg_execute(RESTORE_CALENDAR, None)


def test_run_log_survives_unpersisted_plan():
    """
    计划未落库时审计日志仍须写入（回归）

    缺陷：plan_run_log.plan_id 有外键约束，全部任务未排产时计划头不会写入，
    但 plan_id 仍被带入审计 -> ForeignKeyViolation -> 审计静默丢失。
    而「为什么这次排不出来」恰恰是最需要留痕的信息。
    """
    print("测试6: 未落库时审计仍留痕...")
    pg_execute("UPDATE capacity_calendar SET available_hours = 0.01", None)
    try:
        r = run("new_plan")
        assert not r.get("plan_persisted"), "本场景应无计划落库"
        assert r.get("run_id"), "审计日志丢失（外键回归）"
        rows = pg_query("SELECT * FROM plan_run_log WHERE run_id=%s", (r["run_id"],))
        assert rows, "审计未写入库"
        row = rows[0]
        assert row["plan_id"] is None, f"未落库计划的 plan_id 应为空, 实际{row['plan_id']}"
        assert row["tasks_unscheduled"] > 0, "未记录未排产数量"
        print(f"   OK 审计已留痕 run_id={r['run_id']}, "
              f"未排产{row['tasks_unscheduled']}个, plan_id为空")
    finally:
        pg_execute(RESTORE_CALENDAR, None)


def test_no_silent_failure():
    """落库结果必须在输出中明示"""
    print("测试7: 落库状态不静默...")
    r = run("new_plan")
    out = r.get("output_content") or ""
    if r.get("plan_persisted"):
        assert "已写入数据库" in out, "成功落库未告知"
        print("   OK 落库成功并已在输出中说明")
    else:
        assert "未写入数据库" in out, "落库失败未告知"
        print("   OK 落库失败已在输出中明示")


if __name__ == "__main__":
    # 与其他依赖真实库的测试互斥，避免交叠执行互相清理数据
    _lock = acquire_test_lock()
    passed = 0
    tests = [test_new_plan_end_to_end, test_db_matches_output, test_run_log_written,
             test_shortage_check, test_capacity_shortfall_explained,
             test_run_log_survives_unpersisted_plan, test_no_silent_failure]
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