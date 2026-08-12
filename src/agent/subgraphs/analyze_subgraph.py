"""
智能排产计划Agent - Analyze子图

分析物料缺口、产能缺口、交付风险，并判断是否可走快速路径。

与早期版本的差异：
    - 物料需求改为按真实 BOM（state["boms"]）计算，不再硬编码「每件2个MAT-001」
    - 库存可用量考虑在途数量与采购提前期
    - 产能缺口按日历逐日累计比对，不再用「平均分配」粗略估算
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List

from src.models.state import PlanAgentState
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_date(value: Any):
    """把 ISO 字符串 / date / datetime 统一为 date"""
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    if isinstance(value, datetime):
        return value.date()
    return value


def analyze_shortage(state: PlanAgentState) -> Dict[str, Any]:
    """
    分析物料缺口

    计算逻辑：
        1. 按 BOM 展开每个订单明细的物料需求，按（物料, 交期）汇总
        2. 可用量 = 在库 - 已预留 - 安全库存
        3. 在途量仅在采购提前期能覆盖交期时才计入
        4. 按缺口占需求的比例定风险等级
    """
    orders = state.get("orders") or []
    materials = state.get("materials") or []
    inventory = state.get("inventory") or []
    boms = state.get("boms") or {}

    if not orders or not boms:
        return {"shortages": []}

    mat_map = {m["material_id"]: m for m in materials}
    inv_map = {i["material_id"]: i for i in inventory}

    # 1. 按 BOM 展开需求，按 (material_id, due_date) 汇总
    demand: Dict[tuple, Dict[str, Any]] = {}
    for order in orders:
        for item in order.get("items", []):
            due = _parse_date(item.get("due_date")) or _parse_date(order.get("required_date"))
            if due is None:
                continue

            for bom in boms.get(item["product_id"], []):
                key = (bom["material_id"], due)
                if key not in demand:
                    demand[key] = {"quantity": 0.0, "orders": set()}
                demand[key]["quantity"] += item["quantity"] * bom["quantity_required"]
                demand[key]["orders"].add(order["order_id"])

    # 2. 逐（物料, 交期）判断缺口
    today = datetime.now().date()
    shortages: List[Dict[str, Any]] = []

    for (material_id, due), info in sorted(demand.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        mat = mat_map.get(material_id, {})
        inv = inv_map.get(material_id, {})

        on_hand = inv.get("quantity_on_hand", 0.0)
        reserved = inv.get("quantity_reserved", 0.0)
        on_order = inv.get("quantity_on_order", 0.0)
        safety = mat.get("safety_stock", 0.0)
        lead_days = mat.get("lead_time_days", 0)

        days_until_due = (due - today).days

        # 在途物料只有能在交期前到货才算可用
        usable_on_order = on_order if lead_days <= days_until_due else 0.0

        # 安全库存不参与生产消耗
        available = on_hand - reserved - safety + usable_on_order
        required = info["quantity"]
        gap = required - available

        if gap <= 0:
            continue

        ratio = gap / required if required else 0
        if ratio >= 0.5 or days_until_due < lead_days:
            risk = "critical"
        elif ratio >= 0.2:
            risk = "high"
        else:
            risk = "medium"

        shortages.append({
            "material_id": material_id,
            "material_name": mat.get("material_name", material_id),
            "unit": mat.get("unit", ""),
            "required_quantity": round(required, 2),
            "available_quantity": round(available, 2),
            "shortage_quantity": round(gap, 2),
            "required_date": due.isoformat(),
            "risk_level": risk,
            "lead_time_days": lead_days,
            "days_until_due": days_until_due,
            "purchase_deadline": (due - timedelta(days=lead_days)).isoformat(),
            "source_orders": sorted(info["orders"]),
            "detail": {
                "on_hand": on_hand,
                "reserved": reserved,
                "safety_stock": safety,
                "on_order": on_order,
                "usable_on_order": usable_on_order,
            },
        })

    return {"shortages": shortages}


def analyze_capacity(state: PlanAgentState) -> Dict[str, Any]:
    """
    分析产能缺口

    按工作中心汇总排产窗口内的可用工时，与订单总需求工时比对。
    """
    orders = state.get("orders") or []
    work_centers = state.get("work_centers") or []
    calendar = state.get("capacity_calendar") or []
    existing_tasks = state.get("existing_tasks") or []

    if not work_centers:
        return {"capacity_gaps": [{
            "center_id": None,
            "reason": "没有可用的工作中心（全部 inactive 或 maintenance）",
            "risk_level": "critical",
        }]}

    # 总需求工时
    required_hours = 0.0
    for order in orders:
        for item in order.get("items", []):
            required_hours += item.get("duration_hours") or 0.0

    # 已被现有任务占用的工时
    occupied: Dict[str, float] = {}
    for t in existing_tasks:
        start = datetime.fromisoformat(t["start_time"])
        end = datetime.fromisoformat(t["end_time"])
        occupied[t["center_id"]] = occupied.get(t["center_id"], 0.0) + (end - start).total_seconds() / 3600

    # 各中心可用工时
    per_center: Dict[str, float] = {}
    for row in calendar:
        cid = row["center_id"]
        per_center[cid] = per_center.get(cid, 0.0) + (row.get("available_hours") or 0.0)

    total_available = sum(per_center.values()) - sum(occupied.values())

    capacity_gaps: List[Dict[str, Any]] = []

    # 整体产能是否够
    if required_hours > total_available:
        capacity_gaps.append({
            "center_id": "ALL",
            "center_name": "全部产线合计",
            "required_hours": round(required_hours, 2),
            "available_hours": round(total_available, 2),
            "gap_hours": round(required_hours - total_available, 2),
            "utilization_rate": round(required_hours / total_available, 3) if total_available > 0 else None,
            "risk_level": "critical",
            "reason": "排产窗口内总产能不足",
        })

    # 单中心利用率预警（超过 85% 视为瓶颈）
    if per_center:
        avg_need = required_hours / len(per_center)
        for cid, avail in sorted(per_center.items()):
            net = avail - occupied.get(cid, 0.0)
            if net <= 0:
                capacity_gaps.append({
                    "center_id": cid,
                    "center_name": next((w["center_name"] for w in work_centers if w["center_id"] == cid), cid),
                    "required_hours": round(avg_need, 2),
                    "available_hours": round(net, 2),
                    "gap_hours": round(avg_need, 2),
                    "utilization_rate": None,
                    "risk_level": "high",
                    "reason": "该产线窗口内无剩余可用工时",
                })
                continue

            util = avg_need / net
            if util > 0.85:
                capacity_gaps.append({
                    "center_id": cid,
                    "center_name": next((w["center_name"] for w in work_centers if w["center_id"] == cid), cid),
                    "required_hours": round(avg_need, 2),
                    "available_hours": round(net, 2),
                    "gap_hours": round(max(0.0, avg_need - net), 2),
                    "utilization_rate": round(util, 3),
                    "risk_level": "high" if util > 1 else "medium",
                    "reason": "产线负荷偏高，可能成为瓶颈",
                })

    return {"capacity_gaps": capacity_gaps}


def analyze_delivery_risk(state: PlanAgentState) -> Dict[str, Any]:
    """
    分析交付风险

    用「剩余可用工时 vs 所需工时」判断交期是否来得及，
    比早期「交期少于3天就报警」更贴近实际。
    """
    orders = state.get("orders") or []
    work_centers = state.get("work_centers") or []
    calendar = state.get("capacity_calendar") or []

    if not orders:
        return {"delivery_risks": []}

    # 按日期累计全厂可用工时
    daily: Dict[Any, float] = {}
    for row in calendar:
        d = _parse_date(row["work_date"])
        daily[d] = daily.get(d, 0.0) + (row.get("available_hours") or 0.0)

    today = datetime.now().date()
    delivery_risks: List[Dict[str, Any]] = []

    for order in orders:
        for item in order.get("items", []):
            due = _parse_date(item.get("due_date")) or _parse_date(order.get("required_date"))
            if due is None:
                delivery_risks.append({
                    "order_id": order["order_id"],
                    "item_id": item["item_id"],
                    "product_id": item["product_id"],
                    "due_date": None,
                    "risk_level": "critical",
                    "reason": "订单与明细均无交期，无法排产",
                })
                continue

            need = item.get("duration_hours") or 0.0

            # 交期前的累计可用工时
            capacity_before_due = sum(h for d, h in daily.items() if today <= d <= due)
            days_remaining = (due - today).days

            if days_remaining < 0:
                risk, reason = "critical", f"交期已过期 {abs(days_remaining)} 天"
            elif capacity_before_due <= 0:
                risk, reason = "critical", "交期前无任何可用产能（含节假日）"
            elif need > capacity_before_due:
                risk = "critical"
                reason = f"所需 {need:.1f}h 超过交期前可用产能 {capacity_before_due:.1f}h"
            elif need > capacity_before_due * 0.8:
                risk = "high"
                reason = f"所需 {need:.1f}h 占交期前产能 {capacity_before_due:.1f}h 的 80% 以上"
            elif days_remaining <= 2:
                risk, reason = "medium", f"交期仅剩 {days_remaining} 天，缓冲不足"
            else:
                continue

            delivery_risks.append({
                "order_id": order["order_id"],
                "item_id": item["item_id"],
                "product_id": item["product_id"],
                "quantity": item["quantity"],
                "due_date": due.isoformat(),
                "days_remaining": days_remaining,
                "required_hours": round(need, 2),
                "capacity_before_due": round(capacity_before_due, 2),
                "priority": order.get("priority", 2),
                "risk_level": risk,
                "reason": reason,
            })

    return {"delivery_risks": delivery_risks}


def check_quick_path(state: PlanAgentState) -> bool:
    """
    判断是否可走快速路径

    条件：无物料缺口、无产能缺口、无交付风险。
    """
    return not (
        state.get("shortages")
        or state.get("capacity_gaps")
        or state.get("delivery_risks")
    )


def analyze_node(state: PlanAgentState) -> Dict[str, Any]:
    """
    Analyze节点主函数

    顺序：物料缺口 -> 产能缺口 -> 交付风险 -> 快速路径判断
    """
    # 上游取数失败则直接透传，不做无意义分析
    if state.get("error"):
        return {
            "shortages": [],
            "capacity_gaps": [],
            "delivery_risks": [],
            "quick_path": False,
            "analysis_summary": "上游数据获取失败，跳过分析",
        }

    merged: Dict[str, Any] = {}
    merged.update(analyze_shortage(state))
    state.update(merged)

    merged.update(analyze_capacity(state))
    state.update(merged)

    merged.update(analyze_delivery_risk(state))
    state.update(merged)

    shortages = merged.get("shortages") or []
    capacity_gaps = merged.get("capacity_gaps") or []
    delivery_risks = merged.get("delivery_risks") or []

    quick_path = check_quick_path(state)

    if quick_path:
        summary = "无约束冲突，可直接排产"
    else:
        parts = []
        if shortages:
            crit = sum(1 for s in shortages if s.get("risk_level") == "critical")
            parts.append(f"物料缺口 {len(shortages)} 项" + (f"（严重 {crit}）" if crit else ""))
        if capacity_gaps:
            parts.append(f"产能瓶颈 {len(capacity_gaps)} 处")
        if delivery_risks:
            crit = sum(1 for d in delivery_risks if d.get("risk_level") == "critical")
            parts.append(f"交付风险 {len(delivery_risks)} 项" + (f"（严重 {crit}）" if crit else ""))
        summary = "，".join(parts) + "，需要详细规划"

    # ---- RAG 知识检索 ----
    # 根据分析结果检索相关排产规则，为下游策略选择和建议生成提供上下文
    from src.services.rag_helper import build_analysis_query, search_knowledge, get_knowledge_context

    query = build_analysis_query(state)
    knowledge_results = search_knowledge(query, top_k=3)
    knowledge_context = get_knowledge_context(query, top_k=3, max_chars=800)

    if knowledge_results:
        logger.info(f"RAG 检索到 {len(knowledge_results)} 条相关规则: "
                     f"{[r['type'] for r in knowledge_results]}")

    execution_log = state.get("execution_log") or []
    execution_log.append({
        "node": "analyze",
        "action": "analyze_all",
        "result": {
            "shortages_count": len(shortages),
            "capacity_gaps_count": len(capacity_gaps),
            "delivery_risks_count": len(delivery_risks),
            "quick_path": quick_path,
            "analysis_summary": summary,
            "knowledge_query": query,
            "knowledge_results_count": len(knowledge_results),
        },
    })

    return {
        "shortages": shortages,
        "capacity_gaps": capacity_gaps,
        "delivery_risks": delivery_risks,
        "quick_path": quick_path,
        "analysis_summary": summary,
        "knowledge_context": knowledge_context,
        "knowledge_results": knowledge_results,
        "execution_log": execution_log,
    }
