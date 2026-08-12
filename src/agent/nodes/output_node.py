"""
智能排产计划Agent - Output节点

职责：把状态格式化为人类可读输出，并生成预警与建议。

原则：如实呈现，不美化。
    - 未排产任务必须显式列出，不能让用户以为「全排上了」
    - 落库失败必须明示，避免「界面成功、库里没有」的静默不一致
    - 超期任务给出具体超期时长，而非笼统的「有风险」
"""
from datetime import datetime
from typing import Any, Dict, List

from src.models.state import PlanAgentState, RequestType
from src.services import plan_writer as writer
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _fmt_dt(value: Any, with_date: bool = True) -> str:
    """格式化时间，紧凑可读"""
    if not value:
        return "-"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.strftime("%m-%d %H:%M") if with_date else dt.strftime("%H:%M")


def format_plan_output(state: PlanAgentState) -> str:
    """
    格式化排产计划输出

    结构：计划概要 -> 任务明细（含分段）-> 未排产 -> 冲突 -> 落库状态
    """
    tasks = state.get("production_tasks") or []
    unscheduled = state.get("unscheduled_tasks") or []
    schedule = state.get("schedule") or {}
    conflicts = state.get("conflicts") or []
    persist = state.get("persist_result") or {}

    lines: List[str] = []

    if not tasks and not unscheduled:
        err = schedule.get("error") or state.get("error")
        lines.append("排产未生成任何任务")
        if err:
            lines.append(f"原因: {err}")
        return "\n".join(lines)

    # ---- 概要 ----
    lines.append("=" * 52)
    lines.append("排产计划已生成")
    lines.append("=" * 52)
    lines.append(f"计划编号: {schedule.get('plan_id') or '(未落库)'}")
    lines.append(f"排产策略: {state.get('strategy')}  ({state.get('strategy_reason', '')})")
    lines.append(f"求解状态: {schedule.get('solver_status')}  "
                 f"耗时 {schedule.get('solve_time_seconds', 0)}s")
    lines.append(f"任务总数: {len(tasks)} 个（拆分为 {schedule.get('segments_count', 0)} 个生产时段）")
    lines.append(f"总工时:   {schedule.get('total_hours', 0)}h")
    if schedule.get("horizon_start"):
        lines.append(f"排产窗口: {schedule['horizon_start']} ~ {schedule.get('horizon_end')}")

    # ---- 任务明细 ----
    if tasks:
        lines.append("")
        lines.append("-" * 52)
        lines.append("任务明细")
        lines.append("-" * 52)
        for t in tasks:
            segs = t.get("segments") or []
            tard = float(t.get("tardiness_hours") or 0)
            flag = f"  [超期{tard}h]" if tard > 0 else ""
            lines.append(
                f"{t['task_id']}  订单{t.get('order_id')}  {t.get('product_id')}"
                f"  x{t.get('quantity')}{flag}"
            )
            lines.append(
                f"  {t.get('center_name')}({t.get('center_id')})"
                f"  {t.get('duration_hours')}h"
                + (f"  换型{t.get('setup_time_minutes', 0)}min" if t.get("setup_time_minutes", 0) > 0 else "")
                + f"  交期 {str(t.get('due_date') or '-')[:10]}"
            )
            if len(segs) > 1:
                lines.append(f"  跨 {len(segs)} 个工作日:")
                for s in segs:
                    lines.append(
                        f"    段{s['segment_index']}: {_fmt_dt(s['start_time'])}"
                        f" ~ {_fmt_dt(s['end_time'], False)}  ({s['duration_hours']}h)"
                    )
            elif segs:
                s = segs[0]
                lines.append(
                    f"  时间: {_fmt_dt(s['start_time'])} ~ "
                    f"{_fmt_dt(s['end_time'], False)}"
                )
            lines.append("")

    # ---- 未排产（必须显式告知）----
    if unscheduled:
        lines.append("-" * 52)
        lines.append(f"未能排产: {len(unscheduled)} 个任务")
        lines.append("-" * 52)
        for u in unscheduled:
            lines.append(
                f"  {u['task_id']}  订单{u.get('order_id')}  {u.get('product_id')}"
                f"  x{u.get('quantity')}  需{u.get('duration_hours')}h"
            )
            lines.append(f"    交期 {str(u.get('due_date') or '-')[:10]}  原因: {u.get('reason')}")
        lines.append("")

    # ---- 冲突 ----
    if conflicts:
        high = [c for c in conflicts if c.get("severity") == "high"]
        other = [c for c in conflicts if c.get("severity") != "high"]
        lines.append("-" * 52)
        lines.append(f"校验发现 {len(conflicts)} 项问题（高severity {len(high)} 项）")
        lines.append("-" * 52)
        for c in high + other:
            mark = "[高]" if c.get("severity") == "high" else "[中]"
            lines.append(f"  {mark} {c['description']}")
        lines.append("")

    # ---- 落库状态 ----
    lines.append("-" * 52)
    if state.get("plan_persisted"):
        lines.append(
            f"已写入数据库: {persist.get('segment_rows')} 条任务段, "
            f"产能日历更新 {persist.get('calendar_updates')} 行"
        )
        lines.append(f"计划状态: draft（草稿），确认后方可执行")
    elif tasks:
        lines.append("警告: 计划未写入数据库")
        if state.get("error"):
            lines.append(f"  原因: {state['error']}")

    return "\n".join(lines)


def format_shortage_output(state: PlanAgentState) -> str:
    """格式化物料缺料检查输出"""
    shortages = state.get("shortages") or []
    lines = ["物料缺料检查完成", ""]

    if not shortages:
        lines.append("无物料缺口，库存充足")
        return "\n".join(lines)

    by_risk: Dict[str, List[Dict]] = {}
    for s in shortages:
        by_risk.setdefault(s.get("risk_level", "medium"), []).append(s)

    lines.append(f"发现 {len(shortages)} 项物料缺口:")
    lines.append("")
    for level in ("critical", "high", "medium", "low"):
        items = by_risk.get(level) or []
        if not items:
            continue
        lines.append(f"[{level}] {len(items)} 项")
        for s in items:
            lines.append(
                f"  {s['material_name']}({s['material_id']}): "
                f"缺口 {s['shortage_quantity']}{s.get('unit', '')}"
            )
            lines.append(
                f"    需求 {s['required_quantity']}, 可用 {s['available_quantity']}, "
                f"需求日期 {s['required_date']}"
            )
            if s.get("purchase_deadline"):
                lines.append(
                    f"    采购下单期限 {s['purchase_deadline']}"
                    f"（提前期 {s.get('lead_time_days')} 天）"
                )
        lines.append("")

    return "\n".join(lines)


def format_delivery_output(state: PlanAgentState) -> str:
    """格式化交付达成检查输出"""
    risks = state.get("delivery_risks") or []
    lines = ["交付达成检查完成", ""]

    if not risks:
        lines.append("无交付风险，所有订单按计划进行")
        return "\n".join(lines)

    lines.append(f"发现 {len(risks)} 项交付风险:")
    lines.append("")
    for r in sorted(risks, key=lambda x: x.get("risk_level") != "critical"):
        lines.append(
            f"  [{r.get('risk_level')}] 订单{r['order_id']}"
            f"  {r.get('product_id') or ''}  交期 {r.get('due_date') or '-'}"
        )
        lines.append(f"    {r.get('reason')}")
    return "\n".join(lines)


def generate_alerts(state: PlanAgentState) -> List[Dict]:
    """
    生成预警信息

    TODO: 对接钉钉/企业微信推送，当前仅返回结构化预警供上层消费。
    """
    alerts: List[Dict[str, Any]] = []

    for s in state.get("shortages") or []:
        if s.get("risk_level") in ("high", "critical"):
            alerts.append({
                "type": "shortage",
                "level": s["risk_level"],
                "message": (
                    f"物料{s['material_name']}缺口 {s['shortage_quantity']}"
                    f"{s.get('unit', '')}，需在 {s.get('purchase_deadline', '?')} 前下单"
                ),
                "material_id": s["material_id"],
            })

    for r in state.get("delivery_risks") or []:
        if r.get("risk_level") in ("high", "critical"):
            alerts.append({
                "type": "delivery",
                "level": r["risk_level"],
                "message": f"订单{r['order_id']}交期风险: {r.get('reason')}",
                "order_id": r["order_id"],
            })

    # 未排产任务是硬缺口，必须预警
    for u in state.get("unscheduled_tasks") or []:
        alerts.append({
            "type": "unscheduled",
            "level": "critical",
            "message": (
                f"订单{u.get('order_id')}的任务{u['task_id']}无法排入当前窗口"
                f"（需{u.get('duration_hours')}h）"
            ),
            "order_id": u.get("order_id"),
        })

    for c in state.get("conflicts") or []:
        if c.get("severity") == "high" and c.get("type") != "unscheduled_task":
            alerts.append({
                "type": c["type"],
                "level": "high",
                "message": c["description"],
            })

    return alerts


def generate_recommendations(state: PlanAgentState) -> List[str]:
    """生成可执行建议，尽量给出具体动作而非空泛表述"""
    recs: List[str] = []

    shortages = state.get("shortages") or []
    risks = state.get("delivery_risks") or []
    gaps = state.get("capacity_gaps") or []
    unscheduled = state.get("unscheduled_tasks") or []
    tardy = [t for t in (state.get("production_tasks") or [])
             if float(t.get("tardiness_hours") or 0) > 0]

    if shortages:
        urgent = [s for s in shortages if s.get("risk_level") == "critical"]
        if urgent:
            deadlines = [s.get("purchase_deadline") for s in urgent if s.get("purchase_deadline")]
            when = min(deadlines) if deadlines else "尽快"
            recs.append(f"立即启动 {len(urgent)} 项紧急物料采购，最早下单期限 {when}")
        else:
            recs.append(f"安排 {len(shortages)} 项物料补货")

    if unscheduled:
        hours = sum(float(u.get("duration_hours") or 0) for u in unscheduled)
        recs.append(
            f"{len(unscheduled)} 个任务（共 {hours:.1f}h）无法排入窗口，"
            f"建议延长排产周期、安排加班或外协"
        )

    if tardy:
        worst = max(float(t.get("tardiness_hours") or 0) for t in tardy)
        recs.append(f"{len(tardy)} 个任务将超期（最长 {worst}h），建议与客户协商交期或调整优先级")

    if gaps:
        recs.append(f"{len(gaps)} 处产能瓶颈，建议评估增加班次或调整工艺路线")

    if risks and not tardy:
        recs.append("存在交付风险但当前计划可满足，建议密切跟踪执行进度")

    if state.get("plan_persisted"):
        recs.append(f"计划 {state.get('plan_id')} 处于草稿态，核对无误后请确认下发")

    # ---- RAG 知识增强 ----
    # 检索与当前排产情况相关的规则，补充更具体的操作建议
    from src.services.rag_helper import build_recommendation_query, search_knowledge

    query = build_recommendation_query(state)
    knowledge = search_knowledge(query, top_k=2)

    for k in knowledge:
        content = k["content"]
        # 避免与已有建议重复
        if not any(content[:20] in r for r in recs):
            recs.append(f"[规则参考] {content[:120]}")

    if not recs:
        recs.append("排产计划可行，建议确认执行")

    return recs


def output_node(state: PlanAgentState) -> Dict[str, Any]:
    """
    Output节点主函数

    额外职责：把预警与运行审计落库，形成可追溯记录。
    """
    request_type = state.get("request_type")

    if request_type in (RequestType.NEW_PLAN.value, RequestType.INSERT_ORDER.value,
                        RequestType.REBALANCE.value):
        output_content = format_plan_output(state)
    elif request_type == RequestType.CHECK_SHORTAGE.value:
        output_content = format_shortage_output(state)
    elif request_type == RequestType.CHECK_DELIVERY.value:
        output_content = format_delivery_output(state)
    else:
        output_content = state.get("output_content") or "处理完成"

    alerts = generate_alerts(state)
    recommendations = generate_recommendations(state)

    # ---- 预警与审计落库（失败不影响主流程输出）----
    persisted_alerts = {"shortage": 0, "delivery": 0}
    run_id = None
    try:
        persisted_alerts["shortage"] = writer.save_shortage_alerts(state.get("shortages") or [])
        persisted_alerts["delivery"] = writer.save_delivery_alerts(state.get("delivery_risks") or [])
        run_id = writer.save_run_log(dict(state), state.get("plan_id"))
    except Exception as e:
        logger.warning(f"预警/审计落库失败（不影响输出）: {type(e).__name__}: {e}")

    execution_log = state.get("execution_log") or []
    execution_log.append({
        "node": "output",
        "action": "format_output",
        "result": {
            "output_format": state.get("output_format", "text"),
            "alerts_count": len(alerts),
            "recommendations_count": len(recommendations),
            "alerts_persisted": persisted_alerts,
            "run_id": run_id,
            "timestamp": datetime.now().isoformat(),
        },
    })

    return {
        "output_content": output_content,
        "alerts": alerts,
        "recommendations": recommendations,
        "run_id": run_id,
        "end_time": datetime.now(),
        "execution_log": execution_log,
    }