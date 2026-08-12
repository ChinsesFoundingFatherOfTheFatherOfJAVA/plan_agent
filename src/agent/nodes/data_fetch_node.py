"""
智能排产计划Agent - Data Fetch节点

从 PostgreSQL 读取排产所需的全部数据。

数据来源现状：
    订单 / 物料 / 库存 / BOM / 工艺路线 / 工作中心 / 产能日历 -> PostgreSQL（已对接）
    未来若接入 ERP / WMS / MES，在此层替换为 MCP Tools 调用即可，
    上层节点依赖的数据结构保持不变。

工时来源（v1.4 更新）：
    每件产品的标准工时来自 routing 表（工艺路线），按产品 × 工作中心区分。
    例如 PROD-001 在 WC-001 为 0.1h/件，在 WC-002 为 0.2h/件。
    data_fetch 在订单明细上预置 item["duration_hours"]，取该产品所有可用
    路线中最快的工时（min hours_per_unit × quantity），供 analyze 节点做
    交付风险初判。plan 节点的求解器会按实际分配的工作中心重新计算精确工时。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.models.state import PlanAgentState, RequestType
from src.services import repository as repo
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 缺少工艺路线数据时的兜底工时（小时/件）
# 仅在 routing 表查不到对应产品时使用，并会打 warning 日志
# TODO: 生产环境应视为数据异常并阻断排产，不应静默兜底
FALLBACK_HOURS_PER_UNIT = 0.5


def _planning_window(request_params: Dict[str, Any]) -> tuple:
    """
    解析排产时间窗口

    优先取 LLM 解析出的 date_range，否则默认从今天起 30 天。
    """
    date_range = (request_params or {}).get("date_range") or {}
    start = date_range.get("start")
    end = date_range.get("end")

    if not start:
        start = datetime.now().date().isoformat()
    if not end:
        end = (datetime.fromisoformat(start).date() + timedelta(days=30)).isoformat()

    return start, end


def _attach_duration_hours(
    orders: List[Dict[str, Any]],
    routing_map: Dict[str, List[Dict[str, Any]]],
) -> None:
    """
    为每个订单明细预置 duration_hours（取最快路线的工时）。

    取 min(hours_per_unit) 的原因：analyze 节点用它做交付风险初判，
    应给「最快能做完」的乐观估计 -- 如果连最快的都排不下，那确实有风险。
    plan 节点的求解器会按实际分配的工作中心重新计算精确工时。
    """
    for o in orders:
        for it in o.get("items", []):
            pid = it["product_id"]
            routes = routing_map.get(pid)
            if routes:
                best_rate = min(r["hours_per_unit"] for r in routes)
                it["duration_hours"] = round(best_rate * float(it["quantity"]), 4)
            else:
                it["duration_hours"] = round(FALLBACK_HOURS_PER_UNIT * float(it["quantity"]), 4)
                logger.warning(
                    f"产品 {pid} 无工艺路线数据，使用兜底工时 {FALLBACK_HOURS_PER_UNIT}h/件"
                )


def data_fetch_node(state: PlanAgentState) -> Dict[str, Any]:
    """
    Data Fetch节点主函数

    根据请求类型按需读取数据，避免无用查询。

    Args:
        state: 当前状态

    Returns:
        更新后的状态字典
    """
    request_type = state.get("request_type")
    request_params = state.get("request_params") or {}
    start_date, end_date = _planning_window(request_params)

    results: Dict[str, Any] = {}
    errors: List[str] = []

    try:
        need_orders = request_type in (
            RequestType.NEW_PLAN.value,
            RequestType.INSERT_ORDER.value,
            RequestType.CHECK_SHORTAGE.value,
            RequestType.CHECK_DELIVERY.value,
        )
        need_capacity = request_type in (
            RequestType.NEW_PLAN.value,
            RequestType.INSERT_ORDER.value,
            RequestType.REBALANCE.value,
        )
        need_material = request_type in (
            RequestType.NEW_PLAN.value,
            RequestType.INSERT_ORDER.value,
            RequestType.CHECK_SHORTAGE.value,
        )

        # ---- 订单 ----
        if need_orders:
            order_ids = request_params.get("order_ids") or None
            orders = repo.fetch_orders(order_ids=order_ids)
            results["orders"] = orders

            # 查工艺路线，预置工时
            product_ids = sorted({
                it["product_id"]
                for o in orders
                for it in o.get("items", [])
            }) or None
            routing_map = repo.fetch_routing_map(product_ids)
            results["routing"] = routing_map
            _attach_duration_hours(orders, routing_map)

        # ---- 物料 / 库存 / BOM ----
        if need_material:
            product_ids = sorted({
                it["product_id"]
                for o in results.get("orders", [])
                for it in o["items"]
            }) or None

            bom_map = repo.fetch_bom_map(product_ids)
            results["boms"] = bom_map

            # 只查 BOM 里真正用到的物料，减少无关数据
            material_ids = sorted({
                r["material_id"] for rows in bom_map.values() for r in rows
            }) or None

            results["materials"] = repo.fetch_materials(material_ids)
            results["inventory"] = repo.fetch_inventory(material_ids)

        # ---- 产能 ----
        if need_capacity:
            work_centers = repo.fetch_work_centers(only_active=True)
            results["work_centers"] = work_centers
            results["capacity_calendar"] = repo.fetch_capacity_calendar(
                start_date=start_date,
                end_date=end_date,
                center_ids=[w["center_id"] for w in work_centers] or None,
            )

            # 插单 / 重平衡需要知道现有任务占用
            if request_type in (RequestType.INSERT_ORDER.value, RequestType.REBALANCE.value):
                results["existing_tasks"] = repo.fetch_active_tasks(
                    center_ids=[w["center_id"] for w in work_centers] or None,
                    start_from=start_date,
                )

        results["planning_window"] = {"start": start_date, "end": end_date}

    except Exception as e:
        # 数据获取失败不静默吞掉，写入 error 供下游判断
        errors.append(f"{type(e).__name__}: {e}")
        results["error"] = "数据获取失败: " + "; ".join(errors)

    # ---- 执行日志 ----
    execution_log = state.get("execution_log") or []
    log_data = {
        "fetched": [k for k in results if k not in ("error", "planning_window")],
        "orders_count": len(results.get("orders", [])),
        "work_centers_count": len(results.get("work_centers", [])),
        "calendar_rows": len(results.get("capacity_calendar", [])),
        "existing_tasks": len(results.get("existing_tasks", [])),
        "routing_products": len(results.get("routing") or {}),
        "window": results.get("planning_window"),
        "errors": errors,
        "timestamp": datetime.now().isoformat(),
    }
    execution_log.append({
        "node": "data_fetch",
        "action": "fetch_from_postgres",
        "result": log_data,
    })
    results["execution_log"] = execution_log

    return results