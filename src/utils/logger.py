"""
智能排产计划Agent - 日志模块
提供统一的日志配置和使用方式
"""
import sys
from pathlib import Path
from loguru import logger
from config.settings import settings


def setup_logger():
    """
    配置日志系统
    
    使用loguru作为日志库，支持：
    - 控制台输出（彩色）
    - 文件输出（轮转、压缩）
    - 结构化日志
    """
    # 移除默认的handler
    logger.remove()
    
    # 控制台输出
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )
    
    logger.add(
        sys.stdout,
        format=log_format,
        level=settings.log_level,
        colorize=True
    )
    
    # 文件输出
    log_file = Path(settings.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    logger.add(
        str(log_file),
        format=log_format,
        level=settings.log_level,
        rotation="10 MB",      # 日志文件轮转大小
        retention="30 days",   # 日志保留时间
        compression="zip",     # 压缩旧日志
        encoding="utf-8"
    )
    
    return logger


def get_logger(name: str = None):
    """
    获取日志实例
    
    Args:
        name: 日志名称（通常是模块名）
        
    Returns:
        日志实例
    """
    if name:
        return logger.bind(name=name)
    return logger


# 初始化日志系统
app_logger = setup_logger()
