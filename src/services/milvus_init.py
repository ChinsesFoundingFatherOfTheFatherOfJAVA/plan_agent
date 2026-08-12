"""
Milvus 集合初始化
为 RAG 知识库创建集合与索引

集合设计（plan_agent_knowledge）：
    id        VARCHAR   主键，文档ID
    vector    FLOAT_VEC 向量（维度由 embedding 模型决定）
    content   VARCHAR   原文内容
    doc_type  VARCHAR   文档类型（scheduling_rule/material_rule/capacity_rule/...）
    source    VARCHAR   来源（文件名等）
"""
from typing import Optional

from config.settings import settings
from src.services.db import get_milvus

# 维度与 rag_service.TfidfEmbedder.vocab_size 一致（当前 512）
# 若改用 embedding 模型（如千问 text-embedding-v3 的 1024 维），
# 需同步修改 VOCAB_SIZE 并重建集合
DEFAULT_DIM = 512  # 与 rag_service.VOCAB_SIZE 一致


def create_knowledge_collection(
    collection_name: Optional[str] = None,
    dim: int = DEFAULT_DIM,
    drop_existing: bool = False,
) -> str:
    """
    创建知识库集合（幂等）

    Args:
        collection_name: 集合名，默认取配置
        dim: 向量维度
        drop_existing: 是否先删除同名集合（重建）

    Returns:
        集合名
    """
    from pymilvus import DataType

    name = collection_name or settings.milvus_collection_name
    client = get_milvus()

    if client.has_collection(name):
        if not drop_existing:
            return name
        client.drop_collection(name)

    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("content", DataType.VARCHAR, max_length=8192)
    schema.add_field("doc_type", DataType.VARCHAR, max_length=64)
    schema.add_field("source", DataType.VARCHAR, max_length=512)

    # HNSW + COSINE：适合知识条目规模小、要求低延迟的场景
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )

    client.create_collection(
        collection_name=name,
        schema=schema,
        index_params=index_params,
    )
    client.load_collection(name)
    return name


def describe_knowledge_collection(collection_name: Optional[str] = None) -> dict:
    """返回集合信息，用于校验初始化结果"""
    name = collection_name or settings.milvus_collection_name
    client = get_milvus()
    if not client.has_collection(name):
        return {"exists": False, "name": name}
    return {
        "exists": True,
        "name": name,
        "description": client.describe_collection(name),
        "stats": client.get_collection_stats(name),
    }
