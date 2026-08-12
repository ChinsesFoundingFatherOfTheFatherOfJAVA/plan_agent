"""
智能排产计划Agent - 状态定义
定义LangGraph状态图的状态结构
"""
from typing import TypedDict, List, Dict, Optional, Annotated
from datetime import datetime
from enum import Enum


class RequestType(str, Enum):
    """请求类型枚举"""
    NEW_PLAN = "new_plan"           # 新建排产计划
    INSERT_ORDER = "insert_order"   # 插单
    CHECK_SHORTAGE = "check_shortage"  # 物料缺料检查
    REBALANCE = "rebalance"         # 产能负荷重平衡
    CHECK_DELIVERY = "check_delivery"  # 交付达成检查
    QUERY = "query"                 # 查询


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PLANNED = "planned"           # 已计划
    SCHEDULED = "scheduled"       # 已排产
    IN_PROGRESS = "in_progress"   # 生产中
    COMPLETED = "completed"       # 已完成


class ConflictSeverity(str, Enum):
    """冲突严重程度"""
    LOW = "low"        # 低风险，可忽略
    MEDIUM = "medium"  # 中等风险，需关注
    HIGH = "high"      # 高风险，需人工确认


class PlanAgentState(TypedDict):
    """
    智能排产计划Agent状态定义
    
    状态流转：input -> data_fetch -> analyze -> plan -> validate -> output
    """
    # ============ 输入层 ============
    user_input: str                          # 用户输入/指令
    request_type: Optional[str]              # 请求类型: new_plan/insert_order/check_shortage/rebalance/check_delivery/query
    request_params: Optional[Dict]           # 请求参数（从LLM解析出的结构化参数）
    
    # ============ 数据层 ============
    orders: Optional[List[Dict]]             # 订单数据
    materials: Optional[List[Dict]]          # 物料数据
    inventory: Optional[List[Dict]]          # 库存数据
    work_centers: Optional[List[Dict]]       # 工作中心数据
    capacity_calendar: Optional[List[Dict]]  # 产能日历数据
    boms: Optional[Dict]                     # BOM映射 {product_id: [{material_id, quantity_required}]}
    routing: Optional[Dict]                  # 工艺路线 {product_id: [{center_id, hours_per_unit}]}
    existing_tasks: Optional[List[Dict]]     # 库中已排产任务（插单/重平衡时作为固定占用）
    planning_window: Optional[Dict]          # 排产时间窗口 {start, end}
    
    # ============ 分析层 ============
    shortages: Optional[List[Dict]]          # 物料缺口
    capacity_gaps: Optional[List[Dict]]      # 产能缺口
    delivery_risks: Optional[List[Dict]]     # 交付风险
    analysis_summary: Optional[str]          # 分析摘要
    knowledge_context: Optional[str]          # RAG检索到的相关规则上下文
    knowledge_results: Optional[List[Dict]]   # RAG检索结果（含score/doc_id/type）
    quick_path: bool                         # 快速路径标志（无约束冲突时可跳过详细分析）
    
    # ============ 排产层 ============
    strategy: Optional[str]                  # 排产策略: priority_first/shortest_job/load_balance/custom
    strategy_params: Optional[Dict]          # 策略参数（LLM建议的权重配置）
    production_tasks: Optional[List[Dict]]   # 生成的生产任务（每项含 segments 真实时间分段）
    unscheduled_tasks: Optional[List[Dict]]  # 因产能不足未能排产的任务（含原因）
    schedule: Optional[Dict]                 # 排产计划元信息（含求解统计）
    plan_id: Optional[str]                   # 计划ID
    plan_persisted: bool                     # 计划是否已成功写入数据库
    persist_result: Optional[Dict]           # 落库结果（段数/工时/日历更新行数）
    
    # ============ 校验层 ============
    validation_passed: bool                  # 校验是否通过
    conflicts: Optional[List[Dict]]          # 冲突列表
    retry_count: int                         # 重试次数
    max_retry: int                           # 最大重试次数（默认3）
    
    # ============ 输出层 ============
    output_format: str                       # 输出格式: text/card/excel
    output_content: Optional[str]            # 输出内容
    alerts: Optional[List[Dict]]             # 预警信息
    recommendations: Optional[List[str]]     # 建议操作
    
    # ============ 元数据 ============
    messages: Annotated[List[Dict], "消息历史"]  # 对话消息历史
    error: Optional[str]                     # 错误信息
    run_id: Optional[str]                    # 本次运行的审计ID（plan_run_log）
    strategy_reason: Optional[str]           # 策略选择理由（可解释性）
    start_time: Optional[datetime]           # 开始时间
    end_time: Optional[datetime]             # 结束时间
    execution_log: Optional[List[Dict]]      # 执行日志


def create_initial_state(user_input: str) -> PlanAgentState:
    """
    创建初始状态
    
    Args:
        user_input: 用户输入的指令
        
    Returns:
        初始化的PlanAgentState
    """
    return PlanAgentState(
        # 输入层
        user_input=user_input,
        request_type=None,
        request_params=None,
        
        # 数据层
        orders=None,
        materials=None,
        inventory=None,
        work_centers=None,
        capacity_calendar=None,
        boms=None,
        routing=None,
        existing_tasks=None,
        planning_window=None,
        
        # 分析层
        shortages=None,
        capacity_gaps=None,
        delivery_risks=None,
        analysis_summary=None,
        knowledge_context=None,
        knowledge_results=None,
        quick_path=False,
        
        # 排产层
        strategy=None,
        strategy_params=None,
        production_tasks=None,
        unscheduled_tasks=None,
        schedule=None,
        plan_id=None,
        plan_persisted=False,
        persist_result=None,
        
        # 校验层
        validation_passed=False,
        conflicts=None,
        retry_count=0,
        max_retry=3,
        
        # 输出层
        output_format="text",
        output_content=None,
        alerts=None,
        recommendations=None,
        
        # 元数据
        messages=[],
        error=None,
        run_id=None,
        strategy_reason=None,
        start_time=datetime.now(),
        end_time=None,
        execution_log=[]
    )
