"""
智能排产计划Agent - 自定义异常
定义业务相关的异常类型
"""
from typing import Optional, Dict, Any


class PlanAgentError(Exception):
    """Agent基础异常"""
    
    def __init__(self, message: str, code: str = None, details: Dict[str, Any] = None):
        super().__init__(message)
        self.code = code or "UNKNOWN_ERROR"
        self.details = details or {}


class DataFetchError(PlanAgentError):
    """数据获取异常"""
    
    def __init__(self, message: str, source: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="DATA_FETCH_ERROR", details=details)
        self.source = source


class ValidationError(PlanAgentError):
    """数据验证异常"""
    
    def __init__(self, message: str, field: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="VALIDATION_ERROR", details=details)
        self.field = field


class SolverError(PlanAgentError):
    """求解器异常"""
    
    def __init__(self, message: str, status: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="SOLVER_ERROR", details=details)
        self.status = status


class InfeasibleError(SolverError):
    """无可行解异常"""
    
    def __init__(self, message: str = "排产问题无可行解", details: Dict[str, Any] = None):
        super().__init__(message, status="INFEASIBLE", details=details)


class TimeoutError(SolverError):
    """求解超时异常"""
    
    def __init__(self, message: str = "求解超时", timeout_seconds: int = None, details: Dict[str, Any] = None):
        super().__init__(message, status="TIMEOUT", details=details)
        self.timeout_seconds = timeout_seconds


class LLMError(PlanAgentError):
    """LLM调用异常"""
    
    def __init__(self, message: str, model: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="LLM_ERROR", details=details)
        self.model = model


class MCPError(PlanAgentError):
    """MCP服务异常"""
    
    def __init__(self, message: str, server: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="MCP_ERROR", details=details)
        self.server = server


class DatabaseError(PlanAgentError):
    """数据库异常"""
    
    def __init__(self, message: str, operation: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="DATABASE_ERROR", details=details)
        self.operation = operation


class RedisError(PlanAgentError):
    """Redis异常"""
    
    def __init__(self, message: str, operation: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="REDIS_ERROR", details=details)
        self.operation = operation


class MilvusError(PlanAgentError):
    """Milvus异常"""
    
    def __init__(self, message: str, operation: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="MILVUS_ERROR", details=details)
        self.operation = operation


class CeleryError(PlanAgentError):
    """Celery任务异常"""
    
    def __init__(self, message: str, task_id: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="CELERY_ERROR", details=details)
        self.task_id = task_id


class DingTalkError(PlanAgentError):
    """钉钉异常"""
    
    def __init__(self, message: str, webhook: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="DINGTALK_ERROR", details=details)
        self.webhook = webhook


class RetryExhaustedError(PlanAgentError):
    """重试耗尽异常"""
    
    def __init__(self, message: str, retry_count: int = 0, max_retry: int = 3, details: Dict[str, Any] = None):
        super().__init__(message, code="RETRY_EXHAUSTED", details=details)
        self.retry_count = retry_count
        self.max_retry = max_retry


class BusinessRuleError(PlanAgentError):
    """业务规则异常"""
    
    def __init__(self, message: str, rule: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="BUSINESS_RULE_ERROR", details=details)
        self.rule = rule


class ConfigurationError(PlanAgentError):
    """配置异常"""
    
    def __init__(self, message: str, config_key: str = None, details: Dict[str, Any] = None):
        super().__init__(message, code="CONFIGURATION_ERROR", details=details)
        self.config_key = config_key


def handle_exception(func):
    """
    异常处理装饰器
    
    统一处理异常，记录日志，返回友好的错误信息
    """
    from functools import wraps
    from src.utils.logger import get_logger
    
    logger = get_logger(func.__module__)
    
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PlanAgentError as e:
            logger.error(f"业务异常: {e.code} - {str(e)}", extra={"details": e.details})
            raise
        except Exception as e:
            logger.exception(f"未预期的异常: {str(e)}")
            raise PlanAgentError(
                message=f"系统内部错误: {str(e)}",
                code="INTERNAL_ERROR",
                details={"original_error": str(e)}
            )
    
    return wrapper


def handle_async_exception(func):
    """
    异步异常处理装饰器
    """
    from functools import wraps
    from src.utils.logger import get_logger
    
    logger = get_logger(func.__module__)
    
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except PlanAgentError as e:
            logger.error(f"业务异常: {e.code} - {str(e)}", extra={"details": e.details})
            raise
        except Exception as e:
            logger.exception(f"未预期的异常: {str(e)}")
            raise PlanAgentError(
                message=f"系统内部错误: {str(e)}",
                code="INTERNAL_ERROR",
                details={"original_error": str(e)}
            )
    
    return wrapper
