"""
应用配置模块
使用pydantic-settings管理环境变量
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
import os


class Settings(BaseSettings):
    """应用配置类"""
    
    # LLM配置
    llm_provider: str = Field(default="qwen", description="LLM提供商")
    llm_model_name: str = Field(default="qwen3", description="LLM模型名称")
    llm_api_key: Optional[str] = Field(default=None, description="LLM API密钥")
    llm_base_url: str = Field(default="http://localhost:8000/v1", description="LLM API地址")
    
    # PostgreSQL配置
    postgres_host: str = Field(default="localhost", description="PostgreSQL主机")
    postgres_port: int = Field(default=5432, description="PostgreSQL端口")
    postgres_db: str = Field(default="plan_agent", description="数据库名称")
    postgres_user: str = Field(default="postgres", description="数据库用户")
    postgres_password: Optional[str] = Field(default=None, description="数据库密码")
    
    # Redis配置
    redis_host: str = Field(default="localhost", description="Redis主机")
    redis_port: int = Field(default=6379, description="Redis端口")
    redis_db: int = Field(default=0, description="Redis数据库")
    redis_password: Optional[str] = Field(default=None, description="Redis密码")
    
    # Milvus配置
    milvus_host: str = Field(default="localhost", description="Milvus主机")
    milvus_port: int = Field(default=19530, description="Milvus端口")
    milvus_collection_name: str = Field(default="plan_agent_vectors", description="Milvus集合名称")
    
    # Celery配置
    celery_broker_url: str = Field(
        default="redis://localhost:6379/1", 
        description="Celery Broker URL"
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/2", 
        description="Celery Result Backend"
    )
    
    # 日志配置
    log_level: str = Field(default="DEBUG", description="日志级别")
    log_file: str = Field(default="logs/plan_agent.log", description="日志文件路径")
    
    # 应用配置
    app_env: str = Field(default="development", description="应用环境")
    app_debug: bool = Field(default=True, description="调试模式")
    
    # OR-Tools配置
    ortools_max_time_seconds: int = Field(default=30, description="OR-Tools求解最大时间(秒)")
    ortools_num_workers: int = Field(default=4, description="OR-Tools并行求解数")
    
    # 排产配置
    max_auto_retry: int = Field(default=3, description="自动重试最大次数")
    default_planning_horizon_days: int = Field(default=30, description="默认排产周期(天)")
    

    # 钉钉机器人配置
    dingtalk_app_id: Optional[str] = Field(default=None, description="钉钉应用ID")
    dingtalk_agent_id: Optional[str] = Field(default=None, description="钉钉AgentId")
    dingtalk_client_id: Optional[str] = Field(default=None, description="钉钉Client ID (AppKey)")
    dingtalk_client_secret: Optional[str] = Field(default=None, description="钉钉Client Secret (AppSecret)")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# 全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings
