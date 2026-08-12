"""
智能排产计划Agent - Input节点
负责解析用户意图并路由到相应的处理流程
"""
from typing import Dict, Any
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

import logging
from src.models.state import PlanAgentState, RequestType

logger = logging.getLogger(__name__)


# 意图解析系统提示词
INTENT_PARSING_PROMPT = """你是一个智能排产助手，负责解析用户的排产需求。

## 你的任务
1. 理解用户的自然语言输入
2. 提取关键信息（订单ID、时间范围、产品等）
3. 确定请求类型

## 支持的请求类型
- new_plan: 新建排产计划（如"安排下周的生产计划"）
- insert_order: 插单处理（如"订单ORD-008紧急，帮我插单"）
- check_shortage: 物料缺料检查（如"检查未来三天的物料缺口"）
- rebalance: 产能负荷重平衡（如"重新平衡产线负荷"）
- check_delivery: 交付达成检查（如"检查订单交付进度"）
- query: 查询信息（如"查看当前排产计划"）

## 输出格式
请以JSON格式返回解析结果：
```json
{
    "request_type": "请求类型",
    "params": {
        "order_ids": ["订单ID列表"],
        "date_range": {"start": "开始日期", "end": "结束日期"},
        "product_ids": ["产品ID列表"],
        "priority": "优先级",
        "其他提取的参数"
    },
    "confidence": 0.95,
    "reasoning": "解析推理过程"
}
```

## 注意事项
- 如果用户输入不明确，设置request_type为null，并在reasoning中说明
- 日期格式统一为YYYY-MM-DD
- 优先级：1-紧急，2-正常，3-低
- **重要**：今天是{today}，请基于此日期计算下周、下下周等相对时间
- request_type 必须严格使用上述列表中的值，不要自创类型名
"""


def parse_user_intent(state: PlanAgentState, llm: BaseChatModel) -> Dict[str, Any]:
    """
    解析用户意图
    
    Args:
        state: 当前状态
        llm: 语言模型实例
        
    Returns:
        更新后的状态字典
    """
    user_input = state["user_input"]

    # 如果调用方已指定 request_type，跳过 LLM 解析以减少延迟
    pre_set_type = state.get("request_type")
    if pre_set_type:
        execution_log = state.get("execution_log", [])
        execution_log.append({
            "node": "input",
            "action": "parse_intent",
            "result": {
                "request_type": pre_set_type,
                "confidence": 1.0,
                "reasoning": "request_type pre-set by caller, skip LLM"
            }
        })
        return {
            "request_type": pre_set_type,
            "params": {},
            "confidence": 1.0,
            "reasoning": "pre-set by caller",
            "execution_log": execution_log,
        }
    
    # 构建消息，注入当前日期帮助 LLM 正确解析相对时间
    from datetime import datetime as _dt
    today_str = _dt.now().strftime("%Y-%m-%d")
    system_content = INTENT_PARSING_PROMPT.replace("{today}", today_str)
    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=f"当前日期：{today_str}\n用户输入：{user_input}")
    ]
    
    try:
        # 调用LLM解析意图
        response = llm.invoke(messages)
        response_content = response.content
        
        # 解析JSON响应
        import json
        # 提取JSON部分（处理可能的markdown代码块）
        json_str = response_content
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]
        
        parsed_result = json.loads(json_str.strip())
        
        request_type = parsed_result.get("request_type")
        params = parsed_result.get("params", {})
        confidence = parsed_result.get("confidence", 0.0)
        reasoning = parsed_result.get("reasoning", "")

        # request_type 映射：LLM 可能返回非标准类型名，统一映射
        _TYPE_MAP = {
            "schedule_production": "new_plan", "schedule": "new_plan",
            "plan": "new_plan", "production_plan": "new_plan", "create_plan": "new_plan",
            "insert": "insert_order", "add_order": "insert_order",
            "shortage": "check_shortage", "material_check": "check_shortage",
            "delivery": "check_delivery", "delivery_check": "check_delivery",
            "balance": "rebalance", "load_balance": "rebalance",
            "view": "query", "query_plan": "query",
        }
        _VALID = ("new_plan", "insert_order", "check_shortage", "check_delivery", "rebalance", "query")
        if request_type and request_type not in _VALID:
            mapped = _TYPE_MAP.get(request_type)
            if mapped:
                logger.info(f"LLM request_type 映射: {request_type} -> {mapped}")
                request_type = mapped
            else:
                logger.warning(f"LLM 返回未知 request_type: {request_type}")
                request_type = None
        
        # 记录解析日志
        execution_log = state.get("execution_log", [])
        execution_log.append({
            "node": "input",
            "action": "parse_intent",
            "result": {
                "request_type": request_type,
                "confidence": confidence,
                "reasoning": reasoning
            }
        })
        
        # 更新消息历史
        messages = state.get("messages", [])
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": f"意图解析完成：{request_type}"})
        
        return {
            "request_type": request_type,
            "request_params": params,
            "messages": messages,
            "execution_log": execution_log
        }
        
    except Exception as e:
        # LLM 调用失败/超时/解析异常，降级为关键词检测
        logger.warning(f"LLM 意图解析失败，降级为关键词检测: {type(e).__name__}: {e}")
        fallback_type = _keyword_fallback(user_input)
        execution_log = state.get("execution_log", [])
        execution_log.append({
            "node": "input", "action": "parse_intent",
            "result": {"request_type": fallback_type, "confidence": 0.5, "reasoning": "LLM降级"},
            "error": str(e)
        })
        return {
            "request_type": fallback_type,
            "request_params": {},
            "confidence": 0.5,
            "reasoning": "LLM降级关键词检测",
            "execution_log": execution_log
        }




def _keyword_fallback(user_input: str) -> str:
    """LLM 不可用时的关键词意图检测降级方案"""
    text = user_input
    if any(kw in text for kw in ["插单", "insert"]):
        return "insert_order"
    if any(kw in text for kw in ["缺料", "物料", "shortage"]):
        return "check_shortage"
    if any(kw in text for kw in ["交付", "交期", "delivery"]):
        return "check_delivery"
    if any(kw in text for kw in ["平衡", "rebalance"]):
        return "rebalance"
    if any(kw in text for kw in ["计划", "排产", "安排", "plan", "生产"]):
        return "new_plan"
    return "new_plan"


def route_by_request_type(state: PlanAgentState) -> str:
    """
    主路由：根据请求类型分发到相应节点
    
    Args:
        state: 当前状态
        
    Returns:
        下一个节点名称
    """
    request_type = state.get("request_type")
    
    # 支持的请求类型 -> data_fetch
    valid_types = [
        RequestType.NEW_PLAN.value,
        RequestType.INSERT_ORDER.value,
        RequestType.CHECK_SHORTAGE.value,
        RequestType.REBALANCE.value,
        RequestType.CHECK_DELIVERY.value
    ]
    
    if request_type in valid_types:
        return "data_fetch"
    elif request_type == RequestType.QUERY.value:
        return "output"  # 查询直接输出
    else:
        return "input"  # 重新解析


def input_node(state: PlanAgentState, llm: BaseChatModel) -> Dict[str, Any]:
    """
    Input节点主函数
    
    Args:
        state: 当前状态
        llm: 语言模型实例
        
    Returns:
        更新后的状态字典
    """
    return parse_user_intent(state, llm)
