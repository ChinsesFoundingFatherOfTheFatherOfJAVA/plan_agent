"""
智能排产计划Agent - Validate子图

职责：校验排产结果、评估冲突严重度、决定重试或放行

校验项（全部基于求解器输出的真实时间分段 segments）：
    1. 交期违反      任务完工晚于交期（求解器已按软约束最小化，此处如实报告残留超期）
    2. 未排产任务    产能不足导致放不下，属高严重度
    3. 资源重叠      同中心同版本时间重叠（DB 亦有排他约束兜底，此处提前拦截给出可读信息）
    4. 日产能超限    某中心某日累计工时超过产能日历可用工时
    5. 非工作时间    任务段落在班次之外（日历映射的回归防线）
    6. 落库失败      计划未能写入数据库

重试策略：
    仅当「重试可能改变结果」时才重试 —— 即存在交期违反或资源重叠。
    产能绝对不足（未排产）重试无意义，直接转人工，避免空转 3 次。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.models.state import ConflictSeverity, PlanAgentState


def _dt(value: Any) -> datetime:
    """ISO 字符串 -> datetime"""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _iter_segments(tasks: List[Dict]) -> List[Dict]:
    """展开所有任务段，附带所属任务信息"""
    out = []
    for t in tasks:
        segs = t.get("segments") or []
        if not segs and t.get("start_time"):
            segs = [{"segment_index": 0, "start_time": t["start_time"],
                     "end_time": t["end_time"],
                     "duration_hours": t.get("duration_hours", 0)}]
        for s in segs:
            out.append({
                "task_id": t["task_id"],
                "order_id": t.get("order_id"),
                "center_id": t.get("center_id"),
                "segment_index": s.get("segment_index", 0),
                "start": _dt(s["start_time"]),
                "end": _dt(s["end_time"]),
                "hours": float(s.get("duration_hours") or 0),
            })
    return out


def validate_schedule(state: PlanAgentState) -> Dict[str, Any]:
    """执行全部校验项，返回冲突列表"""
    tasks = state.get("production_tasks") or []
    unscheduled = state.get("unscheduled_tasks") or []
    calendar = state.get("capacity_calendar") or []
    conflicts: List[Dict[str, Any]] = []

    # ---- 1. 交期违反（求解器输出的残留超期）----
    for t in tasks:
        tard = float(t.get("tardiness_hours") or 0)
        if tard > 0:
            conflicts.append({
                "type": "due_date_violation",
                "severity": ConflictSeverity.HIGH.value,
                "task_id": t["task_id"],
                "order_id": t.get("order_id"),
                "tardiness_hours": tard,
                "description": (
                    f"任务{t['task_id']}（订单{t.get('order_id')}）完工 {t.get('end_time')}"
                    f" 超过交期 {t.get('due_date')}，超期 {tard}h"
                ),
            })

    # ---- 2. 未排产任务（产能绝对不足）----
    for u in unscheduled:
        conflicts.append({
            "type": "unscheduled_task",
            "severity": ConflictSeverity.HIGH.value,
            "task_id": u["task_id"],
            "order_id": u.get("order_id"),
            "description": (
                f"任务{u['task_id']}（订单{u.get('order_id')}，"
                f"需 {u.get('duration_hours')}h）{u.get('reason')}"
            ),
            "retryable": False,  # 重排无法创造产能
        })

    segments = _iter_segments(tasks)

    # ---- 3. 资源重叠 ----
    by_center: Dict[str, List[Dict]] = {}
    for s in segments:
        by_center.setdefault(s["center_id"], []).append(s)

    for center_id, segs in by_center.items():
        segs.sort(key=lambda x: x["start"])
        for i in range(len(segs) - 1):
            if segs[i]["end"] > segs[i + 1]["start"]:
                conflicts.append({
                    "type": "resource_overlap",
                    "severity": ConflictSeverity.HIGH.value,
                    "task_id_1": segs[i]["task_id"],
                    "task_id_2": segs[i + 1]["task_id"],
                    "center_id": center_id,
                    "description": (
                        f"工作中心{center_id}上 {segs[i]['task_id']}"
                        f"(段{segs[i]['segment_index']}) 与 {segs[i+1]['task_id']}"
                        f"(段{segs[i+1]['segment_index']}) 时间重叠"
                    ),
                })

    # ---- 4. 日产能超限 / 5. 非工作时间 ----
    cal_map = {
        (c["center_id"], str(c["work_date"])[:10]): float(c.get("available_hours") or 0)
        for c in calendar
    }
    daily: Dict[tuple, float] = {}
    for s in segments:
        key = (s["center_id"], s["start"].date().isoformat())
        daily[key] = daily.get(key, 0.0) + s["hours"]

        # 该日产能为 0（周末/停机）却排了任务 -> 日历映射出错
        if key in cal_map and cal_map[key] <= 0:
            conflicts.append({
                "type": "non_working_time",
                "severity": ConflictSeverity.HIGH.value,
                "task_id": s["task_id"],
                "center_id": s["center_id"],
                "description": (
                    f"任务{s['task_id']}段{s['segment_index']}排在 "
                    f"{key[1]}，但该日 {s['center_id']} 无可用产能"
                ),
            })

    for (center_id, day), hours in sorted(daily.items()):
        available = cal_map.get((center_id, day))
        if available is None:
            continue
        if hours > available + 0.01:
            conflicts.append({
                "type": "capacity_exceeded",
                "severity": ConflictSeverity.MEDIUM.value,
                "center_id": center_id,
                "work_date": day,
                "planned_hours": round(hours, 2),
                "available_hours": available,
                "description": (
                    f"{center_id} 在 {day} 排产 {hours:.1f}h，"
                    f"超过可用产能 {available:.1f}h"
                ),
            })

    # ---- 6. 落库失败 ----
    if tasks and not state.get("plan_persisted"):
        conflicts.append({
            "type": "persist_failed",
            "severity": ConflictSeverity.HIGH.value,
            "description": state.get("error") or "排产计划未能写入数据库",
            "retryable": False,
        })

    return {"conflicts": conflicts}


def evaluate_conflicts(state: PlanAgentState) -> Dict[str, Any]:
    """
    评估是否通过校验

    通过条件：无高严重度冲突。中低严重度允许放行但会在输出中提示。
    """
    conflicts = state.get("conflicts") or []
    high = [c for c in conflicts if c.get("severity") == ConflictSeverity.HIGH.value]
    return {"validation_passed": not high}


def check_validation_result(state: PlanAgentState) -> str:
    """
    校验后路由

    Returns:
        pass  校验通过 -> 输出
        retry 存在可通过重排改善的冲突且未超次数 -> 重新排产
        alert 无法通过重排解决，或已达重试上限 -> 输出并预警
    """
    if state.get("validation_passed"):
        return "pass"

    conflicts = state.get("conflicts") or []
    retry_count = state.get("retry_count", 0)
    max_retry = state.get("max_retry", 3)

    # 仅交期违反与资源重叠有重排价值；产能不足、落库失败重排无意义
    retryable = [
        c for c in conflicts
        if c.get("severity") == ConflictSeverity.HIGH.value
        and c.get("retryable") is not False
        and c.get("type") in ("due_date_violation", "resource_overlap", "non_working_time")
    ]

    if retryable and retry_count < max_retry:
        return "retry"
    return "alert"


def validate_node(state: PlanAgentState) -> Dict[str, Any]:
    """
    Validate节点主函数

    顺序：校验 -> 评估 -> 计数
    """
    out: Dict[str, Any] = {}

    out.update(validate_schedule(state))
    state.update(out)

    out.update(evaluate_conflicts(state))
    state.update(out)

    # 需要重试时递增计数，避免死循环
    retry_count = state.get("retry_count", 0)
    if check_validation_result(state) == "retry":
        retry_count += 1
    out["retry_count"] = retry_count

    conflicts = out.get("conflicts") or []
    by_type: Dict[str, int] = {}
    for c in conflicts:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1

    execution_log = state.get("execution_log") or []
    execution_log.append({
        "node": "validate",
        "action": "validate_all",
        "result": {
            "validation_passed": out.get("validation_passed"),
            "conflicts_count": len(conflicts),
            "conflicts_by_type": by_type,
            "retry_count": retry_count,
            "timestamp": datetime.now().isoformat(),
        },
    })
    out["execution_log"] = execution_log

    return out