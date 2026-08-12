"""
智能排产计划Agent - 主状态图
连接所有节点，构建完整的LangGraph工作流
"""
from typing import Dict, Any
from langgraph.graph import StateGraph, END
from langchain_core.language_models import BaseChatModel

from src.models.state import PlanAgentState, create_initial_state
from src.agent.nodes.input_node import input_node, route_by_request_type
from src.agent.nodes.data_fetch_node import data_fetch_node
from src.agent.nodes.output_node import output_node
from src.agent.subgraphs.analyze_subgraph import analyze_node
from src.agent.subgraphs.plan_subgraph import plan_node
from src.agent.subgraphs.validate_subgraph import validate_node, check_validation_result


def build_plan_graph(llm: BaseChatModel) -> StateGraph:
    """
    构建智能排产计划Agent状态图
    
    图结构：
    input -> data_fetch -> analyze -> plan -> validate -> output
    
    Args:
        llm: 语言模型实例
        
    Returns:
        编译后的状态图
    """
    # 创建状态图
    workflow = StateGraph(PlanAgentState)
    
    # 添加节点
    workflow.add_node("input", lambda state: input_node(state, llm))
    workflow.add_node("data_fetch", data_fetch_node)
    workflow.add_node("analyze", analyze_node)
    workflow.add_node("plan", plan_node)
    workflow.add_node("validate", validate_node)
    workflow.add_node("output", output_node)
    
    # 设置入口点
    workflow.set_entry_point("input")
    
    # 添加边
    # input节点：根据请求类型路由
    workflow.add_conditional_edges(
        "input",
        route_by_request_type,
        {
            "data_fetch": "data_fetch",
            "output": "output",
            "input": "input"  # 重新解析
        }
    )
    
    # data_fetch -> analyze
    workflow.add_edge("data_fetch", "analyze")
    
    # analyze -> plan
    workflow.add_edge("analyze", "plan")
    
    # plan -> validate
    workflow.add_edge("plan", "validate")
    
    # validate节点：根据校验结果路由
    workflow.add_conditional_edges(
        "validate",
        check_validation_result,
        {
            "pass": "output",
            "alert": "output",  # 有冲突但仍输出，附带预警
            "retry": "plan"     # 重新排产
        }
    )
    
    # output -> END
    workflow.add_edge("output", END)
    
    # 编译图
    return workflow.compile()


def run_plan_agent_sync(user_input: str, llm: BaseChatModel) -> Dict[str, Any]:
    """
    同步运行智能排产计划Agent
    
    Args:
        user_input: 用户输入
        llm: 语言模型实例
        
    Returns:
        执行结果
    """
    # 构建图
    graph = build_plan_graph(llm)
    
    # 创建初始状态
    initial_state = create_initial_state(user_input)
    
    # 执行图
    result = graph.invoke(initial_state)
    
    return result
