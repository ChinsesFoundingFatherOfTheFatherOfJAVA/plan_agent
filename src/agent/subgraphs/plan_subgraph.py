"""
智能排产计划Agent - Plan子图

职责：策略选择 -> OR-Tools 日历感知求解 -> 结果落库

与初版的差异：
    - 求解输入改由 prepare_solver_inputs 装配工作日历，任务只排在班次内
    - 插单 / 重平衡场景把库中既有任务作为固定占用传入，保护已确认的生产安排
    - 求解结果直接写入 PostgreSQL（production_plan / production_task），
      并回写 capacity_calendar.planned_hours
    - 产能不足不再静默丢弃，转为 unscheduled_tasks 供 output 节点解释
"""
from datetime import datetime
from typing import Any, Dict

from src.models.state import PlanAgentState, RequestType
from src.services import plan_writer as writer
from src.services.ortools_service import (
    ORToolsScheduler,
    SolverStrategy,
    build_fixed_intervals,
    prepare_solver_inputs,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _rule_based_strategy(state: PlanAgentState) -> Dict[str, Any]:
    """规则式策略选择（LLM 不可用时的降级方案）"""
    request_type = state.get("request_type")
    delivery_risks = state.get("delivery_risks") or []
    capacity_gaps = state.get("capacity_gaps") or []

    if request_type == RequestType.INSERT_ORDER.value:
        strategy = "priority_first"
        params = {"priority_weight": 0.6, "due_date_weight": 0.3, "resource_weight": 0.1}
        reason = "插单场景，优先保障紧急订单并尽量少扰动既有计划"
    elif delivery_risks:
        strategy = "priority_first"
        params = {"priority_weight": 0.5, "due_date_weight": 0.4, "resource_weight": 0.1}
        reason = f"存在 {len(delivery_risks)} 项交付风险，优先保交期"
    elif capacity_gaps or request_type == RequestType.REBALANCE.value:
        strategy = "load_balance"
        params = {"balance_weight": 0.6, "efficiency_weight": 0.3, "priority_weight": 0.1}
        reason = f"存在 {len(capacity_gaps)} 处产能瓶颈，平衡各中心负荷"
    else:
        strategy = "shortest_job"
        params = {"duration_weight": 0.5, "priority_weight": 0.3, "setup_weight": 0.2}
        reason = "无显著约束冲突，按最短作业优先提升流转效率"

    return {"strategy": strategy, "strategy_params": params, "strategy_reason": reason}


def _llm_strategy_suggestion(state: PlanAgentState, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用 LLM 分析业务上下文，给出策略建议和权重调整。

    LLM 可以：
        - 确认或推翻规则式策略的选择
        - 调整策略权重参数
        - 给出更贴合业务场景的理由

    降级：LLM 不可用、超时、或返回格式错误时，沿用规则式结果。
    """
    import json
    from src.services.celery_app import _get_llm_or_fallback

    llm = _get_llm_or_fallback()
    if llm is None:
        return fallback

    # MockLLM 实例跳过 LLM 策略建议，直接用规则 fallback
    from src.services.celery_app import _MockLLM
    if isinstance(llm, _MockLLM):
        return fallback

    # 如果 input_node 标记了 request_type 为预设（非 LLM 解析），也跳过 LLM 策略建议
    # 避免 MockLLM 路径下仍然触发真实 LLM 调用导致超时
    _execution_log = state.get("execution_log") or []
    for entry in _execution_log:
        if entry.get("node") == "input" and entry.get("result", {}).get("reasoning") == "request_type pre-set by caller, skip LLM":
            return fallback

    # 构建业务上下文摘要
    orders = state.get("orders") or []
    delivery_risks = state.get("delivery_risks") or []
    capacity_gaps = state.get("capacity_gaps") or []
    shortages = state.get("shortages") or []
    request_type = state.get("request_type", "unknown")

    # 订单摘要
    order_summary = []
    for o in orders[:5]:
        for it in o.get("items", [])[:3]:
            order_summary.append({
                "order_id": o.get("order_id"),
                "product_id": it.get("product_id"),
                "quantity": it.get("quantity"),
                "due_date": str(it.get("due_date", "")),
                "priority": o.get("priority", 2),
            })

    context = {
        "request_type": request_type,
        "order_count": len(orders),
        "orders": order_summary,
        "delivery_risk_count": len(delivery_risks),
        "capacity_gap_count": len(capacity_gaps),
        "shortage_count": len(shortages),
        "rule_based_suggestion": {
            "strategy": fallback["strategy"],
            "reason": fallback["strategy_reason"],
        },
    }

    prompt = f"""你是排产策略顾问。根据当前排产上下文，推荐最合适的排产策略。

## 可用策略
- priority_first: 按优先级排序（紧急订单优先）
- earliest_due: 按交期排序（最近交期优先）
- shortest_job: 最短作业优先（提升流转效率）
- load_balance: 负荷均衡（避免瓶颈）

## 当前业务上下文
{json.dumps(context, ensure_ascii=False, indent=2)}

## 输出格式（严格 JSON）
```json
{{
    "strategy": "策略名（必须从可用策略中选一个）",
    "reasoning": "选择理由（一句话，结合具体业务数据）",
    "weight_adjustments": {{
        "priority_weight": 0.0,
        "due_date_weight": 0.0,
        "duration_weight": 0.0,
        "setup_weight": 0.0,
        "balance_weight": 0.0
    }}
}}
```

注意：weight_adjustments 中只填与所选策略相关的权重，其余填 0。权重总和不必为 1。"""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        messages = [
            SystemMessage(content="你是排产策略顾问，请严格按 JSON 格式输出。"),
            HumanMessage(content=prompt),
        ]
        response = llm.invoke(messages)
        content_str = response.content

        # 提取 JSON
        json_str = content_str
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]

        parsed = json.loads(json_str.strip())

        strategy = parsed.get("strategy", fallback["strategy"])
        reasoning = parsed.get("reasoning", fallback["strategy_reason"])
        weights = parsed.get("weight_adjustments", {})

        # 合并权重：LLM 权重覆盖规则式权重
        params = dict(fallback.get("strategy_params", {}))
        for k, v in weights.items():
            if v is not None and float(v) > 0:
                params[k] = float(v)

        logger.info(f"LLM 策略建议: {strategy}（规则式: {fallback['strategy']}）理由: {reasoning[:60]}")

        return {
            "strategy": strategy,
            "strategy_params": params,
            "strategy_reason": reasoning,
        }
    except Exception as e:
        logger.warning(f"LLM 策略建议失败，降级为规则式: {type(e).__name__}: {e}")
        return fallback


def select_strategy(state: PlanAgentState) -> Dict[str, Any]:
    """
    选择排产策略（规则式 + LLM 增强）。

    流程：
        1. 规则式给出基础策略和权重
        2. LLM 分析业务上下文，可调整策略和权重
        3. RAG 检索相关排产规则，补充策略理由
    """
    # ---- 1. 规则式基础策略 ----
    result = _rule_based_strategy(state)

    # ---- 2. LLM 策略增强 ----
    result = _llm_strategy_suggestion(state, result)

    # ---- 3. RAG 知识增强 ----
    from src.services.rag_helper import build_strategy_query, search_knowledge

    query = build_strategy_query(result["strategy"], state)
    knowledge = search_knowledge(query, top_k=2)

    if knowledge:
        rule_hint = knowledge[0]["content"][:80]
        result["strategy_reason"] = f"{result['strategy_reason']}。规则参考: {rule_hint}..."

    return result


def solve_with_ortools(state: PlanAgentState) -> Dict[str, Any]:
    """
    调用 OR-Tools 求解

    产出：
        production_tasks   已排产任务（含 segments 真实时间分段）
        unscheduled_tasks  放不下的任务（含原因）
        schedule           计划元信息 + 求解统计
    """
    from config.settings import settings

    orders = state.get("orders") or []
    work_centers = state.get("work_centers") or []
    capacity_calendar = state.get("capacity_calendar") or []
    existing_tasks = state.get("existing_tasks") or []
    routing_map = state.get("routing") or {}
    request_type = state.get("request_type")

    # 把工艺路线注入到每个 item 上，供 prepare_solver_inputs 读取
    # _routing = {center_id: {"hours_per_unit": float, "setup_time_minutes": int}}
    for o in orders:
        for it in o.get("items", []):
            routes = routing_map.get(it["product_id"]) or []
            it["_routing"] = {
                r["center_id"]: {
                    "hours_per_unit": r["hours_per_unit"],
                    "setup_time_minutes": r.get("setup_time_minutes", 0),
                }
                for r in routes
            }

    # 从 state 中获取排产窗口起始时间，传给求解器
    planning_window = state.get("planning_window") or {}
    from datetime import datetime as _dt
    solver_start_date = None
    if planning_window.get("start"):
        try:
            solver_start_date = _dt.fromisoformat(planning_window["start"])
        except Exception:
            pass

    task_inputs, wc_inputs = prepare_solver_inputs(
        orders, work_centers, capacity_calendar,
        start_date=solver_start_date,
    )

    if not task_inputs or not wc_inputs:
        reason = "无待排产任务" if not task_inputs else "排产窗口内无可用产能"
        logger.warning(f"跳过求解: {reason}")
        return {
            "production_tasks": [],
            "unscheduled_tasks": [],
            "schedule": {"plan_id": None, "error": reason, "tasks_count": 0},
            "plan_id": None,
        }

    # 插单 / 重平衡：把库中既有任务作为固定占用，避免覆盖已确认的生产安排
    fixed = {}
    if request_type in (RequestType.INSERT_ORDER.value, RequestType.REBALANCE.value):
        fixed = build_fixed_intervals(existing_tasks)
        if fixed:
            logger.info(f"已加载 {sum(len(v) for v in fixed.values())} 个既有任务作为固定占用")

    # 策略映射：LLM 可能返回 enum 中不存在的策略名，做 fallback
    _raw_strategy = state.get("strategy", "priority_first")
    try:
        _strategy = SolverStrategy(_raw_strategy)
    except ValueError:
        # LLM 返回了未知策略，做合理映射
        _strategy_map = {
            "earliest_due": SolverStrategy.PRIORITY_FIRST,
            "edd": SolverStrategy.PRIORITY_FIRST,
            "spt": SolverStrategy.SHORTEST_JOB,
            "shortest_processing_time": SolverStrategy.SHORTEST_JOB,
        }
        _strategy = _strategy_map.get(_raw_strategy, SolverStrategy.PRIORITY_FIRST)
        logger.warning(f"未知策略 {_raw_strategy}，映射为 {_strategy.value}")

    scheduler = ORToolsScheduler(
        strategy=_strategy,
        max_time_seconds=settings.ortools_max_time_seconds,
        num_workers=settings.ortools_num_workers,
        strategy_params=state.get("strategy_params") or {},
        fixed_intervals=fixed,
    )

    result = scheduler.solve(task_inputs, wc_inputs)

    if not result.success:
        logger.error(f"求解失败: {result.status} {result.statistics.get('error')}")
        return {
            "production_tasks": [],
            "unscheduled_tasks": [],
            "schedule": {
                "plan_id": None,
                "error": result.statistics.get("error", "求解失败"),
                "solver_status": result.status,
            },
            "plan_id": None,
        }

    plan_id = writer.new_plan_id()
    window = state.get("planning_window") or {}
    total_hours = sum(
        s["duration_hours"] for t in result.tasks for s in t.get("segments", [])
    )

    schedule = {
        "plan_id": plan_id,
        "plan_date": datetime.now().date().isoformat(),
        "horizon_start": window.get("start"),
        "horizon_end": window.get("end"),
        "tasks_count": len(result.tasks),
        "segments_count": sum(len(t.get("segments", [])) for t in result.tasks),
        "unscheduled_count": len(result.unscheduled_tasks),
        "total_hours": round(total_hours, 2),
        "solve_time_seconds": round(result.solve_time_seconds, 3),
        "solver_status": result.status,
        "statistics": result.statistics,
    }

    logger.info(
        f"求解完成: {result.status} 已排{len(result.tasks)} 未排{len(result.unscheduled_tasks)} "
        f"工时{total_hours:.1f}h 耗时{result.solve_time_seconds:.2f}s"
    )

    return {
        "production_tasks": result.tasks,
        "unscheduled_tasks": result.unscheduled_tasks,
        "schedule": schedule,
        "plan_id": plan_id,
    }


def persist_plan(state: PlanAgentState) -> Dict[str, Any]:
    """
    排产结果落库

    仅在有实际任务时写入。落库失败不中断流程：
    置 plan_persisted=False 并把原因写入 error，由 output 节点如实告知用户，
    避免「界面显示排产成功、库里却没有数据」的静默不一致。
    """
    tasks = state.get("production_tasks") or []
    schedule = state.get("schedule") or {}

    if not tasks:
        return {"plan_persisted": False, "persist_result": None}

    try:
        res = writer.save_plan(
            tasks=tasks,
            schedule=schedule,
            plan_name=f"{state.get('request_type') or 'plan'} {datetime.now():%Y-%m-%d %H:%M}",
            created_by="agent",
            status="draft",  # 草稿态，需人工确认后转 confirmed
        )
        return {"plan_persisted": True, "persist_result": res}

    except writer.PlanWriteError as e:
        logger.error(f"计划落库失败: {e}")
        return {
            "plan_persisted": False,
            "persist_result": None,
            "error": f"排产计划落库失败: {e}",
        }
    except Exception as e:
        logger.exception("计划落库出现未预期异常")
        return {
            "plan_persisted": False,
            "persist_result": None,
            "error": f"排产计划落库异常: {type(e).__name__}: {e}",
        }


def plan_node(state: PlanAgentState) -> Dict[str, Any]:
    """
    Plan节点主函数

    顺序：策略选择 -> 求解 -> 落库
    """
    # 上游取数失败则不做无意义求解
    if state.get("error") and not state.get("orders"):
        return {
            "production_tasks": [],
            "unscheduled_tasks": [],
            "schedule": {"plan_id": None, "error": state["error"]},
            "plan_id": None,
            "plan_persisted": False,
        }

    out: Dict[str, Any] = {}

    strategy_result = select_strategy(state)
    out.update(strategy_result)
    state.update(strategy_result)

    solve_result = solve_with_ortools(state)
    out.update(solve_result)
    state.update(solve_result)

    persist_result = persist_plan(state)
    out.update(persist_result)
    state.update(persist_result)

    execution_log = state.get("execution_log") or []
    execution_log.append({
        "node": "plan",
        "action": "plan_all",
        "result": {
            "strategy": out.get("strategy"),
            "strategy_reason": out.get("strategy_reason"),
            "tasks_count": len(out.get("production_tasks") or []),
            "unscheduled_count": len(out.get("unscheduled_tasks") or []),
            "plan_id": out.get("plan_id"),
            "persisted": out.get("plan_persisted"),
            "segment_rows": (out.get("persist_result") or {}).get("segment_rows"),
            "solver_status": (out.get("schedule") or {}).get("solver_status"),
            "timestamp": datetime.now().isoformat(),
        },
    })
    out["execution_log"] = execution_log

    return out