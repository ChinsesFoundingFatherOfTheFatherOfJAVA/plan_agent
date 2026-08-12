"""
数据访问层（Repository）

职责：封装所有 PostgreSQL 查询，并把 PG 原生类型转换为业务层期望的类型。

类型转换约定（关键）：
    Decimal        -> float      （OR-Tools 与 JSON 序列化均不接受 Decimal）
    date/datetime  -> ISO 字符串 （与现有节点、LLM 输出格式保持一致）

这样上层节点（data_fetch / analyze / plan）无需改动数据结构即可从模拟数据切换到真实库。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.services.db import pg_query


# ============ 类型规整 ============

def _norm(value: Any) -> Any:
    """把单个 PG 值转换为业务层类型"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _norm_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """规整一行"""
    return {k: _norm(v) for k, v in row.items()}


def _norm_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """规整多行"""
    return [_norm_row(r) for r in rows]


# ============ 订单 ============

def fetch_orders(
    statuses: Optional[List[str]] = None,
    order_ids: Optional[List[str]] = None,
    due_before: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    查询订单（含明细，嵌套为 items 列表）

    Args:
        statuses: 订单状态过滤，默认 ['confirmed', 'in_progress']
        order_ids: 指定订单ID列表（插单场景用）
        due_before: 只取交期早于该日期的订单（YYYY-MM-DD）
        limit: 最大订单数

    Returns:
        [{order_id, customer_id, order_date, required_date, priority, status,
          total_amount, items: [{item_id, product_id, quantity, unit_price, due_date}]}]
    """
    if statuses is None:
        statuses = ["confirmed", "in_progress"]

    where = ["o.status = ANY(%s)"]
    params: List[Any] = [statuses]

    if order_ids:
        where.append("o.order_id = ANY(%s)")
        params.append(order_ids)
    if due_before:
        where.append("o.required_date <= %s")
        params.append(due_before)

    sql = f"""
        SELECT o.order_id, o.customer_id, o.order_date, o.required_date,
               o.priority, o.status, o.total_amount
        FROM sales_order o
        WHERE {' AND '.join(where)}
        ORDER BY o.priority ASC, o.required_date ASC
        LIMIT %s
    """
    params.append(limit)
    orders = _norm_rows(pg_query(sql, tuple(params)))

    if not orders:
        return []

    ids = [o["order_id"] for o in orders]
    items = _norm_rows(pg_query(
        """SELECT item_id, order_id, product_id, quantity, unit_price, due_date
           FROM sales_order_item
           WHERE order_id = ANY(%s)
           ORDER BY item_id""",
        (ids,),
    ))

    grouped: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
    for it in items:
        grouped[it["order_id"]].append(it)

    for o in orders:
        o["items"] = grouped.get(o["order_id"], [])

    return orders


# ============ 物料 / 库存 / BOM ============

def fetch_materials(material_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """查询物料主数据"""
    if material_ids:
        return _norm_rows(pg_query(
            """SELECT material_id, material_name, material_type, unit,
                      safety_stock, lead_time_days, supplier_id
               FROM material WHERE material_id = ANY(%s) ORDER BY material_id""",
            (material_ids,),
        ))
    return _norm_rows(pg_query(
        """SELECT material_id, material_name, material_type, unit,
                  safety_stock, lead_time_days, supplier_id
           FROM material ORDER BY material_id"""
    ))


def fetch_inventory(material_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    查询库存（按物料聚合，跨仓库汇总）

    多仓库场景下同一物料有多行，此处汇总为单行，避免上层重复计算。
    """
    where = ""
    params: tuple = ()
    if material_ids:
        where = "WHERE material_id = ANY(%s)"
        params = (material_ids,)

    return _norm_rows(pg_query(
        f"""SELECT material_id,
                   SUM(quantity_on_hand)  AS quantity_on_hand,
                   SUM(quantity_reserved) AS quantity_reserved,
                   SUM(quantity_on_order) AS quantity_on_order,
                   MAX(last_updated)      AS last_updated
            FROM inventory {where}
            GROUP BY material_id
            ORDER BY material_id""",
        params,
    ))


def fetch_boms(product_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    查询 BOM（含物料名称与提前期，避免上层再次 join）
    """
    where = ""
    params: tuple = ()
    if product_ids:
        where = "WHERE b.product_id = ANY(%s)"
        params = (product_ids,)

    return _norm_rows(pg_query(
        f"""SELECT b.bom_id, b.product_id, b.material_id, b.quantity_required,
                   m.material_name, m.unit, m.lead_time_days, m.safety_stock
            FROM bill_of_material b
            JOIN material m USING (material_id)
            {where}
            ORDER BY b.product_id, b.material_id""",
        params,
    ))


def fetch_bom_map(product_ids: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    返回 {product_id: [{material_id, quantity_required, material_name, ...}]}
    便于缺料计算直接按产品取用。
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    for row in fetch_boms(product_ids):
        result.setdefault(row["product_id"], []).append(row)
    return result


# ============ 工艺路线 ============

def fetch_routing(product_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    查询工艺路线

    返回每个产品在哪些工作中心可加工、每件标准工时多少。
    求解器据此做差异化工作中心分配，替代全局 0.5h/件 假设。

    Args:
        product_ids: 指定产品，None 则查全部 active 路线

    Returns:
        [{routing_id, product_id, center_id, hours_per_unit, setup_time_minutes, status}]
    """
    where = ["status = 'active'"]
    params: List[Any] = []
    if product_ids:
        where.append("product_id = ANY(%s)")
        params.append(product_ids)
    return _norm_rows(pg_query(
        f"""SELECT routing_id, product_id, center_id, hours_per_unit,
                   setup_time_minutes, status
            FROM routing
            WHERE {' AND '.join(where)}
            ORDER BY product_id, center_id""",
        tuple(params),
    ))


def fetch_routing_map(product_ids: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    查询工艺路线并按 product_id 分组

    Returns:
        {product_id: [{center_id, hours_per_unit, setup_time_minutes}, ...]}
    """
    rows = fetch_routing(product_ids)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["product_id"], []).append({
            "center_id": r["center_id"],
            "hours_per_unit": r["hours_per_unit"],
            "setup_time_minutes": r["setup_time_minutes"],
        })
    return grouped


# ============ 产能 ============

def fetch_work_centers(only_active: bool = True) -> List[Dict[str, Any]]:
    """查询工作中心"""
    where = "WHERE status = 'active'" if only_active else ""
    return _norm_rows(pg_query(
        f"""SELECT center_id, center_name, capacity_per_day, efficiency_rate, status
            FROM work_center {where} ORDER BY center_id"""
    ))


def fetch_capacity_calendar(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    center_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    查询产能日历

    Args:
        start_date: 起始日期，默认今天
        end_date: 结束日期，默认起始 +30 天
    """
    where = ["work_date >= COALESCE(%s::date, CURRENT_DATE)"]
    params: List[Any] = [start_date]

    where.append("work_date <= COALESCE(%s::date, COALESCE(%s::date, CURRENT_DATE) + 30)")
    params.extend([end_date, start_date])

    if center_ids:
        where.append("center_id = ANY(%s)")
        params.append(center_ids)

    return _norm_rows(pg_query(
        f"""SELECT calendar_id, center_id, work_date,
                   available_hours, planned_hours, actual_hours,
                   (available_hours - planned_hours) AS remaining_hours
            FROM capacity_calendar
            WHERE {' AND '.join(where)}
            ORDER BY center_id, work_date""",
        tuple(params),
    ))


# ============ 已有计划 / 任务 ============

def fetch_active_tasks(
    center_ids: Optional[List[str]] = None,
    start_from: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    查询未完成的生产任务（插单重排时需要知道现有占用）
    """
    where = ["t.status IN ('planned','scheduled','in_progress')"]
    params: List[Any] = []

    if center_ids:
        where.append("t.center_id = ANY(%s)")
        params.append(center_ids)
    if start_from:
        where.append("t.end_time >= %s")
        params.append(start_from)

    return _norm_rows(pg_query(
        f"""SELECT t.task_id, t.plan_id, t.plan_version, t.order_id, t.item_id,
                   t.product_id, t.center_id, t.quantity,
                   t.start_time, t.end_time, t.status, t.priority
            FROM production_task t
            WHERE {' AND '.join(where)}
            ORDER BY t.center_id, t.start_time""",
        tuple(params),
    ))


def fetch_latest_plan() -> Optional[Dict[str, Any]]:
    """查询最新的排产计划"""
    rows = _norm_rows(pg_query(
        """SELECT plan_id, plan_name, plan_date, horizon_start, horizon_end,
                  status, created_by, created_at
           FROM production_plan
           ORDER BY created_at DESC LIMIT 1"""
    ))
    return rows[0] if rows else None
