"""
智能排产计划Agent - RAG知识库服务

架构选择（无 API Key 环境下的务实方案）：

    向量化：jieba 分词 + TF-IDF（scikit-learn），纯本地、无需网络、无需 API Key
    存储：  Milvus FLOAT_VECTOR（维度固定为 VOCAB_SIZE=512，取 Top-K 词频特征）
    检索：  Milvus COSINE 相似度 + 元数据过滤
    降级：  Milvus 不可用时回退到内存关键词匹配，不阻塞主流程

为什么不用 embedding 模型：
    当前环境无 LLM API Key（.env 中为占位符），也没有本地部署的 embedding 模型。
    TF-IDF 在排产知识这种小规模、领域集中的场景下效果足够。
    后续接入通义千问 text-embedding-v3（1024维）时，只需替换 _embed 方法，
    上层 search/get_context 接口不变。

知识来源：
    - 排产规则（scheduling_rule）
    - 物料规则（material_rule）
    - 产能规则（capacity_rule）
    - 交期规则（due_date_rule）
    - 工艺规则（routing_rule）
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from src.services.db import get_milvus
from src.utils.logger import get_logger

logger = get_logger(__name__)

# TF-IDF 词汇表大小（取 Top-K 高频词作为向量维度）
# 512 在排产知识领域够用，且 Milvus 查询效率好
VOCAB_SIZE = 512

# Milvus 集合配置
DEFAULT_COLLECTION = "plan_agent_knowledge"


@dataclass
class SearchResult:
    """检索结果"""
    doc_id: str
    content: str
    score: float
    metadata: Dict[str, Any]


class TfidfEmbedder:
    """
    基于 jieba + TF-IDF 的本地向量化器

    工作原理：
        1. jieba 分词后去停用词
        2. sklearn TfidfVectorizer 拟合词汇表（限 VOCAB_SIZE 个特征）
        3. 文本 -> 归一化 TF-IDF 向量 -> Milvus FLOAT_VECTOR

    持久化：
        词汇表序列化后存入 Milvus 集合的元数据行（id='__vocab__'），
        重启时自动加载，保证向量空间一致。
    """

    def __init__(self, vocab_size: int = VOCAB_SIZE):
        self.vocab_size = vocab_size
        self._vectorizer = None
        self._fitted = False

    def fit(self, corpus: List[str]) -> None:
        """拟合词汇表"""
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD

        tokenized = [self._tokenize(text) for text in corpus]
        # 用空格连接分词结果给 TfidfVectorizer（它默认按空格分词）
        joined = [" ".join(tokens) for tokens in tokenized]

        self._vectorizer = TfidfVectorizer(
            max_features=self.vocab_size,
            token_pattern=r"\S+",  # 按空格分词（已用 jieba 预处理）
        )
        self._vectorizer.fit(joined)
        self._fitted = True
        logger.info(f"TF-IDF 词汇表拟合完成: {len(self._vectorizer.vocabulary_)} 个特征")

    def embed(self, text: str) -> List[float]:
        """文本 -> 归一化 TF-IDF 向量"""
        if not self._fitted:
            # 未拟合时返回零向量（不应发生在正常流程中）
            return [0.0] * self.vocab_size

        tokens = self._tokenize(text)
        joined = " ".join(tokens)
        vec = self._vectorizer.transform([joined]).toarray()[0]

        # 归一化为单位向量（配合 COSINE 度量）
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        # 补零到 vocab_size（Milvus 要求固定维度）
        result = np.zeros(self.vocab_size, dtype=np.float32)
        result[:len(vec)] = vec
        return result.tolist()

    def batch_embed(self, texts: List[str]) -> List[List[float]]:
        """批量向量化"""
        return [self.embed(t) for t in texts]

    def serialize(self) -> bytes:
        """序列化词汇表（压缩后存入 Milvus 元数据行）"""
        if not self._fitted:
            return b""
        import zlib
        raw = pickle.dumps({
            "vectorizer": self._vectorizer,
            "vocab_size": self.vocab_size,
        })
        return zlib.compress(raw)

    @classmethod
    def deserialize(cls, data: bytes) -> Optional["TfidfEmbedder"]:
        """反序列化词汇表"""
        if not data:
            return None
        try:
            import zlib
            # 兼容：先尝试解压，失败则直接反序列化（旧格式）
            try:
                data = zlib.decompress(data)
            except Exception:
                pass
            obj = pickle.loads(data)
            emb = cls(vocab_size=obj["vocab_size"])
            emb._vectorizer = obj["vectorizer"]
            emb._fitted = True
            return emb
        except Exception as e:
            logger.warning(f"词汇表反序列化失败: {e}")
            return None

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """jieba 分词 + 简单停用词过滤"""
        import jieba

        # 排产领域常见停用词
        stop_words = {
            "的", "了", "是", "在", "和", "与", "或", "为", "对", "到",
            "按", "需", "应", "不", "可", "以", "及", "等", "个", "种",
            "进行", "需要", "可以", "应该", "必须", "如果", "则", "时",
        }

        tokens = jieba.lcut(text)
        return [t.strip() for t in tokens if t.strip() and t not in stop_words and len(t) > 1]


class RAGService:
    """
    RAG 知识库服务

    用法：
        rag = RAGService()
        await rag.initialize()
        await rag.add_document("排产规则：紧急订单优先", {"type": "scheduling_rule"})
        results = await rag.search("紧急订单怎么处理")
    """

    VOCAB_ROW_ID = "__vocab__"

    def __init__(self, collection_name: str = DEFAULT_COLLECTION):
        self.collection_name = collection_name
        self.embedder = TfidfEmbedder()
        self._initialized = False
        self._milvus_available = False

    async def initialize(self) -> None:
        """
        初始化：连接 Milvus，加载或拟合词汇表

        如果集合为空，标记为需要拟合（首次 add_document 时触发）。
        如果集合有数据，从元数据行加载词汇表。
        Milvus 不可用时降级为内存模式。
        """
        try:
            client = get_milvus()

            # 确保集合存在
            if not client.has_collection(self.collection_name):
                self._create_collection(client)

            client.load_collection(self.collection_name)

            # 尝试加载词汇表
            vocab = self._load_vocab(client)
            if vocab:
                self.embedder = vocab
                logger.info("从 Milvus 加载 TF-IDF 词汇表成功")
            else:
                logger.info("Milvus 中无词汇表，将在首次添加文档时拟合")

            self._milvus_available = True
            self._initialized = True
            logger.info(f"RAG 服务初始化完成 (Milvus, 集合: {self.collection_name})")

        except Exception as e:
            logger.warning(f"Milvus 连接失败，降级为内存模式: {type(e).__name__}: {e}")
            self._milvus_available = False
            self._initialized = True
            # 内存存储
            self._memory_docs: Dict[str, Dict[str, Any]] = {}

    def _create_collection(self, client) -> None:
        """创建 Milvus 集合"""
        from pymilvus import DataType

        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=VOCAB_SIZE)
        schema.add_field("content", DataType.VARCHAR, max_length=8192)
        schema.add_field("doc_type", DataType.VARCHAR, max_length=64)
        schema.add_field("source", DataType.VARCHAR, max_length=512)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )

        client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )
        client.load_collection(self.collection_name)
        logger.info(f"Milvus 集合 {self.collection_name} 已创建 (dim={VOCAB_SIZE})")

    def _load_vocab(self, client) -> Optional[TfidfEmbedder]:
        """从 Milvus 元数据行加载词汇表"""
        try:
            rows = client.query(
                self.collection_name,
                filter='id == "__vocab__"',
                output_fields=["content"],
                limit=1,
            )
            if rows:
                import base64
                data = base64.b64decode(rows[0]["content"])
                return TfidfEmbedder.deserialize(data)
        except Exception as e:
            logger.warning(f"加载词汇表失败: {e}")
        return None

    def _save_vocab(self, client) -> None:
        """保存词汇表到 Milvus 元数据行"""
        import base64

        data = self.embedder.serialize()
        if not data:
            return

        # base64 编码后存入 content 字段（VARCHAR max 8192）
        encoded = base64.b64encode(data).decode("ascii")
        if len(encoded) > 8190:
            logger.warning(f"词汇表序列化后过大({len(encoded)}字符)，无法存入 Milvus，"
                          "重启后需要重新拟合")
            return

        # upsert（用 insert + delete 模拟，Milvus 2.6 支持 upsert）
        try:
            client.upsert(
                self.collection_name,
                [{
                    "id": self.VOCAB_ROW_ID,
                    "vector": [0.0] * VOCAB_SIZE,  # 占位向量
                    "content": encoded,
                    "doc_type": "__vocab__",
                    "source": "system",
                }],
            )
        except Exception as e:
            logger.warning(f"保存词汇表失败: {e}")

    async def add_document(
        self,
        content: str,
        metadata: Dict[str, Any] = None,
    ) -> str:
        """
        添加文档到知识库

        Args:
            content: 文档内容
            metadata: 元数据（type, source 等）

        Returns:
            文档ID
        """
        if not self._initialized:
            await self.initialize()

        doc_id = hashlib.md5(content.encode("utf-8")).hexdigest()[:16]
        metadata = metadata or {}
        doc_type = metadata.get("type", "general")
        source = metadata.get("source", "manual")

        if self._milvus_available:
            client = get_milvus()

            # 首次添加：先拟合词汇表
            if not self.embedder._fitted:
                # 收集现有文档 + 新文档作为语料
                corpus = self._collect_corpus(client)
                corpus.append(content)
                self.embedder.fit(corpus)
                self._save_vocab(client)
                # 重新向量化已有文档（词汇表变了）
                self._reindex_all(client)

            vec = self.embedder.embed(content)

            client.upsert(
                self.collection_name,
                [{
                    "id": doc_id,
                    "vector": vec,
                    "content": content,
                    "doc_type": doc_type,
                    "source": source,
                }],
            )
            logger.info(f"文档已添加: {doc_id} (type={doc_type})")
        else:
            # 内存模式
            self._memory_docs[doc_id] = {
                "content": content,
                "doc_type": doc_type,
                "source": source,
                "metadata": metadata,
            }

        return doc_id

    async def add_documents(
        self,
        documents: List[Dict[str, Any]],
    ) -> List[str]:
        """
        批量添加文档

        首次添加时一次性拟合词汇表，效率高于逐条添加。
        """
        if not self._initialized:
            await self.initialize()

        if self._milvus_available and not self.embedder._fitted:
            client = get_milvus()
            # 一次性拟合
            existing = self._collect_corpus(client)
            new_corpus = [d["content"] for d in documents]
            self.embedder.fit(existing + new_corpus)
            self._save_vocab(client)
            # 重索引已有文档
            if existing:
                self._reindex_all(client)

        doc_ids = []
        for doc in documents:
            doc_id = await self.add_document(
                content=doc["content"],
                metadata=doc.get("metadata", {}),
            )
            doc_ids.append(doc_id)

        return doc_ids

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter_type: Optional[str] = None,
    ) -> List[SearchResult]:
        """
        语义检索

        Args:
            query: 查询文本
            top_k: 返回结果数量
            filter_type: 按文档类型过滤（如 scheduling_rule）

        Returns:
            按相关度排序的检索结果
        """
        if not self._initialized:
            await self.initialize()

        if self._milvus_available and self.embedder._fitted:
            return self._search_milvus(query, top_k, filter_type)
        elif self._milvus_available:
            # Milvus 可用但词汇表未拟合（集合为空）
            return []
        else:
            return self._search_memory(query, top_k, filter_type)

    def _search_milvus(
        self, query: str, top_k: int, filter_type: Optional[str]
    ) -> List[SearchResult]:
        """Milvus 向量检索"""
        client = get_milvus()
        query_vec = self.embedder.embed(query)

        # 构建过滤条件
        filt = 'id != "__vocab__"'
        if filter_type:
            filt += f' and doc_type == "{filter_type}"'

        results = client.search(
            collection_name=self.collection_name,
            data=[query_vec],
            limit=top_k,
            filter=filt,
            output_fields=["content", "doc_type", "source"],
        )

        if not results or not results[0]:
            return []

        out = []
        for hit in results[0]:
            entity = hit.get("entity", {})
            out.append(SearchResult(
                doc_id=hit.get("id", ""),
                content=entity.get("content", ""),
                score=float(hit.get("distance", hit.get("score", 0))),
                metadata={
                    "type": entity.get("doc_type", ""),
                    "source": entity.get("source", ""),
                },
            ))
        return out

    def _search_memory(
        self, query: str, top_k: int, filter_type: Optional[str]
    ) -> List[SearchResult]:
        """内存模式关键词匹配（降级）"""
        results = []
        query_tokens = set(TfidfEmbedder._tokenize(query))

        for doc_id, doc in self._memory_docs.items():
            if filter_type and doc["doc_type"] != filter_type:
                continue
            doc_tokens = set(TfidfEmbedder._tokenize(doc["content"]))
            overlap = len(query_tokens & doc_tokens)
            if overlap > 0:
                score = overlap / max(len(query_tokens), 1)
                results.append(SearchResult(
                    doc_id=doc_id,
                    content=doc["content"],
                    score=score,
                    metadata={"type": doc["doc_type"], "source": doc["source"]},
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def get_context(
        self,
        query: str,
        max_tokens: int = 2000,
        top_k: int = 3,
        filter_type: Optional[str] = None,
    ) -> str:
        """
        获取 RAG 增强上下文

        检索 top_k 文档并拼接为上下文文本，供 LLM 或规则引擎参考。
        """
        results = await self.search(query, top_k=top_k, filter_type=filter_type)

        if not results:
            return ""

        parts = []
        total_len = 0
        for r in results:
            content = r.content
            if total_len + len(content) > max_tokens:
                content = content[:max_tokens - total_len] + "..."
            parts.append(content)
            total_len += len(content)
            if total_len >= max_tokens:
                break

        return "\n\n".join(parts)

    def _collect_corpus(self, client) -> List[str]:
        """收集集合中所有现有文档内容（排除词汇表行）"""
        try:
            rows = client.query(
                self.collection_name,
                filter='id != "__vocab__"',
                output_fields=["content"],
                limit=10000,
            )
            return [r["content"] for r in rows if r.get("content")]
        except Exception:
            return []

    def _reindex_all(self, client) -> None:
        """词汇表变化后重新向量化所有文档"""
        try:
            rows = client.query(
                self.collection_name,
                filter='id != "__vocab__"',
                output_fields=["id", "content", "doc_type", "source"],
                limit=10000,
            )
            if not rows:
                return

            records = []
            for r in rows:
                vec = self.embedder.embed(r["content"])
                records.append({
                    "id": r["id"],
                    "vector": vec,
                    "content": r["content"],
                    "doc_type": r.get("doc_type", "general"),
                    "source": r.get("source", ""),
                })

            client.upsert(self.collection_name, records)
            logger.info(f"重索引 {len(records)} 条文档")
        except Exception as e:
            logger.warning(f"重索引失败: {e}")

    async def delete_document(self, doc_id: str) -> bool:
        """删除文档"""
        if not self._initialized:
            await self.initialize()

        if self._milvus_available:
            try:
                client = get_milvus()
                client.delete(self.collection_name, filter=f'id == "{doc_id}"')
                return True
            except Exception as e:
                logger.warning(f"删除失败: {e}")
                return False
        else:
            if doc_id in self._memory_docs:
                del self._memory_docs[doc_id]
                return True
            return False

    async def clear(self) -> None:
        """清空知识库（保留词汇表行）"""
        if not self._initialized:
            await self.initialize()

        if self._milvus_available:
            try:
                client = get_milvus()
                client.delete(self.collection_name, filter='id != "__vocab__"')
                logger.info("知识库已清空")
            except Exception as e:
                logger.warning(f"清空失败: {e}")
        else:
            self._memory_docs.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计"""
        if self._milvus_available:
            try:
                client = get_milvus()
                client.flush(self.collection_name)
                stats = client.get_collection_stats(self.collection_name)
                row_count = stats.get("row_count", 0)
                # 减去词汇表行
                if row_count > 0:
                    row_count -= 1
                return {
                    "collection_name": self.collection_name,
                    "document_count": row_count,
                    "backend": "milvus",
                    "vocab_fitted": self.embedder._fitted,
                    "initialized": self._initialized,
                }
            except Exception as e:
                logger.warning(f"获取统计失败: {e}")

        return {
            "collection_name": self.collection_name,
            "document_count": len(getattr(self, "_memory_docs", {})),
            "backend": "memory",
            "vocab_fitted": self.embedder._fitted,
            "initialized": self._initialized,
        }


# ============ 知识导入器 ============

class ExcelKnowledgeImporter:
    """从 Excel 文件导入知识文档"""

    @staticmethod
    async def import_from_excel(
        file_path: str,
        rag_service: RAGService,
        doc_type: str = "general",
    ) -> int:
        """从 Excel 导入知识"""
        try:
            import pandas as pd

            df = pd.read_excel(file_path)
            documents = []
            for idx, row in df.iterrows():
                content = " | ".join(
                    [f"{col}: {val}" for col, val in row.items() if pd.notna(val)]
                )
                documents.append({
                    "content": content,
                    "metadata": {"type": doc_type, "source": file_path, "row": idx},
                })

            doc_ids = await rag_service.add_documents(documents)
            return len(doc_ids)

        except Exception as e:
            logger.error(f"导入 Excel 失败: {e}")
            return 0


# ============ 种子知识 ============

def create_sample_knowledge() -> List[Dict[str, Any]]:
    """
    创建排产领域种子知识

    涵盖排产规则、物料规则、产能规则、交期规则、工艺规则五大类。
    后续可通过 API 或 Excel 导入更多知识。
    """
    return [
        # ---- 排产规则 ----
        {
            "content": "紧急订单（优先级1）必须在交期内完成，排产时优先分配到工时最短的工作中心。如果产能不足，可以通过加班或外协解决。",
            "metadata": {"type": "scheduling_rule", "priority": "high"},
        },
        {
            "content": "排产策略选择：有交付风险时用优先级优先策略，有产能瓶颈时用负荷均衡策略，无冲突时用最短作业优先策略。",
            "metadata": {"type": "scheduling_rule"},
        },
        {
            "content": "插单场景：新订单插入后，已有确认计划的任务作为固定占用，求解器不允许移动它们。扰动评分超过60分时建议拒绝插单。",
            "metadata": {"type": "scheduling_rule"},
        },
        {
            "content": "排产计划默认为草稿状态（draft），需要人工确认后转为确认状态（confirmed），确认后任务状态从planned变为scheduled。",
            "metadata": {"type": "scheduling_rule"},
        },
        # ---- 物料规则 ----
        {
            "content": "物料安全库存不参与生产消耗，可用量 = 在库 - 已预留 - 安全库存 + 在途。在途物料只有在采购提前期能覆盖交期时才计入。",
            "metadata": {"type": "material_rule"},
        },
        {
            "content": "缺料风险分级：缺口超过需求的50%或交期前无法到货为critical，超过20%为high，其余为medium。critical级别需要立即采购。",
            "metadata": {"type": "material_rule"},
        },
        {
            "content": "BOM（物料清单）定义了产品所需的原材料及数量。排产前必须按BOM展开物料需求，检查库存是否充足。",
            "metadata": {"type": "material_rule"},
        },
        # ---- 产能规则 ----
        {
            "content": "工作中心日产能 = 可用工时 × 效率系数。例如16小时 × 0.95 = 15.2小时。任务只排在工作时间内，夜间和周末不排产。",
            "metadata": {"type": "capacity_rule", "center": "WC-001"},
        },
        {
            "content": "产能日历记录每个工作中心每天的可用工时、已排产工时和实际工时。排产后自动回写planned_hours，不得超过available_hours。",
            "metadata": {"type": "capacity_rule"},
        },
        {
            "content": "停机维护的工作中心（status=maintenance）不参与排产。产能不足时，未排产的任务会进入unscheduled_tasks并发出预警。",
            "metadata": {"type": "capacity_rule"},
        },
        # ---- 交期规则 ----
        {
            "content": "订单明细的交期优先于订单头的需求日期。纯日期交期按当日23:59处理。任务完工时间超过交期会产生超期工时（tardiness_hours）。",
            "metadata": {"type": "due_date_rule"},
        },
        {
            "content": "交付风险判定：交期已过期为critical，交期前无可用产能为critical，所需工时超过交期前产能的80%为high，交期仅剩2天为medium。",
            "metadata": {"type": "due_date_rule"},
        },
        # ---- 工艺规则 ----
        {
            "content": "工艺路线（routing）定义每个产品在各工作中心的标准工时。例如PROD-001在WC-001为0.1小时/件，在WC-002为0.2小时/件。求解器优先分配到工时最短的中心。",
            "metadata": {"type": "routing_rule"},
        },
        {
            "content": "跨天任务会被拆分为多个生产时段（segments），每段对应一个工作日。落库时一段一行，保证排他约束正确生效。",
            "metadata": {"type": "routing_rule"},
        },
        {
            "content": "换型时间（setup_time）目前为0，预留字段。后续接入后，同一工作中心连续生产不同产品时需要计入换型工时。",
            "metadata": {"type": "routing_rule"},
        },
    ]


# 全局 RAG 服务实例
rag_service = RAGService()