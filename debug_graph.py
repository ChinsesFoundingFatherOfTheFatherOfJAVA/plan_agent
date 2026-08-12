# -*- coding: utf-8 -*-
"""调试排产图：直接调用排产图，不需要启动 API 服务

用法：
    .\venv\Scripts\python.exe debug_graph.py
    或在 VSCode 中按 F5 选择"调试排产图(单次)"
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from src.agent.graphs.plan_graph import build_plan_graph
from src.models.state import create_initial_state
from src.services.celery_app import _MockLLM


def main():
    print("=" * 60)
    print("排产图调试脚本")
    print("=" * 60)

    # 使用 MockLLM 跳过 LLM 调用
    llm = _MockLLM("new_plan")
    graph = build_plan_graph(llm)

    state = create_initial_state("安排下周生产计划")
    state["request_type"] = "new_plan"

    print("\n调用排产图...")
    result = graph.invoke(state)

    print("\n" + "=" * 60)
    print("排产结果")
    print("=" * 60)
    print(f"计划ID:   {result.get('plan_id', 'N/A')}")
    print(f"策略:     {result.get('strategy', 'N/A')}")
    print(f"任务数:   {len(result.get('production_tasks', []))}")

    schedule = result.get("schedule", {})
    print(f"求解状态: {schedule.get('solver_status', 'N/A')}")
    print(f"总工时:   {schedule.get('total_hours', 0)}h")

    tasks = result.get("production_tasks", [])
    if tasks:
        print("\n--- 任务明细 ---")
        for t in tasks:
            tid = t.get("task_id", "?")
            pid = t.get("product_id", "?")
            qty = t.get("quantity", 0)
            cid = t.get("center_id", "?")
            dur = t.get("duration_hours", 0)
            print(f"  {tid} {pid} x{qty} @ {cid} {dur}h")

    recs = result.get("recommendations", [])
    if recs:
        print("\n--- 建议 ---")
        for rec in recs:
            print(f"  - {rec[:80]}")

    print(f"\nplan_persisted: {result.get('plan_persisted', False)}")
    print("完成!")


if __name__ == "__main__":
    main()
