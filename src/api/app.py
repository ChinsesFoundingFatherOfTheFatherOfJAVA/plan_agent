"""
智能排产计划Agent - FastAPI 应用入口

端点总览：
    POST /api/scheduling/run         提交排产任务（异步 Celery）
    POST /api/scheduling/sync        同步排产（直接返回结果）
    GET  /api/scheduling/status/{id} 查询异步任务状态
    POST /api/orders/insert          插单
    POST /api/shortage/check         缺料检查
    POST /api/delivery/check         交付检查
    GET  /api/plans/latest           查最新计划
    GET  /api/plans/{plan_id}        查指定计划详情
    POST /api/plans/{plan_id}/confirm 确认计划
    GET  /api/tasks/active           查活跃任务
    GET  /api/health                 健康检查
    GET  /api/stats                  库内统计概览

启动方式：
    uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload
"""
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.services import plan_writer as writer
from src.services import repository as repo
from src.services.db import pg_query, pg_execute, pg_connection
from src.services.celery_app import get_task_status, run_scheduling, insert_order_task
from src.utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="智能排产计划Agent",
    description="基于 LangGraph + OR-Tools 的约束排产服务",
    version="1.5.0",
)


# ============ 请求/响应模型 ============

class SchedulingRequest(BaseModel):
    """排产请求"""
    user_input: str = Field(default="安排下周的生产计划", description="用户自然语言指令")
    request_type: Optional[str] = Field(default=None, description="直接指定类型，跳过 LLM 解析")
    sync: bool = Field(default=False, description="True 则同步执行直接返回结果")


class InsertOrderRequest(BaseModel):
    """插单请求"""
    order_id: str = Field(..., description="订单号")
    customer_id: str = Field(default="CUST-NEW", description="客户ID")
    required_date: str = Field(..., description="需求日期 YYYY-MM-DD")
    priority: int = Field(default=1, ge=1, le=3, description="优先级 1紧急 2正常 3低")
    items: List[Dict[str, Any]] = Field(..., description="订单明细列表")
    sync: bool = Field(default=False, description="True 则同步执行直接返回结果")


# ============ 排产 ============

@app.post("/api/scheduling/run", summary="提交异步排产任务")
async def submit_scheduling(req: SchedulingRequest):
    """
    提交排产任务到 Celery 异步执行，返回 task_id。
    sync=True 时同步执行直接返回结果（适合快速求解）。

    适用于长时间求解场景，通过 /api/scheduling/status/{task_id} 轮询结果。
    """
    if req.sync:
        return await sync_scheduling(req)
    task = run_scheduling.apply(args=[req.model_dump()])
    return {"task_id": task.id, "status": "PENDING", "message": "排产任务已提交"}


@app.post("/api/scheduling/sync", summary="同步排产")
async def sync_scheduling(req: SchedulingRequest):
    """
    同步执行排产，直接返回完整结果。

    适用于快速求解或调试场景。大规模排产可能超时，建议用异步接口。
    """
    from src.agent.graphs.plan_graph import build_plan_graph
    from src.models.state import create_initial_state
    from src.services.celery_app import _get_llm_or_fallback, _MockLLM

    try:
        initial_state = create_initial_state(req.user_input)
        if req.request_type:
            initial_state["request_type"] = req.request_type
            llm = _MockLLM(req.request_type)
        else:
            llm = _get_llm_or_fallback()
        graph = build_plan_graph(llm)

        result = graph.invoke(initial_state)

        tasks = result.get("production_tasks") or []
        schedule = result.get("schedule") or {}
        persist = result.get("persist_result") or {}

        return {
            "success": True,
            "plan_id": result.get("plan_id"),
            "request_type": result.get("request_type"),
            "strategy": result.get("strategy"),
            "tasks_count": len(tasks),
            "unscheduled_count": len(result.get("unscheduled_tasks") or []),
            "segments_count": schedule.get("segments_count", 0),
            "total_hours": schedule.get("total_hours", 0),
            "solver_status": schedule.get("solver_status"),
            "persisted": result.get("plan_persisted", False),
            "segment_rows": persist.get("segment_rows"),
            "alerts": result.get("alerts") or [],
            "recommendations": result.get("recommendations") or [],
            "output": result.get("output_content"),
            "run_id": result.get("run_id"),
        }
    except Exception as e:
        logger.exception("同步排产失败")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/scheduling/status/{task_id}", summary="查询异步任务状态")
async def scheduling_status(task_id: str):
    """查询 Celery 异步任务的状态与结果"""
    return get_task_status(task_id)


# ============ 插单 ============

@app.post("/api/orders/insert", summary="插单")
async def insert_order(req: InsertOrderRequest):
    """
    插单：创建/更新订单 -> 重排（保护既有计划）-> 扰动评分 -> 日志

    默认异步执行返回 task_id；sync=True 时同步执行直接返回结果。
    """
    if req.sync:
        result = insert_order_task.apply(args=[req.model_dump()]).get()
        return result
    task = insert_order_task.apply(args=[req.model_dump()])
    return {"task_id": task.id, "order_id": req.order_id, "status": "PENDING", "message": "插单任务已提交"}


# ============ 缺料 / 交付检查 ============

@app.post("/api/shortage/check", summary="缺料检查")
async def check_shortage(sync: bool = Query(default=False, description="True 则同步执行")):
    """缺料检查，默认异步，sync=True 同步返回"""
    from src.services.celery_app import check_shortage as _check
    if sync:
        return _check.apply().get()
    task = _check.apply()
    return {"task_id": task.id, "status": "PENDING", "message": "缺料检查已提交"}


@app.post("/api/delivery/check", summary="交付检查")
async def check_delivery(sync: bool = Query(default=False, description="True 则同步执行")):
    """交付进度检查，默认异步，sync=True 同步返回"""
    from src.services.celery_app import check_delivery_progress as _check
    if sync:
        return _check.apply().get()
    task = _check.apply()
    return {"task_id": task.id, "status": "PENDING", "message": "交付检查已提交"}


# ============ 计划查询 ============

@app.get("/api/plans/latest", summary="查最新计划")
async def get_latest_plan():
    """获取最新的排产计划及任务概要"""
    plan = repo.fetch_latest_plan()
    if not plan:
        raise HTTPException(status_code=404, detail="无排产计划")
    return _plan_with_tasks(plan["plan_id"])


@app.get("/api/plans/{plan_id}", summary="查指定计划详情")
async def get_plan(plan_id: str):
    """获取指定计划的完整信息，含所有任务段"""
    return _plan_with_tasks(plan_id)


@app.post("/api/plans/{plan_id}/confirm", summary="确认计划")
async def confirm_plan(plan_id: str):
    """
    把计划从 draft 置为 confirmed，任务状态从 planned 转为 scheduled。

    同一时间只允许一个 confirmed/executing 计划（uk_plan_active 约束）。
    """
    rows = pg_query("SELECT status FROM production_plan WHERE plan_id=%s", (plan_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"计划 {plan_id} 不存在")
    if rows[0]["status"] not in ("draft", "confirmed"):
        raise HTTPException(status_code=400, detail=f"计划状态为 {rows[0]['status']}，无法确认")
    try:
        n = writer.confirm_plan(plan_id)
        return {"confirmed": n, "plan_id": plan_id}
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"确认失败（可能已有其他生效计划）: {e}")


@app.delete("/api/plans/{plan_id}", summary="删除草稿计划")
async def delete_plan(plan_id: str):
    """删除草稿计划（仅 draft 状态可删），同时清理关联的任务和产能分配"""
    try:
        n = writer.delete_plan(plan_id)
        return {"deleted": n, "plan_id": plan_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _plan_with_tasks(plan_id: str) -> Dict[str, Any]:
    """组装计划头 + 任务段"""
    plans = pg_query(
        """SELECT plan_id, plan_name, plan_date, horizon_start, horizon_end,
                  status, created_by, created_at, updated_at
           FROM production_plan WHERE plan_id=%s""",
        (plan_id,),
    )
    if not plans:
        raise HTTPException(status_code=404, detail=f"计划 {plan_id} 不存在")
    plan = plans[0]

    tasks = pg_query(
        """SELECT task_id, parent_task_id, segment_index, segment_count,
                  order_id, item_id, product_id, center_id,
                  quantity, start_time, end_time, duration_hours,
                  status, priority, due_date, tardiness_hours,
                  setup_time_minutes
           FROM production_task WHERE plan_id=%s
           ORDER BY parent_task_id, segment_index""",
        (plan_id,),
    )

    # 按 parent_task_id 分组，展示逻辑任务
    logical: Dict[str, List[Dict]] = {}
    for t in tasks:
        pid = t["parent_task_id"]
        logical.setdefault(pid, []).append(_norm_row(t))

    return {
        "plan": _norm_row(plan),
        "total_segments": len(tasks),
        "logical_tasks": [
            {
                "task_id": pid,
                "segments": segs,
                "start_time": segs[0]["start_time"],
                "end_time": segs[-1]["end_time"],
                "total_hours": sum(s["duration_hours"] for s in segs),
            }
            for pid, segs in logical.items()
        ],
    }


# ============ 活跃任务查询 ============

@app.get("/api/tasks/active", summary="查活跃生产任务")
async def get_active_tasks(
    center_id: Optional[str] = Query(default=None, description="按工作中心过滤"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """查询库中未完成的生产任务"""
    where = ["status IN ('planned', 'scheduled', 'in_progress')"]
    params: List[Any] = []
    if center_id:
        where.append("center_id = %s")
        params.append(center_id)

    rows = pg_query(
        f"""SELECT task_id, parent_task_id, plan_id, order_id, product_id,
                   center_id, quantity, start_time, end_time, duration_hours,
                   status, priority, due_date
            FROM production_task
            WHERE {' AND '.join(where)}
            ORDER BY start_time
            LIMIT %s""",
        tuple(params + [limit]),
    )
    return {"tasks": [_norm_row(r) for r in rows], "count": len(rows)}


# ============ 统计 ============

@app.get("/api/stats", summary="库内统计概览")
async def get_stats():
    """库内各表行数与排产概况"""
    tables = [
        "sales_order", "sales_order_item", "material", "inventory",
        "bill_of_material", "work_center", "routing",
        "production_plan", "production_task", "capacity_calendar",
        "plan_run_log", "shortage_alert", "delivery_alert",
        "insert_order_log",
    ]
    counts = {}
    for t in tables:
        try:
            counts[t] = pg_query(f"SELECT COUNT(*) c FROM {t}")[0]["c"]
        except Exception:
            counts[t] = "N/A"

    # 计划状态分布
    plan_status = pg_query(
        "SELECT status, COUNT(*) c FROM production_plan GROUP BY status ORDER BY status"
    )
    # 活跃预警
    active_shortage = pg_query(
        "SELECT COUNT(*) c FROM shortage_alert WHERE status='active'"
    )[0]["c"]
    active_delivery = pg_query(
        "SELECT COUNT(*) c FROM delivery_alert WHERE status='active'"
    )[0]["c"]

    return {
        "table_counts": counts,
        "plan_status": {r["status"]: r["c"] for r in plan_status},
        "active_alerts": {"shortage": active_shortage, "delivery": active_delivery},
        "timestamp": datetime.now().isoformat(),
    }


# ============ RAG 知识库 ============

@app.get("/api/knowledge/search", summary="知识库检索")
async def knowledge_search(
    q: str = Query(..., description="查询文本"),
    top_k: int = Query(default=5, ge=1, le=20),
    doc_type: Optional[str] = Query(default=None, description="按类型过滤"),
):
    """
    检索排产知识库，返回相关文档。

    类型包括：scheduling_rule / material_rule / capacity_rule / due_date_rule / routing_rule
    """
    from src.services.rag_service import rag_service
    if not rag_service._initialized:
        await rag_service.initialize()
    results = await rag_service.search(q, top_k=top_k, filter_type=doc_type)
    return {
        "query": q,
        "results": [
            {"doc_id": r.doc_id, "content": r.content, "score": r.score,
             "type": r.metadata.get("type", ""), "source": r.metadata.get("source", "")}
            for r in results
        ],
        "count": len(results),
    }


@app.get("/api/knowledge/context", summary="获取RAG上下文")
async def knowledge_context(
    q: str = Query(..., description="查询文本"),
    top_k: int = Query(default=3, ge=1, le=10),
):
    """检索知识库并返回拼接的上下文文本，供 LLM 或规则引擎参考"""
    from src.services.rag_service import rag_service
    if not rag_service._initialized:
        await rag_service.initialize()
    ctx = await rag_service.get_context(q, top_k=top_k)
    return {"query": q, "context": ctx, "length": len(ctx)}


@app.post("/api/knowledge/import", summary="导入种子知识")
async def knowledge_import():
    """将排产领域种子知识导入 Milvus（幂等，重复导入会更新）"""
    from src.services.rag_service import rag_service, create_sample_knowledge
    if not rag_service._initialized:
        await rag_service.initialize()
    docs = create_sample_knowledge()
    ids = await rag_service.add_documents(docs)
    return {"imported": len(ids), "doc_ids": ids}


@app.get("/api/knowledge/stats", summary="知识库统计")
async def knowledge_stats():
    """知识库文档数量、后端模式、词汇表状态"""
    from src.services.rag_service import rag_service
    if not rag_service._initialized:
        await rag_service.initialize()
    return rag_service.get_stats()





# ============ 工作中心 ============

@app.get("/api/work-centers", summary="查工作中心列表")
async def get_work_centers():
    """获取所有工作中心，含中文名称、产能、效率、状态"""
    rows = pg_query(
        """SELECT center_id, center_name, capacity_per_day,
                  efficiency_rate, status
           FROM work_center ORDER BY center_id"""
    )
    return {"work_centers": [_norm_row(r) for r in rows], "count": len(rows)}


# ============ 任务时间调整 ============

class TaskTimeUpdate(BaseModel):
    """任务时间更新请求"""
    start_time: str = Field(..., description="新开始时间 ISO 格式")
    end_time: str = Field(..., description="新结束时间 ISO 格式")


@app.patch("/api/tasks/{task_id}", summary="更新任务段时间")
async def update_task_time(task_id: str, req: TaskTimeUpdate):
    """
    更新任务段的开始/结束时间。

    会自动重算 duration_hours，并重新汇总 capacity_allocation。
    受排他约束保护：如果新时间与同计划同中心的其他任务重叠，返回 409。

    注意：拖拽后前端应将时间对齐到工作时间内（08:00-23:12），
    后端不做时间合法性校验（只防重叠），前端负责约束。
    """
    from psycopg2 import errors as pg_errors

    # 查询当前任务信息
    rows = pg_query(
        """SELECT task_id, plan_id, center_id, segment_index, setup_time_minutes
           FROM production_task WHERE task_id = %s""",
        (task_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    task = rows[0]

    # 解析新时间
    try:
        from datetime import datetime as _dt
        new_start = _dt.fromisoformat(req.start_time.replace("Z", "+00:00"))
        new_end = _dt.fromisoformat(req.end_time.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式无效，需 ISO 格式")

    if new_end <= new_start:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")

    # 计算新工时
    delta_seconds = (new_end - new_start).total_seconds()
    new_duration = round(delta_seconds / 3600.0, 4)

    # 更新任务时间
    try:
        with pg_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE production_task
                       SET start_time = %s, end_time = %s, duration_hours = %s
                       WHERE task_id = %s""",
                    (new_start, new_end, new_duration, task_id),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="任务不存在")

                # 重算该计划的 capacity_allocation
                plan_id = task["plan_id"]
                # 获取该计划的所有任务段
                from psycopg2.extras import RealDictCursor as _RDC2
                with conn.cursor(cursor_factory=_RDC2) as dcur:
                    dcur.execute(
                        "SELECT task_id, center_id, start_time, end_time, "
                        "duration_hours, setup_time_minutes "
                        "FROM production_task WHERE plan_id = %s",
                        (plan_id,),
                    )
                    all_tasks = dcur.fetchall()
                # 删旧 allocation
                cur.execute(
                    "DELETE FROM capacity_allocation WHERE plan_id = %s",
                    (plan_id,),
                )
                # 重算 allocation
                from datetime import datetime, date as _date
                agg = {}
                for t in all_tasks:
                    cid = t["center_id"]
                    setup_h = float(t.get("setup_time_minutes") or 0) / 60.0
                    d = t["start_time"].date() if hasattr(t["start_time"], "date") else _date.fromisoformat(str(t["start_time"])[:10])
                    # 换型时间按任务只计一次，累加到该中心该日的 setup_hours
                    ph, sh = agg.get((cid, d), (0.0, 0.0))
                    agg[(cid, d)] = (ph + float(t["duration_hours"]), sh + setup_h)

                from psycopg2.extras import execute_values
                if agg:
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
                    # 重算 capacity_calendar
                    centers = sorted({c for c, _ in agg})
                    dates = sorted({d for _, d in agg})
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
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        err_str = str(e)
        if "ExclusionViolation" in type(e).__name__ or "exclusion" in err_str.lower():
            raise HTTPException(
                status_code=409,
                detail=f"时间重叠：新时间与同中心已有任务冲突",
            )
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {err_str[:200]}")

    return {
        "task_id": task_id,
        "start_time": new_start.isoformat(),
        "end_time": new_end.isoformat(),
        "duration_hours": new_duration,
        "message": "任务时间已更新，产能已重算",
    }


# ============ 产能负荷 ============

@app.get("/api/capacity/load", summary="产能负荷数据")
async def capacity_load(
    center_id: Optional[str] = Query(default=None, description="按工作中心过滤"),
    start_date: Optional[str] = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="结束日期 YYYY-MM-DD"),
):
    """获取产能日历数据，含可用工时、已排工时、负荷率"""
    where = ["1=1"]
    params: List[Any] = []
    if center_id:
        where.append("center_id = %s")
        params.append(center_id)
    if start_date:
        where.append("work_date >= %s")
        params.append(start_date)
    if end_date:
        where.append("work_date <= %s")
        params.append(end_date)

    rows = pg_query(
        f"""SELECT center_id, work_date, available_hours, planned_hours, actual_hours
            FROM capacity_calendar
            WHERE {' AND '.join(where)}
            ORDER BY center_id, work_date""",
        tuple(params),
    )
    items = []
    for r in rows:
        avail = float(r["available_hours"])
        planned = float(r["planned_hours"])
        load_rate = round(planned / avail * 100, 1) if avail > 0 else 0
        items.append({
            "center_id": r["center_id"],
            "work_date": r["work_date"].isoformat() if r["work_date"] else None,
            "available_hours": avail,
            "planned_hours": planned,
            "actual_hours": float(r.get("actual_hours") or 0),
            "load_rate": load_rate,
        })
    return {"items": items, "count": len(items)}


# ============ 预警查询 ============

@app.get("/api/alerts/shortage", summary="缺料预警列表")
async def shortage_alerts(
    status: Optional[str] = Query(default=None, description="按状态过滤: active/resolved"),
):
    """查询缺料预警"""
    where = ["1=1"]
    params: List[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    rows = pg_query(
        f"""SELECT alert_id, material_id, required_date, required_quantity,
                  available_quantity, shortage_quantity, risk_level, status, created_at
           FROM shortage_alert
           WHERE {' AND '.join(where)}
           ORDER BY created_at DESC""",
        tuple(params),
    )
    return {"items": [_norm_row(r) for r in rows], "count": len(rows)}


@app.get("/api/alerts/delivery", summary="交付预警列表")
async def delivery_alerts(
    status: Optional[str] = Query(default=None, description="按状态过滤: active/resolved"),
):
    """查询交付延误预警"""
    where = ["1=1"]
    params: List[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    rows = pg_query(
        f"""SELECT alert_id, order_id, item_id, expected_date, required_date,
                  delay_days, alert_type, status, created_at
           FROM delivery_alert
           WHERE {' AND '.join(where)}
           ORDER BY created_at DESC""",
        tuple(params),
    )
    return {"items": [_norm_row(r) for r in rows], "count": len(rows)}


# ============ 插单历史 ============

@app.get("/api/insert-orders", summary="插单历史记录")
async def insert_order_logs():
    """查询插单扰动记录及受影响任务"""
    logs = pg_query(
        """SELECT log_id, order_id, original_plan_id, new_plan_id,
                  disruption_score, disruption_level, status, created_at
           FROM insert_order_log
           ORDER BY created_at DESC"""
    )
    result = []
    for log in logs:
        affected = pg_query(
            """SELECT task_id, impact_type, old_start_time, new_start_time, delay_minutes
               FROM insert_order_affected_task
               WHERE log_id = %s ORDER BY delay_minutes DESC""",
            (log["log_id"],),
        )
        result.append({
            **_norm_row(log),
            "affected_tasks": [_norm_row(a) for a in affected],
        })
    return {"items": result, "count": len(result)}


# ============ 计划列表 ============

@app.get("/api/plans", summary="计划列表")
async def list_plans(
    status: Optional[str] = Query(default=None, description="按状态过滤"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """获取排产计划列表"""
    where = ["1=1"]
    params: List[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    rows = pg_query(
        f"""SELECT plan_id, plan_name, plan_date, horizon_start, horizon_end,
                  status, created_by, created_at, updated_at
           FROM production_plan
           WHERE {' AND '.join(where)}
           ORDER BY created_at DESC
           LIMIT %s""",
        tuple(params + [limit]),
    )
    return {"items": [_norm_row(r) for r in rows], "count": len(rows)}


# ============ 物料库存 ============

@app.get("/api/materials", summary="物料库存概览")
async def materials_overview():
    """获取物料主数据及库存信息"""
    rows = pg_query(
        """SELECT m.material_id, m.material_name, m.material_type, m.unit,
                  m.safety_stock, m.lead_time_days,
                  COALESCE(i.quantity_on_hand, 0) AS quantity_on_hand,
                  COALESCE(i.quantity_reserved, 0) AS quantity_reserved,
                  COALESCE(i.quantity_on_order, 0) AS quantity_on_order
           FROM material m
           LEFT JOIN inventory i ON m.material_id = i.material_id
           ORDER BY m.material_id"""
    )
    items = []
    for r in rows:
        on_hand = float(r["quantity_on_hand"])
        reserved = float(r["quantity_reserved"])
        safety = float(r["safety_stock"])
        available = on_hand - reserved
        below_safety = available < safety
        items.append({
            **_norm_row(r),
            "available_quantity": available,
            "below_safety": below_safety,
        })
    return {"items": items, "count": len(items)}


# ============ 审计日志 ============

@app.get("/api/run-logs", summary="排产审计日志")
async def run_logs(limit: int = Query(default=20, ge=1, le=100)):
    """查询排产运行审计日志"""
    rows = pg_query(
        """SELECT run_id, plan_id, request_type, user_input, strategy,
                  solver_status, tasks_total, tasks_scheduled, tasks_unscheduled,
                  tardy_tasks, solve_seconds, validation_passed,
                  conflicts_count, retry_count, created_at
           FROM plan_run_log
           ORDER BY created_at DESC
           LIMIT %s""",
        (limit,),
    )
    return {"items": [_norm_row(r) for r in rows], "count": len(rows)}



# ============ 钉钉机器人 ============

@app.post("/api/dingtalk/start", summary="启动钉钉机器人")
async def start_dingtalk():
    """启动钉钉 Stream 机器人（异步后台运行）"""
    import threading
    from src.services.dingtalk_bot import start_dingtalk_bot, is_running

    if is_running():
        return {"status": "already_running", "message": "钉钉机器人已在运行"}

    def _run():
        start_dingtalk_bot()

    t = threading.Thread(target=_run, daemon=True, name="dingtalk-bot")
    t.start()

    # 等待连接建立
    import time
    time.sleep(3)

    from src.services.dingtalk_bot import is_running as _check
    if _check():
        return {"status": "started", "message": "钉钉机器人已启动，在钉钉中@机器人发送消息"}
    else:
        return {"status": "failed", "message": "启动失败，请检查配置和日志"}


@app.get("/api/dingtalk/status", summary="钉钉机器人状态")
async def dingtalk_status():
    """查询钉钉机器人运行状态"""
    from src.services.dingtalk_bot import is_running
    from config.settings import settings

    return {
        "running": is_running(),
        "client_id": settings.dingtalk_client_id[:10] + "..." if settings.dingtalk_client_id else None,
        "app_id": settings.dingtalk_app_id,
    }


@app.post("/api/dingtalk/stop", summary="停止钉钉机器人")
async def stop_dingtalk():
    """停止钉钉机器人"""
    from src.services.dingtalk_bot import stop_dingtalk_bot
    stop_dingtalk_bot()
    return {"status": "stopped", "message": "钉钉机器人已停止"}

# ============ 健康检查 ============

@app.get("/api/health", summary="健康检查")
async def health():
    """检查 PG / Redis 连通性"""
    checks = {}
    # PG
    try:
        pg_query("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"
    # Redis
    try:
        from src.services.db import get_redis
        r = get_redis()
        r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "healthy" if all_ok else "degraded", "checks": checks}


# ============ 工具 ============

def _norm_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 PG 原生类型（Decimal/date/datetime）转为 JSON 可序列化类型"""
    from decimal import Decimal
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = float(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif hasattr(v, "isoformat"):  # date
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


# ============ 前端静态文件 ============

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 托管甘特图前端页面
_static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/", summary="首页")
async def index():
    """导航首页"""
    index_path = os.path.join(_static_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type='text/html; charset=utf-8')
    return {"message": "API running", "docs": "/docs", "gantt": "/gantt"}

@app.get("/gantt", summary="甘特图可视化")
async def gantt_page():
    """排产计划甘特图页面"""
    index_path = os.path.join(_static_dir, "gantt.html")
    if not os.path.isfile(index_path):
        raise HTTPException(status_code=404, detail="前端页面未部署")
    return FileResponse(index_path, media_type='text/html; charset=utf-8')

@app.get("/capacity", summary="产能负荷页面")
async def capacity_page():
    return FileResponse(os.path.join(_static_dir, "capacity.html"), media_type='text/html; charset=utf-8')

@app.get("/alerts", summary="预警中心页面")
async def alerts_page():
    return FileResponse(os.path.join(_static_dir, "alerts.html"), media_type='text/html; charset=utf-8')

@app.get("/insert-orders", summary="插单管理页面")
async def insert_orders_page():
    return FileResponse(os.path.join(_static_dir, "insert-orders.html"), media_type='text/html; charset=utf-8')

@app.get("/materials", summary="物料库存页面")
async def materials_page():
    return FileResponse(os.path.join(_static_dir, "materials.html"), media_type='text/html; charset=utf-8')

@app.get("/run-logs", summary="审计日志页面")
async def run_logs_page():
    return FileResponse(os.path.join(_static_dir, "run-logs.html"), media_type='text/html; charset=utf-8')

