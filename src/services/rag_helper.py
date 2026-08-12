"""
RAG 知识检索辅助模块

封装各节点对知识库的查询逻辑，避免在节点代码中直接操作 async RAGService。
所有方法返回同步结果（内部用 asyncio.run 或预初始化的 event loop）。

设计原则：
    - 知识检索失败不阻塞排产主流程，失败时返回空结果并打 warning
    - 检索查询基于分析结果自动构造，不需要用户手动指定
    - 结果格式统一为 List[Dict]，含 content / score / type 字段
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 全局 RAG 服务实例（惰性初始化）
_rag = None
_rag_initialized = False


async def _ensure_rag():
    """确保 RAG 服务已初始化"""
    global _rag, _rag_initialized
    if _rag is None:
        from src.services.rag_service import rag_service
        _rag = rag_service
    if not _rag_initialized:
        await _rag.initialize()
        _rag_initialized = True
    return _rag


def _run_async(coro):
    """
    同步执行异步协程

    三种场景：
        1. 无运行中的 event loop（普通脚本、Celery worker）-> asyncio.run
        2. 有运行中的 event loop（FastAPI async 端点）-> 新建线程跑
        3. 不可用 -> 抛异常让调用方 catch
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 场景1：无运行中的 loop
        return asyncio.run(coro)

    # 场景2：有运行中的 loop（FastAPI），在线程中执行
    import threading
    result_holder = {}
    def _run():
        try:
            new_loop = asyncio.new_event_loop()
            try:
                result_holder["result"] = new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=_run)
    t.start()
    t.join()

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder.get("result")


def search_knowledge(query: str, top_k: int = 3,
                     filter_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    检索知识库（同步接口）

    Returns:
        [{doc_id, content, score, type, source}]
        失败时返回空列表
    """
    async def _search():
        rag = await _ensure_rag()
        results = await rag.search(query, top_k=top_k, filter_type=filter_type)
        return [
            {
                "doc_id": r.doc_id,
                "content": r.content,
                "score": round(r.score, 4),
                "type": r.metadata.get("type", ""),
                "source": r.metadata.get("source", ""),
            }
            for r in results
        ]

    try:
        return _run_async(_search())
    except Exception as e:
        logger.warning(f"知识检索失败（不阻塞主流程）: {type(e).__name__}: {e}")
        return []


def get_knowledge_context(query: str, top_k: int = 3,
                          max_chars: int = 1000) -> str:
    """
    获取知识上下文文本（同步接口）

    Returns:
        拼接的上下文文本；失败时返回空字符串
    """
    async def _get():
        rag = await _ensure_rag()
        return await rag.get_context(query, top_k=top_k, max_tokens=max_chars)

    try:
        return _run_async(_get())
    except Exception as e:
        logger.warning(f"知识上下文获取失败: {type(e).__name__}: {e}")
        return ""


def build_analysis_query(state: Dict[str, Any]) -> str:
    """
    根据分析结果构造知识检索查询

    把发现的物料缺口、产能瓶颈、交付风险关键词拼接成查询，
    让知识库返回最相关的排产规则。
    """
    parts = []

    shortages = state.get("shortages") or []
    if shortages:
        levels = {s.get("risk_level", "medium") for s in shortages}
        if "critical" in levels:
            parts.append("物料严重缺料紧急采购")
        else:
            parts.append("物料缺料库存安全")

    gaps = state.get("capacity_gaps") or []
    if gaps:
        parts.append("产能不足工作中心瓶颈")

    risks = state.get("delivery_risks") or []
    if risks:
        levels = {r.get("risk_level", "medium") for r in risks}
        if "critical" in levels:
            parts.append("交期超期交付风险")
        else:
            parts.append("交付进度风险")

    request_type = state.get("request_type")
    if request_type == "insert_order":
        parts.append("插单扰动固定占用")
    elif request_type == "rebalance":
        parts.append("负荷均衡重平衡")

    if not parts:
        parts.append("排产策略规则")

    return " ".join(parts)


def build_strategy_query(strategy: str, state: Dict[str, Any]) -> str:
    """
    为策略选择构造知识检索查询
    """
    strategy_keywords = {
        "priority_first": "优先级 紧急订单 交期",
        "shortest_job": "最短作业 工时 效率",
        "load_balance": "负荷均衡 产能瓶颈",
    }
    base = strategy_keywords.get(strategy, "排产策略")
    gaps = state.get("capacity_gaps") or []
    if gaps:
        base += " 产能不足"
    risks = state.get("delivery_risks") or []
    if risks:
        base += " 交付风险"
    return base


def build_recommendation_query(state: Dict[str, Any]) -> str:
    """
    为建议生成构造知识检索查询
    """
    parts = []
    if state.get("shortages"):
        parts.append("缺料采购安全库存")
    if state.get("unscheduled_tasks"):
        parts.append("产能不足加班外协")
    if any(float(t.get("tardiness_hours") or 0) > 0
           for t in (state.get("production_tasks") or [])):
        parts.append("交期超期客户协商")
    if state.get("capacity_gaps"):
        parts.append("产能瓶颈班次调整")
    if state.get("plan_persisted"):
        parts.append("计划确认草稿下发")

    if not parts:
        parts.append("排产计划执行")

    return " ".join(parts)