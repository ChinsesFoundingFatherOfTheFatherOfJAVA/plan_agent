"""
排产结果落库服务（Plan Writer）

职责：把求解器产出的计划写入 PostgreSQL，并维护派生数据的一致性。

写入范围（同一事务，保证原子性）：
    production_plan          计划头
    production_task          任务段（跨天任务一段一行，见 migration 002）
    capacity_calendar        回写 planned_hours（按段所属日期累加）
    plan_run_log             求解审计
    shortage_alert           物料缺料预警
    delivery_alert           交付风险预警
    insert_order_log         插单扰动记录（插单场景）

关键约束处理：
    ExclusionViolation  同中心时间重叠 -> 由 DB 排他约束兜底，捕获后转为业务错误
                        （求解器已保证不重叠，触发即说明与库中既有计划冲突）
    UniqueViolation     重复写入 -> plan_id 冲突时报错，段序号冲突时报错
"""
import hashlib
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from psycopg2 import errors as pg_errors
from psycopg2.extras import execute_values

from src.services.db import pg_connection
from src.utils.logger import get_logger

logger = get_logger(__name__)


class PlanWriteError(Exception):
    """落库失败（约束冲突、数据不一致等）"""


# ==================== ID 生成 ====================

def new_plan_id() -> str:
    """生成计划ID：PLAN-YYYYMMDD-XXXX"""
    return f"PLAN-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"


def new_run_id() -> str:
    """生成运行审计ID"""
    return f"RUN-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:3].upper()}"


def _short_id(prefix: str) -> str:
    """生成短ID（预警类）"""
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:5].upper()}"


def _plan_scope_tag(plan_id: str, plan_version: int) -> str:
    """
    由 plan_id + 版本派生 4 位确定性短标签。

    用途：production_task.task_id 是全局主键，而段号 TASK-001-S0 仅在计划内唯一，
    不同计划排同一批订单会撞主键（migration 003 已允许多计划并存，此处必须配套）。
    使用哈希而非随机值，保证同一计划重复保存时 task_id 稳定，维持幂等。
    """
    raw = f"{plan_id}#{plan_version}".encode("utf-8")
    return hashlib.blake2s(raw, digest_size=2).hexdigest().upper()


def _segment_task_id(plan_id: str, plan_version: int, parent: str,
                     idx: int, count: int) -> str:
    """
    生成段主键，形如 TASK-001-S0-9F3A。

    规则：始终附加计划标签以保证跨计划唯一；单段任务不加 -S0 后缀，保持可读。
    超长时截断 parent 部分，确保总长不超过 VARCHAR(32)。
    """
    tag = _plan_scope_tag(plan_id, plan_version)
    suffix = f"-{tag}" if count == 1 else f"-S{idx}-{tag}"
    room = 32 - len(suffix)
    return f"{parent[:room]}{suffix}"


# ==================== 主入口 ====================

def save_plan(
    tasks: List[Dict[str, Any]],
    schedule: Dict[str, Any],
    plan_name: Optional[str] = None,
    created_by: str = "agent",
    status: str = "draft",
    plan_version: int = 1,
    update_calendar: bool = True,
) -> Dict[str, Any]:
    """
    保存排产计划（计划头 + 任务段 + 产能回写），单事务。

    Args:
        tasks: 求解器输出的任务列表，每项须含 segments
        schedule: 计划元信息（plan_id/plan_date/horizon_start/horizon_end）
        plan_name: 计划名称
        created_by: 创建者
        status: 计划状态，默认 draft（需人工确认后转 confirmed）
        plan_version: 计划版本号
        update_calendar: 是否回写 capacity_calendar.planned_hours

    Returns:
        {plan_id, task_rows, segments, total_hours, centers}

    Raises:
        PlanWriteError: 时间重叠或唯一约束冲突
    """
    plan_id = (schedule or {}).get("plan_id") or new_plan_id()
    plan_date = _as_date(
        (schedule or {}).get("plan_date")) or datetime.now().date()

    horizon_start, horizon_end = _horizon_from_tasks(tasks, schedule)

    # 展开任务段：一段一行
    rows = _build_task_rows(plan_id, plan_version, tasks)

    try:
        with pg_connection() as conn:
            with conn.cursor() as cur:
                # ---- 1. 计划头 ----
                cur.execute(
                    """INSERT INTO production_plan
                           (plan_id, plan_name, plan_date, horizon_start, horizon_end,
                            status, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (plan_id) DO UPDATE SET
                           plan_name     = EXCLUDED.plan_name,
                           horizon_start = EXCLUDED.horizon_start,
                           horizon_end   = EXCLUDED.horizon_end,
                           status        = EXCLUDED.status""",
                    (plan_id, plan_name or f"自动排产 {plan_date}", plan_date,
                     horizon_start, horizon_end, status, created_by),
                )

                # ---- 2. 任务段 ----
                if rows:
                    # 重跑同一版本时先清理，保证幂等
                    cur.execute(
                        "DELETE FROM production_task WHERE plan_id = %s AND plan_version = %s",
                        (plan_id, plan_version),
                    )
                    execute_values(
                        cur,
                        """INSERT INTO production_task
                               (task_id, plan_id, plan_version, parent_task_id,
                                segment_index, segment_count, order_id, item_id,
                                product_id, center_id, quantity, start_time, end_time,
                                status, priority, duration_hours, due_date,
                                tardiness_hours, setup_time_minutes)
                           VALUES %s""",
                        rows,
                    )

                # ---- 3. 产能日历回写 ----
                calendar_updates = 0
                if update_calendar and rows:
                    calendar_updates = _update_planned_hours(cur, plan_id, tasks)

        total_hours = sum(
            s["duration_hours"] for t in tasks for s in t.get("segments", []))
        centers = sorted({t["center_id"] for t in tasks if t.get("center_id")})

        logger.info(
            f"计划落库成功 plan_id={plan_id} 任务={len(tasks)} 段={len(rows)} "
            f"工时={total_hours:.1f}h 日历更新={calendar_updates}行"
        )
        return {
            "plan_id": plan_id,
            "plan_version": plan_version,
            "task_count": len(tasks),
            "segment_rows": len(rows),
            "total_hours": round(total_hours, 2),
            "centers": centers,
            "horizon_start": _iso(horizon_start),
            "horizon_end": _iso(horizon_end),
            "calendar_updates": calendar_updates,
        }

    except pg_errors.ExclusionViolation as e:
        # 求解器内部已保证不重叠，走到这里说明与库中既有计划（其他 plan_id）冲突
        raise PlanWriteError(
            f"任务时间与已有计划重叠，被数据库排他约束拒绝。"
            f"插单场景请先加载既有任务作为固定占用。原始错误: {_first_line(e)}"
        ) from e
    except pg_errors.UniqueViolation as e:
        raise PlanWriteError(f"计划或任务段主键重复: {_first_line(e)}") from e
    except pg_errors.ForeignKeyViolation as e:
        raise PlanWriteError(
            f"外键校验失败（订单/明细/工作中心在库中不存在）: {_first_line(e)}") from e


def _build_task_rows(
    plan_id: str, plan_version: int, tasks: List[Dict[str, Any]]
) -> List[Tuple]:
    """
    把任务展开为「段行」。

    task_id 规则：
        单段任务 -> 沿用逻辑任务号（TASK-001）
        多段任务 -> TASK-001-S0 / -S1 ...（保证主键唯一且可读）
    """
    rows: List[Tuple] = []
    for t in tasks:
        segments = t.get("segments") or []
        if not segments:
            # 兜底：无分段信息时按整段写入
            if not t.get("start_time") or not t.get("end_time"):
                continue
            segments = [{
                "segment_index": 0,
                "start_time": t["start_time"],
                "end_time": t["end_time"],
                "duration_hours": t.get("duration_hours"),
            }]

        parent = t["task_id"]
        count = len(segments)
        for seg in segments:
            idx = seg.get("segment_index", 0)
            seg_task_id = _segment_task_id(plan_id, plan_version, parent, idx, count)
            rows.append((
                seg_task_id,
                plan_id,
                plan_version,
                parent[:32],
                idx,
                count,
                t.get("order_id"),
                t.get("item_id"),
                t["product_id"],
                t["center_id"],
                t["quantity"],
                seg["start_time"],
                seg["end_time"],
                t.get("status", "planned"),
                t.get("priority", 2),
                seg.get("duration_hours"),
                _as_date(t.get("due_date")),
                t.get("tardiness_hours", 0),
                t.get("setup_time_minutes", 0),
            ))
    return rows


def _update_planned_hours(cur, plan_id: str, tasks: List[Dict[str, Any]]) -> int:
    """
    通过 capacity_allocation 明细表回写产能日历（多计划安全）。

    流程：
        1. 删除该 plan_id 的旧 allocation
        2. 写入新 allocation（按 center_id + work_date 聚合，含换型时间）
        3. 从 allocation 汇总重算涉及范围内 capacity_calendar.planned_hours

    多个计划并存时，每个计划只维护自己的 allocation，
    planned_hours 从全表汇总，不会互相覆盖。
    """
    # ---- 1. 汇总 (center_id, work_date) -> (planned_hours, setup_hours) ----
    agg: Dict[Tuple[str, date], Tuple[float, float]] = {}
    for t in tasks:
        cid = t.get("center_id")
        if not cid:
            continue
        setup_min = float(t.get("setup_time_minutes") or 0)
        setup_hours = setup_min / 60.0
        first_date = None
        for seg in t.get("segments") or []:
            d = datetime.fromisoformat(str(seg["start_time"])).date()
            if first_date is None:
                first_date = d
                # 换型时间计入第一段所在日期
                ph, sh = agg.get((cid, d), (0.0, 0.0))
                agg[(cid, d)] = (ph + setup_hours, sh + setup_hours)
            ph, sh = agg.get((cid, d), (0.0, 0.0))
            agg[(cid, d)] = (ph + float(seg.get("duration_hours") or 0), sh)

    if not agg:
        return 0

    centers = sorted({c for c, _ in agg})
    dates = sorted({d for _, d in agg})

    # ---- 2. 删除该 plan_id 的旧 allocation ----
    cur.execute(
        """DELETE FROM capacity_allocation
           WHERE plan_id = %s AND center_id = ANY(%s)
             AND work_date BETWEEN %s AND %s""",
        (plan_id, centers, dates[0], dates[-1]),
    )

    # ---- 3. 写入新 allocation ----
    alloc_rows = [
        (plan_id, c, d, round(ph, 2), round(sh, 2))
        for (c, d), (ph, sh) in agg.items()
    ]
    execute_values(
        cur,
        """INSERT INTO capacity_allocation
               (plan_id, center_id, work_date, planned_hours, setup_hours)
           VALUES %s
           ON CONFLICT (plan_id, center_id, work_date)
           DO UPDATE SET planned_hours = EXCLUDED.planned_hours,
                         setup_hours = EXCLUDED.setup_hours""",
        alloc_rows,
    )

    # ---- 4. 从 allocation 汇总重算 capacity_calendar.planned_hours ----
    cur.execute(
        """UPDATE capacity_calendar AS cc SET planned_hours = COALESCE(sub.total, 0)
           FROM (
               SELECT center_id, work_date, SUM(planned_hours) AS total
               FROM capacity_allocation
               WHERE center_id = ANY(%s) AND work_date BETWEEN %s AND %s
               GROUP BY center_id, work_date
           ) AS sub
           WHERE cc.center_id = sub.center_id AND cc.work_date = sub.work_date""",
        (centers, dates[0], dates[-1]),
    )

    # 清零没有 allocation 的日期（该范围内可能旧数据被删了）
    cur.execute(
        """UPDATE capacity_calendar SET planned_hours = 0
           WHERE center_id = ANY(%s) AND work_date BETWEEN %s AND %s
             AND (center_id, work_date) NOT IN (
                 SELECT center_id, work_date FROM capacity_allocation
                 WHERE center_id = ANY(%s) AND work_date BETWEEN %s AND %s
             )""",
        (centers, dates[0], dates[-1], centers, dates[0], dates[-1]),
    )

    return len(alloc_rows)


# ==================== 预警落库 ====================

def save_shortage_alerts(shortages: List[Dict[str, Any]]) -> int:
    """
    保存物料缺料预警。

    同物料 + 同需求日期已有 active 预警时更新数量，避免重复告警。
    """
    if not shortages:
        return 0

    rows = [(
        _short_id("SA"),
        s["material_id"],
        _as_date(s.get("required_date")),
        s.get("required_quantity", 0),
        s.get("available_quantity", 0),
        s.get("shortage_quantity", 0),
        s.get("risk_level", "medium"),
        "active",
    ) for s in shortages if s.get("material_id") and s.get("required_date")]

    if not rows:
        return 0

    with pg_connection() as conn:
        with conn.cursor() as cur:
            # 同物料同日期的旧 active 预警先关闭，保证「一物料一日期一条有效预警」
            keys = [(r[1], r[2]) for r in rows]
            execute_values(
                cur,
                """UPDATE shortage_alert AS a SET status = 'resolved'
                   FROM (VALUES %s) AS v(material_id, required_date)
                   WHERE a.material_id = v.material_id
                     AND a.required_date = v.required_date::date
                     AND a.status = 'active'""",
                keys,
            )
            execute_values(
                cur,
                """INSERT INTO shortage_alert
                       (alert_id, material_id, required_date, required_quantity,
                        available_quantity, shortage_quantity, risk_level, status)
                   VALUES %s""",
                rows,
            )
    logger.info(f"写入缺料预警 {len(rows)} 条")
    return len(rows)


def save_delivery_alerts(risks: List[Dict[str, Any]]) -> int:
    """
    保存交付风险预警。

    delay_days 由「预计完工 - 交期」推算；无完工信息时置 NULL。
    """
    if not risks:
        return 0

    level_map = {"critical": "critical", "high": "delayed", "medium": "at_risk", "low": "at_risk"}
    rows = []
    for r in risks:
        if not r.get("order_id") or not r.get("due_date"):
            continue
        required = _as_date(r["due_date"])
        expected = _as_date(r.get("expected_date"))
        delay = (expected - required).days if expected and required else None
        rows.append((
            _short_id("DA"),
            r["order_id"],
            r.get("item_id"),
            expected,
            required,
            delay,
            level_map.get(r.get("risk_level", "medium"), "at_risk"),
            "active",
        ))

    if not rows:
        return 0

    with pg_connection() as conn:
        with conn.cursor() as cur:
            keys = [(r[1], r[4]) for r in rows]
            execute_values(
                cur,
                """UPDATE delivery_alert AS a SET status = 'resolved'
                   FROM (VALUES %s) AS v(order_id, required_date)
                   WHERE a.order_id = v.order_id
                     AND a.required_date = v.required_date::date
                     AND a.status = 'active'""",
                keys,
            )
            execute_values(
                cur,
                """INSERT INTO delivery_alert
                       (alert_id, order_id, item_id, expected_date, required_date,
                        delay_days, alert_type, status)
                   VALUES %s""",
                rows,
            )
    logger.info(f"写入交付预警 {len(rows)} 条")
    return len(rows)


# ==================== 审计日志 ====================

def save_run_log(state: Dict[str, Any], plan_id: Optional[str]) -> str:
    """
    写入排产运行审计，便于追溯「这版计划为何如此排」。
    """
    run_id = new_run_id()
    schedule = state.get("schedule") or {}
    stats = schedule.get("statistics") or {}

    # plan_id 有外键约束：计划头未写入时（如全部任务未排产、落库失败）必须置空，
    # 否则审计日志本身会因外键失败而丢失 —— 而这恰是最需要留痕的场景。
    if plan_id and not state.get("plan_persisted"):
        plan_id = None

    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO plan_run_log
                       (run_id, plan_id, request_type, user_input, strategy,
                        solver_status, tasks_total, tasks_scheduled, tasks_unscheduled,
                        tardy_tasks, solve_seconds, validation_passed,
                        conflicts_count, retry_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    plan_id,
                    state.get("request_type") or "unknown",
                    (state.get("user_input") or "")[:2000],
                    state.get("strategy"),
                    schedule.get("solver_status"),
                    stats.get("total_tasks", len(state.get("production_tasks") or [])),
                    len(state.get("production_tasks") or []),
                    len(state.get("unscheduled_tasks") or []),
                    stats.get("tardy_tasks", 0),
                    schedule.get("solve_time_seconds"),
                    state.get("validation_passed"),
                    len(state.get("conflicts") or []),
                    state.get("retry_count", 0),
                ),
            )
    return run_id


def save_insert_order_log(
    order_id: str,
    disruption: Dict[str, Any],
    original_plan_id: Optional[str],
    new_plan_id_: Optional[str],
    affected_tasks: List[Dict[str, Any]] = None,
) -> str:
    """
    保存插单扰动记录及受影响任务明细。
    """
    log_id = _short_id("IOL")
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO insert_order_log
                       (log_id, order_id, original_plan_id, new_plan_id,
                        disruption_score, disruption_level, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (log_id, order_id, original_plan_id, new_plan_id_,
                 disruption.get("score"), disruption.get("level"), "completed"),
            )
            if affected_tasks:
                execute_values(
                    cur,
                    """INSERT INTO insert_order_affected_task
                           (log_id, task_id, impact_type, old_start_time,
                            new_start_time, delay_minutes)
                       VALUES %s
                       ON CONFLICT (log_id, task_id) DO NOTHING""",
                    [(log_id, a["task_id"], a.get("impact_type", "moved"),
                      a.get("old_start_time"), a.get("new_start_time"),
                      a.get("delay_minutes")) for a in affected_tasks],
                )
    return log_id


def confirm_plan(plan_id: str) -> int:
    """确认计划：draft -> confirmed，任务 planned -> scheduled

    处理逻辑（同一事务）：
        1. 将已有的 confirmed/executing 计划置为 cancelled（uk_plan_active 唯一约束只允许一个 confirmed/executing）
        2. 将目标计划从 draft 置为 confirmed
        3. 将该计划下的任务从 planned 置为 scheduled
    """
    with pg_connection() as conn:
        with conn.cursor() as cur:
            # 1. 归档旧 confirmed/executing 计划（排除当前计划）
            cur.execute(
                "UPDATE production_plan SET status='cancelled' "
                "WHERE status IN ('confirmed','executing') AND plan_id != %s",
                (plan_id,),
            )
            archived = cur.rowcount
            if archived:
                logger.info(f"已取消(归档) {archived} 个旧确认计划")

            # 2. 确认目标计划
            cur.execute(
                "UPDATE production_plan SET status='confirmed' WHERE plan_id=%s AND status='draft'",
                (plan_id,),
            )
            n = cur.rowcount
            if n == 0:
                raise ValueError(f"计划 {plan_id} 不存在或非 draft 状态")

            # 3. 任务状态转为 scheduled
            cur.execute(
                "UPDATE production_task SET status='scheduled' WHERE plan_id=%s AND status='planned'",
                (plan_id,),
            )
            logger.info(f"计划 {plan_id} 已确认，任务已转为 scheduled")
    return n


def delete_plan(plan_id: str) -> int:
    """删除草稿计划（仅 draft 状态可删）

    同一事务内：
        1. 校验计划存在且为 draft 状态
        2. 删除关联的 capacity_allocation
        3. 删除关联的 production_task
        4. 删除 production_plan 本身
    """
    with pg_connection() as conn:
        with conn.cursor() as cur:
            # 校验状态
            cur.execute(
                "SELECT status FROM production_plan WHERE plan_id=%s",
                (plan_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"计划 {plan_id} 不存在")
            if row[0] != "draft":
                raise ValueError(f"计划 {plan_id} 状态为 {row[0]}，仅 draft 可删除")

            # 删除关联数据（capacity_allocation 有外键约束）
            cur.execute("DELETE FROM capacity_allocation WHERE plan_id=%s", (plan_id,))
            cur.execute("DELETE FROM production_task WHERE plan_id=%s", (plan_id,))
            cur.execute("DELETE FROM production_plan WHERE plan_id=%s", (plan_id,))
            n = cur.rowcount
            logger.info(f"已删除计划 {plan_id}（{n} 行）")
    return n


# ==================== 工具 ====================

def _horizon_from_tasks(
    tasks: List[Dict[str, Any]], schedule: Dict[str, Any]
) -> Tuple[Optional[date], Optional[date]]:
    """由任务段的真实时间推导计划视界，优先使用实际排产结果"""
    starts, ends = [], []
    for t in tasks or []:
        for seg in t.get("segments") or []:
            starts.append(datetime.fromisoformat(str(seg["start_time"])).date())
            ends.append(datetime.fromisoformat(str(seg["end_time"])).date())

    if starts and ends:
        return min(starts), max(ends)

    s = _as_date((schedule or {}).get("horizon_start"))
    e = _as_date((schedule or {}).get("horizon_end"))
    if s and not e:
        e = s + timedelta(days=7)
    return s, e


def _as_date(value: Any) -> Optional[date]:
    """ISO 字符串 / datetime / date -> date"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _iso(value: Any) -> Optional[str]:
    """date -> ISO 字符串"""
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _first_line(e: Exception) -> str:
    """截取数据库错误首行，避免日志过长"""
    return str(e).strip().split("\n")[0][:300]