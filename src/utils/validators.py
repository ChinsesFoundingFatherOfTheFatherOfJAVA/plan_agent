"""
智能排产计划Agent - 数据验证工具
提供数据验证和校验功能
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, date
from pydantic import BaseModel, Field, validator
from enum import Enum


class OrderStatus(str, Enum):
    """订单状态"""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Priority(int, Enum):
    """优先级"""
    URGENT = 1
    NORMAL = 2
    LOW = 3


class OrderItemValidator(BaseModel):
    """订单明细验证"""
    item_id: str = Field(..., description="明细ID")
    product_id: str = Field(..., description="产品ID")
    quantity: float = Field(..., gt=0, description="数量")
    due_date: Optional[str] = Field(None, description="交期")
    
    @validator("due_date")
    def validate_due_date(cls, v):
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise ValueError("日期格式必须为YYYY-MM-DD")
        return v


class OrderValidator(BaseModel):
    """订单验证"""
    order_id: str = Field(..., description="订单ID")
    customer_id: str = Field(..., description="客户ID")
    order_date: str = Field(..., description="下单日期")
    required_date: str = Field(..., description="要求交期")
    priority: int = Field(default=2, ge=1, le=3, description="优先级")
    status: str = Field(default="pending", description="状态")
    items: List[OrderItemValidator] = Field(..., min_items=1, description="订单明细")
    
    @validator("order_date", "required_date")
    def validate_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("日期格式必须为YYYY-MM-DD")
        return v
    
    @validator("status")
    def validate_status(cls, v):
        valid_statuses = [s.value for s in OrderStatus]
        if v not in valid_statuses:
            raise ValueError(f"状态必须是{valid_statuses}之一")
        return v


class MaterialValidator(BaseModel):
    """物料验证"""
    material_id: str = Field(..., description="物料ID")
    material_name: str = Field(..., description="物料名称")
    material_type: str = Field(..., description="类型")
    unit: str = Field(..., description="单位")
    safety_stock: float = Field(default=0, ge=0, description="安全库存")
    lead_time_days: int = Field(default=0, ge=0, description="采购提前期")
    
    @validator("material_type")
    def validate_material_type(cls, v):
        valid_types = ["raw", "semi", "finished"]
        if v not in valid_types:
            raise ValueError(f"物料类型必须是{valid_types}之一")
        return v


class WorkCenterValidator(BaseModel):
    """工作中心验证"""
    center_id: str = Field(..., description="工作中心ID")
    center_name: str = Field(..., description="名称")
    capacity_per_day: float = Field(..., gt=0, description="日产能")
    efficiency_rate: float = Field(default=1.0, gt=0, le=1.0, description="效率系数")
    status: str = Field(default="active", description="状态")
    
    @validator("status")
    def validate_status(cls, v):
        valid_statuses = ["active", "inactive", "maintenance"]
        if v not in valid_statuses:
            raise ValueError(f"状态必须是{valid_statuses}之一")
        return v


class ProductionTaskValidator(BaseModel):
    """生产任务验证"""
    task_id: str = Field(..., description="任务ID")
    order_id: str = Field(..., description="订单ID")
    product_id: str = Field(..., description="产品ID")
    center_id: str = Field(..., description="工作中心ID")
    quantity: float = Field(..., gt=0, description="数量")
    start_time: str = Field(..., description="开始时间")
    end_time: str = Field(..., description="结束时间")
    priority: int = Field(default=2, ge=1, le=3, description="优先级")
    
    @validator("start_time", "end_time")
    def validate_time(cls, v):
        try:
            datetime.fromisoformat(v)
        except ValueError:
            raise ValueError("时间格式必须为ISO格式")
        return v
    
    @validator("end_time")
    def validate_end_after_start(cls, v, values):
        if "start_time" in values:
            start = datetime.fromisoformat(values["start_time"])
            end = datetime.fromisoformat(v)
            if end <= start:
                raise ValueError("结束时间必须大于开始时间")
        return v


def validate_order_data(data: Dict[str, Any]) -> List[str]:
    """
    验证订单数据
    
    Args:
        data: 订单数据
        
    Returns:
        错误信息列表，空列表表示验证通过
    """
    errors = []
    
    try:
        OrderValidator(**data)
    except Exception as e:
        errors.append(f"订单验证失败: {str(e)}")
    
    return errors


def validate_order_item_data(data: Dict[str, Any]) -> List[str]:
    """
    验证订单明细数据
    
    Args:
        data: 订单明细数据
        
    Returns:
        错误信息列表
    """
    errors = []
    
    try:
        OrderItemValidator(**data)
    except Exception as e:
        errors.append(f"订单明细验证失败: {str(e)}")
    
    return errors


def validate_material_data(data: Dict[str, Any]) -> List[str]:
    """
    验证物料数据
    
    Args:
        data: 物料数据
        
    Returns:
        错误信息列表
    """
    errors = []
    
    try:
        MaterialValidator(**data)
    except Exception as e:
        errors.append(f"物料验证失败: {str(e)}")
    
    return errors


def validate_work_center_data(data: Dict[str, Any]) -> List[str]:
    """
    验证工作中心数据
    
    Args:
        data: 工作中心数据
        
    Returns:
        错误信息列表
    """
    errors = []
    
    try:
        WorkCenterValidator(**data)
    except Exception as e:
        errors.append(f"工作中心验证失败: {str(e)}")
    
    return errors


def validate_production_task_data(data: Dict[str, Any]) -> List[str]:
    """
    验证生产任务数据
    
    Args:
        data: 生产任务数据
        
    Returns:
        错误信息列表
    """
    errors = []
    
    try:
        ProductionTaskValidator(**data)
    except Exception as e:
        errors.append(f"生产任务验证失败: {str(e)}")
    
    return errors


def validate_schedule_tasks(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    验证排产任务列表
    
    检查：
    1. 数据格式验证
    2. 时间重叠检查
    3. 交期约束检查
    
    Args:
        tasks: 排产任务列表
        
    Returns:
        验证结果
    """
    errors = []
    warnings = []
    
    # 按工作中心分组
    tasks_by_center = {}
    for task in tasks:
        center_id = task.get("center_id")
        if center_id not in tasks_by_center:
            tasks_by_center[center_id] = []
        tasks_by_center[center_id].append(task)
    
    # 检查每个工作中心的时间重叠
    for center_id, center_tasks in tasks_by_center.items():
        sorted_tasks = sorted(center_tasks, key=lambda t: t.get("start_time", ""))
        
        for i in range(len(sorted_tasks) - 1):
            current_end = datetime.fromisoformat(sorted_tasks[i]["end_time"])
            next_start = datetime.fromisoformat(sorted_tasks[i + 1]["start_time"])
            
            if current_end > next_start:
                errors.append(
                    f"工作中心{center_id}上任务{sorted_tasks[i]['task_id']}和"
                    f"{sorted_tasks[i + 1]['task_id']}时间重叠"
                )
    
    # 检查交期约束
    for task in tasks:
        if task.get("due_date"):
            due_date = datetime.fromisoformat(task["due_date"])
            end_time = datetime.fromisoformat(task["end_time"])
            
            if end_time > due_date:
                errors.append(
                    f"任务{task['task_id']}完成时间超过交期"
                )
            elif (due_date - end_time).days < 1:
                warnings.append(
                    f"任务{task['task_id']}交期紧迫，仅剩{(due_date - end_time).days}天"
                )
    
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }
