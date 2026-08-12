"""
智能排产计划Agent - Celery异步任务

所有任务均已接入真实业务逻辑（v1.5）：
    run_scheduling          调用 LangGraph 执行完整排产流程
    check_shortage          走图的 check_shortage 路径
    check_delivery_progress 走图的 check_delivery 路径
    generate_weekly_report  汇总库内数据生成周报
    insert_order_task       插单：评估扰动 -> 重排 -> 记录影响

设计要点：
    - LLM 不可用时降级为规则式 input_node（不阻塞排产核心链路）
    - 每个任务都做 try/except 并返回结构化结果，不抛异常给 Celery
    - PROGRESS 状态由图的 execution_log 驱动，不再用 sleep 模拟
"""
from celery import Celery
from celery.schedules import crontab
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import json
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

# 创建Celery应用
celery_app = Celery(
    "plan_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

# 配置
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_soft_time_limit=300,
    task_time_limit=600,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    result_expires=3600,
    worker_concurrency=4,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "daily-shortage-check": {
            "task": "src.services.celery_app.check_shortage",
            "schedule": crontab(hour=2, minute=0),
            "args": (),
        },
        "hourly-delivery-check": {
            "task": "src.services.celery_app.check_delivery_progress",
            "schedule": crontab(minute=0),
            "args": (),
        },
        "weekly-report": {
            "task": "src.services.celery_app.generate_weekly_report",
            "schedule": crontab(hour=8, minute=0, day_of_week=1),
            "args": (),
        },
    },
)


# ============ LLM 工厂 ============

def _get_llm():
    """
    获取 LLM 实例，失败时返回 None。

    Celery worker 运行在独立进程中，LLM 初始化可能因网络/配额失败。
    降级为 None 时 input_node 走规则式解析，排产核心链路不受影响。
    """
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.llm_model_name,
            temperature=0,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or None,
        )
    except Exception as e:
        logger.warning(f"LLM 初始化失败，降级为规则式解析: {type(e).__name__}: {e}")
        return None


class RuleBasedLLM:
    """
    规则式 LLM 降级方案

    当真实 LLM 不可用时，按关键词推断请求类型。
    覆盖率有限，但保证排产核心链路可执行。
    """

    def invoke(self, messages):
        # 从 messages 中提取用户输入文本
        text = ""
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict):
                text = last.get("content", "")
            elif hasattr(last, "content"):
                text = last.content
        elif isinstance(messages, str):
            text = messages

        text_lower = text.lower()
        if "插单" in text or "insert" in text_lower:
            rt = "insert_order"
        elif "缺料" in text or "shortage" in text_lower:
            rt = "check_shortage"
        elif "交付" in text or "delivery" in text_lower:
            rt = "check_delivery"
        elif "重平衡" in text or "rebalance" in text_lower:
            rt = "rebalance"
        else:
            rt = "new_plan"

        class R:
            def __init__(self):
                self.content = json.dumps({
                    "request_type": rt,
                    "params": {},
                    "confidence": 0.6,
                    "reasoning": "rule-based fallback",
                }, ensure_ascii=False)

        return R()


class _MockLLM:
    """内部用：已知请求类型的 Mock LLM，跳过真实 LLM 调用"""

    def __init__(self, request_type: str):
        self._rt = request_type

    def invoke(self, messages):
        """返回预设意图 JSON。messages 参数未使用，仅保持接口兼容。"""
        import json
        class R:
            def __init__(self):
                self.content = json.dumps({
                    "request_type": self._rt,
                    "params": {},
                    "confidence": 1.0,
                    "reasoning": "skip LLM - intent known",
                }, ensure_ascii=False)
        return R()



def _get_llm_or_fallback():
    """获取 LLM，不可用时返回规则式降级"""
    llm = _get_llm()
    return llm if llm is not None else RuleBasedLLM()


# ============ 排产任务 ============

@celery_app.task(bind=True, name="src.services.celery_app.run_scheduling")
def run_scheduling(self, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行异步排产任务

    Args:
        params: {
            user_input: 用户自然语言指令（可选，默认"安排生产计划"）
            request_type: 直接指定请求类型（可选，跳过 LLM 解析）
        }

    Returns:
        排产结果摘要（plan_id, 任务数, 段数, 工时, 落库状态, 预警）
    """
    try:
        self.update_state(state="PROGRESS", meta={"progress": 10, "status": "初始化"})

        from src.agent.graphs.plan_graph import build_plan_graph
        from src.models.state import create_initial_state

        user_input = params.get("user_input", "安排下周的生产计划")
        # 如果调用方指定了 request_type，跳过 LLM 初始化
        if params.get("request_type"):
            from src.services.celery_app import _MockLLM
            llm = _MockLLM(params["request_type"])
        else:
            llm = _get_llm_or_fallback()
        graph = build_plan_graph(llm)

        self.update_state(state="PROGRESS", meta={"progress": 30, "status": "执行排产图"})

        initial_state = create_initial_state(user_input)
        # 如果直接指定了请求类型，预置到 state 中
        if params.get("request_type"):
            initial_state["request_type"] = params["request_type"]

        result = graph.invoke(initial_state)

        self.update_state(state="PROGRESS", meta={"progress": 90, "status": "汇总结果"})

        tasks = result.get("production_tasks") or []
        unscheduled = result.get("unscheduled_tasks") or []
        schedule = result.get("schedule") or {}
        persist = result.get("persist_result") or {}

        return {
            "success": True,
            "plan_id": result.get("plan_id"),
            "request_type": result.get("request_type"),
            "strategy": result.get("strategy"),
            "tasks_count": len(tasks),
            "unscheduled_count": len(unscheduled),
            "segments_count": schedule.get("segments_count", 0),
            "total_hours": schedule.get("total_hours", 0),
            "solver_status": schedule.get("solver_status"),
            "solve_time": schedule.get("solve_time_seconds"),
            "persisted": result.get("plan_persisted", False),
            "segment_rows": persist.get("segment_rows"),
            "alerts_count": len(result.get("alerts") or []),
            "validation_passed": result.get("validation_passed"),
            "run_id": result.get("run_id"),
            "output_preview": (result.get("output_content") or "")[:500],
        }

    except Exception as e:
        logger.exception("排产任务异常")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ============ 缺料检查 ============

@celery_app.task(name="src.services.celery_app.check_shortage")  # 无 bind=True：本任务不调用 self.update_state
def check_shortage() -> Dict[str, Any]:
    """
    物料缺料检查（定时任务：每天凌晨2点）

    走图的 check_shortage 路径，结果含缺口明细并自动落库预警。
    """
    try:
        from src.agent.graphs.plan_graph import build_plan_graph
        from src.models.state import create_initial_state

        # check_shortage 已知意图，跳过 LLM 调用以避免不必要的延迟
        from src.services.celery_app import _MockLLM
        llm = _MockLLM("check_shortage")
        graph = build_plan_graph(llm)
        result = graph.invoke(create_initial_state("检查所有物料的缺料情况"))

        shortages = result.get("shortages") or []
        alerts = result.get("alerts") or []

        return {
            "success": True,
            "shortage_count": len(shortages),
            "alerts_count": len(alerts),
            "critical_count": sum(1 for s in shortages if s.get("risk_level") == "critical"),
            "shortages": [
                {"material_id": s["material_id"], "material_name": s.get("material_name"),
                 "shortage_quantity": s.get("shortage_quantity"), "risk_level": s.get("risk_level"),
                 "required_date": s.get("required_date")}
                for s in shortages
            ],
            "run_id": result.get("run_id"),
            "message": f"发现 {len(shortages)} 项物料缺口" if shortages else "物料充足",
        }

    except Exception as e:
        logger.exception("缺料检查异常")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ============ 交付进度检查 ============

@celery_app.task(name="src.services.celery_app.check_delivery_progress")  # 无 bind=True：本任务不调用 self.update_state
def check_delivery_progress() -> Dict[str, Any]:
    """
    交付进度检查（定时任务：每小时）

    走图的 check_delivery 路径，识别交付风险并落库预警。
    """
    try:
        from src.agent.graphs.plan_graph import build_plan_graph
        from src.models.state import create_initial_state

        # check_delivery 已知意图，跳过 LLM 调用以避免不必要的延迟
        from src.services.celery_app import _MockLLM
        llm = _MockLLM("check_delivery")
        graph = build_plan_graph(llm)
        result = graph.invoke(create_initial_state("检查所有订单的交付达成情况"))

        risks = result.get("delivery_risks") or []

        return {
            "success": True,
            "at_risk_count": len(risks),
            "critical_count": sum(1 for r in risks if r.get("risk_level") == "critical"),
            "delayed_count": sum(1 for r in risks if r.get("risk_level") == "high"),
            "risks": [
                {"order_id": r["order_id"], "risk_level": r.get("risk_level"),
                 "reason": r.get("reason"), "due_date": r.get("due_date")}
                for r in risks
            ],
            "run_id": result.get("run_id"),
            "message": f"发现 {len(risks)} 项交付风险" if risks else "所有订单按计划进行",
        }

    except Exception as e:
        logger.exception("交付检查异常")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ============ 周报 ============

@celery_app.task(name="src.services.celery_app.generate_weekly_report")
def generate_weekly_report() -> Dict[str, Any]:
    """
    生成周报（定时任务：每周一8点）

    汇总库内 production_plan / production_task / shortage_alert / delivery_alert
    生成上周的排产执行摘要。不依赖图，直接查库。
    """
    try:
        from src.services.db import pg_query
        from src.services import plan_writer

        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday()) - timedelta(days=7)  # 上周一
        week_end = week_start + timedelta(days=6)  # 上周日

        # 计划统计
        plans = pg_query(
            """SELECT status, COUNT(*) AS cnt FROM production_plan
               WHERE plan_date BETWEEN %s AND %s GROUP BY status""",
            (week_start, week_end),
        )
        plan_stats = {r["status"]: r["cnt"] for r in plans}

        # 任务统计
        task_stats = pg_query(
            """SELECT status, COUNT(*) AS cnt, COALESCE(SUM(duration_hours),0) AS hours
               FROM production_task
               WHERE start_time >= %s AND start_time < %s
               GROUP BY status""",
            (week_start, week_end + timedelta(days=1)),
        )

        # 预警统计
        shortage_stats = pg_query(
            """SELECT risk_level, COUNT(*) AS cnt FROM shortage_alert
               WHERE created_at >= %s AND created_at < %s GROUP BY risk_level""",
            (week_start, week_end + timedelta(days=1)),
        )
        delivery_stats = pg_query(
            """SELECT alert_type, COUNT(*) AS cnt FROM delivery_alert
               WHERE created_at >= %s AND created_at < %s GROUP BY alert_type""",
            (week_start, week_end + timedelta(days=1)),
        )

        total_tasks = sum(r["cnt"] for r in task_stats)
        total_hours = sum(float(r["hours"]) for r in task_stats)

        return {
            "success": True,
            "period": f"{week_start.isoformat()} ~ {week_end.isoformat()}",
            "plans": plan_stats,
            "tasks": {r["status"]: {"count": r["cnt"], "hours": float(r["hours"])} for r in task_stats},
            "total_tasks": total_tasks,
            "total_hours": round(total_hours, 1),
            "shortage_alerts": {r["risk_level"]: r["cnt"] for r in shortage_stats},
            "delivery_alerts": {r["alert_type"]: r["cnt"] for r in delivery_stats},
            "generated_at": datetime.now().isoformat(),
            "message": f"周报生成完成：{total_tasks} 个任务，{total_hours:.1f} 工时",
        }

    except Exception as e:
        logger.exception("周报生成异常")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ============ 插单 ============

@celery_app.task(bind=True, name="src.services.celery_app.insert_order_task")
def insert_order_task(self, order_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    插单任务

    流程：
        1. 把新订单写入 sales_order / sales_order_item（如果尚未存在）
        2. 走图的 insert_order 路径，求解器自动加载既有任务作为固定占用
        3. 对比新旧计划，评估扰动并记录到 insert_order_log
        4. 返回扰动评分与受影响任务

    Args:
        order_data: {
            order_id: 订单号（如库中不存在则创建）
            customer_id, required_date, priority,
            items: [{item_id, product_id, quantity, due_date}]
        }
    """
    try:
        self.update_state(state="PROGRESS", meta={"progress": 10, "status": "准备插单数据"})

        from src.services.db import pg_query, pg_execute
        from src.services import plan_writer, repository as repo

        order_id = order_data.get("order_id")
        if not order_id:
            return {"success": False, "error": "缺少 order_id"}

        # ---- 1. 确保订单在库中存在 ----
        existing = pg_query(
            "SELECT order_id FROM sales_order WHERE order_id = %s", (order_id,)
        )

        original_plan = repo.fetch_latest_plan()

        if not existing:
            self.update_state(state="PROGRESS", meta={"progress": 20, "status": "写入新订单"})
            pg_execute(
                """INSERT INTO sales_order (order_id, customer_id, order_date, required_date, priority, status)
                   VALUES (%s, %s, %s, %s, %s, 'confirmed')""",
                (order_id,
                 order_data.get("customer_id", "CUST-NEW"),
                 datetime.now().date(),
                 order_data.get("required_date"),
                 order_data.get("priority", 1)),
            )
            for item in order_data.get("items", []):
                pg_execute(
                    """INSERT INTO sales_order_item (item_id, order_id, product_id, quantity, due_date)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (item["item_id"], order_id, item["product_id"],
                     item["quantity"], item.get("due_date")),
                )

        # ---- 2. 走图重排（insert_order 路径） ----
        self.update_state(state="PROGRESS", meta={"progress": 40, "status": "执行重排"})
        from src.agent.graphs.plan_graph import build_plan_graph
        from src.models.state import create_initial_state

        # insert_order 已知意图，跳过 LLM 调用以避免不必要的延迟
        from src.services.celery_app import _MockLLM
        llm = _MockLLM("insert_order")
        graph = build_plan_graph(llm)

        user_input = f"订单{order_id}紧急，帮我插单"
        initial_state = create_initial_state(user_input)
        initial_state["request_type"] = "insert_order"
        result = graph.invoke(initial_state)

        new_plan_id = result.get("plan_id")
        tasks = result.get("production_tasks") or []
        unscheduled = result.get("unscheduled_tasks") or []
        persisted = result.get("plan_persisted", False)

        # ---- 3. 评估扰动 ----
        self.update_state(state="PROGRESS", meta={"progress": 70, "status": "评估扰动"})

        disruption = _evaluate_disruption(
            original_plan=original_plan,
            new_plan_id=new_plan_id,
            new_tasks=tasks,
            order_id=order_id,
        )

        # ---- 4. 记录插单日志 ----
        if persisted and new_plan_id:
            affected = _find_affected_tasks(original_plan, new_plan_id, tasks)
            try:
                log_id = plan_writer.save_insert_order_log(
                    order_id=order_id,
                    disruption=disruption,
                    original_plan_id=original_plan.get("plan_id") if original_plan else None,
                    new_plan_id_=new_plan_id,
                    affected_tasks=affected,
                )
            except Exception as e:
                logger.warning(f"插单日志写入失败（不影响结果）: {e}")
                log_id = None
        else:
            log_id = None

        self.update_state(state="PROGRESS", meta={"progress": 100, "status": "完成"})

        return {
            "success": True,
            "order_id": order_id,
            "new_plan_id": new_plan_id,
            "original_plan_id": original_plan.get("plan_id") if original_plan else None,
            "disruption_score": disruption.get("score", 0),
            "disruption_level": disruption.get("level", "low"),
            "tasks_count": len(tasks),
            "unscheduled_count": len(unscheduled),
            "persisted": persisted,
            "log_id": log_id,
            "output_preview": (result.get("output_content") or "")[:500],
            "message": disruption.get("message", "插单处理完成"),
        }

    except Exception as e:
        logger.exception("插单任务异常")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def _evaluate_disruption(
    original_plan: Optional[Dict],
    new_plan_id: Optional[str],
    new_tasks: List[Dict],
    order_id: str,
) -> Dict[str, Any]:
    """
    评估插单扰动程度

    评分依据：
        - 新计划中属于插单订单的任务工时占比（越高扰动越大）
        - 未排产任务数量
        - 是否存在超期
    """
    from src.services.db import pg_query

    # 新订单任务工时
    new_order_hours = sum(
        float(t.get("duration_hours") or 0)
        for t in new_tasks if t.get("order_id") == order_id
    )
    total_hours = sum(float(t.get("duration_hours") or 0) for t in new_tasks)

    # 旧计划工时
    old_hours = 0.0
    old_task_count = 0
    if original_plan:
        old_rows = pg_query(
            "SELECT COUNT(*) c, COALESCE(SUM(duration_hours),0) h FROM production_task WHERE plan_id=%s",
            (original_plan["plan_id"],),
        )
        if old_rows:
            old_hours = float(old_rows[0]["h"])
            old_task_count = old_rows[0]["c"]

    # 扰动评分（0-100）
    if total_hours == 0:
        score = 100
    else:
        ratio = new_order_hours / total_hours if total_hours else 0
        score = min(100, ratio * 80 + 20)

    if score < 30:
        level = "low"
        msg = "低扰动，新订单对既有计划影响较小"
    elif score < 60:
        level = "medium"
        msg = "中扰动，部分既有任务被调整，建议人工确认"
    else:
        level = "high"
        msg = "高扰动，既有计划受较大影响，建议评估是否接受"

    return {
        "score": round(score, 1),
        "level": level,
        "message": msg,
        "new_order_hours": round(new_order_hours, 1),
        "total_hours": round(total_hours, 1),
        "old_plan_hours": round(old_hours, 1),
        "old_task_count": old_task_count,
    }


def _find_affected_tasks(
    original_plan: Optional[Dict],
    new_plan_id: str,
    new_tasks: List[Dict],
) -> List[Dict[str, Any]]:
    """
    找出受插单影响的任务（时间被推迟或被取消）

    对比新旧计划中相同 parent_task_id 的任务，检测时间变化。
    """
    from src.services.db import pg_query

    affected = []
    if not original_plan:
        return affected

    old_tasks = pg_query(
        """SELECT parent_task_id, start_time, end_time
           FROM production_task WHERE plan_id=%s""",
        (original_plan["plan_id"],),
    )
    old_map = {r["parent_task_id"]: r for r in old_tasks}

    new_map = {t["task_id"]: t for t in new_tasks}

    for tid, old_t in old_map.items():
        new_t = new_map.get(tid)
        if new_t is None:
            # 任务在新计划中消失（被取消或合并）
            affected.append({
                "task_id": tid,
                "impact_type": "cancelled",
                "old_start_time": old_t["start_time"].isoformat() if old_t["start_time"] else None,
                "new_start_time": None,
                "delay_minutes": None,
            })
        else:
            old_start = datetime.fromisoformat(str(old_t["start_time"])) if old_t["start_time"] else None
            new_start_str = new_t.get("start_time")
            new_start = datetime.fromisoformat(new_start_str) if new_start_str else None
            if old_start and new_start and new_start > old_start:
                delay = int((new_start - old_start).total_seconds() / 60)
                if delay > 0:
                    affected.append({
                        "task_id": tid,
                        "impact_type": "delayed",
                        "old_start_time": old_start.isoformat(),
                        "new_start_time": new_start.isoformat(),
                        "delay_minutes": delay,
                    })

    return affected


# ============ 状态查询 ============

def get_task_status(task_id: str) -> Dict[str, Any]:
    """
    获取异步任务状态

    Args:
        task_id: Celery 任务ID

    Returns:
        {task_id, status, result, info}
    """
    result = celery_app.AsyncResult(task_id)
    return {
        "task_id": task_id,
        "status": result.status,
        "result": result.result if result.ready() else None,
        "info": result.info if result.status == "PROGRESS" else None,
    }