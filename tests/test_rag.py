"""
RAG 服务集成测试

测试策略：
    1. 内存模式（Milvus 不可达时自动降级）
    2. Milvus 模式（服务恢复后验证向量检索）
    3. 种子知识灌入与检索
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.services.rag_service import (
    RAGService, create_sample_knowledge, TfidfEmbedder,
)
from src.services.db import get_milvus
from db_lock import acquire_test_lock, release_test_lock


def test_embedder():
    """TF-IDF 向量化器基本功能"""
    print("测试1: TF-IDF 向量化器...")
    emb = TfidfEmbedder(vocab_size=64)
    corpus = [
        "紧急订单优先排产",
        "物料安全库存不足",
        "工作中心产能日历",
        "交期超期预警",
    ]
    emb.fit(corpus)
    assert emb._fitted

    vec = emb.embed("紧急订单怎么排")
    assert len(vec) == 64
    assert any(v > 0 for v in vec), "向量不应全零"

    # 不同文本应有不同向量
    vec2 = emb.embed("物料库存检查")
    assert vec != vec2

    # 序列化/反序列化
    data = emb.serialize()
    assert len(data) > 0
    emb2 = TfidfEmbedder.deserialize(data)
    assert emb2 is not None
    assert emb2._fitted
    vec3 = emb2.embed("紧急订单怎么排")
    assert vec == vec3, "反序列化后向量应一致"

    print(f"   OK 词汇表{len(emb._vectorizer.vocabulary_)}词, 向量维度{len(vec)}, 序列化{len(data)}字节")


async def test_memory_mode():
    """内存降级模式"""
    print("测试2: 内存降级模式...")
    rag = RAGService(collection_name="test_memory_mode")
    await rag.initialize()

    # 强制内存模式
    rag._milvus_available = False
    rag._memory_docs = {}

    docs = create_sample_knowledge()
    ids = await rag.add_documents(docs)
    assert len(ids) == len(docs)

    stats = rag.get_stats()
    assert stats["backend"] == "memory"
    assert stats["document_count"] == len(docs)

    # 搜索
    results = await rag.search("紧急订单怎么处理", top_k=3)
    assert len(results) > 0, "内存搜索无结果"
    assert "紧急" in results[0].content or "优先" in results[0].content
    print(f"   OK 内存模式: {len(docs)}篇文档, 搜索'紧急订单'返回{len(results)}条, "
          f"top1 score={results[0].score:.2f}")

    # 按类型过滤
    results2 = await rag.search("产能", top_k=5, filter_type="capacity_rule")
    assert len(results2) > 0
    assert all(r.metadata.get("type") == "capacity_rule" for r in results2)
    print(f"   OK 类型过滤: capacity_rule 返回{len(results2)}条")

    # get_context
    ctx = await rag.get_context("物料缺料怎么办", top_k=2)
    assert len(ctx) > 0
    print(f"   OK get_context: {len(ctx)}字符")

    # 删除
    ok = await rag.delete_document(ids[0])
    assert ok
    stats2 = rag.get_stats()
    assert stats2["document_count"] == len(docs) - 1
    print(f"   OK 删除后剩余{stats2['document_count']}篇")


async def test_milvus_mode():
    """Milvus 向量检索模式（需要服务可达）"""
    print("测试3: Milvus 向量检索模式...")
    try:
        client = get_milvus()
        client.list_collections()
    except Exception as e:
        print(f"   SKIP Milvus 不可达: {type(e).__name__}")
        return

    # 使用独立测试集合
    coll = "test_rag_service"
    rag = RAGService(collection_name=coll)
    await rag.initialize()

    # 清空
    await rag.clear()

    # 灌入种子知识
    docs = create_sample_knowledge()
    ids = await rag.add_documents(docs)
    assert len(ids) == len(docs)

    stats = rag.get_stats()
    assert stats["backend"] == "milvus"
    assert stats["document_count"] == len(docs)
    print(f"   OK Milvus 模式: {len(docs)}篇文档已灌入")

    # 向量搜索
    results = await rag.search("紧急订单优先级处理", top_k=3)
    assert len(results) > 0, "向量搜索无结果"
    # 第一个结果应该和"紧急订单"相关
    assert any(kw in results[0].content for kw in ["紧急", "优先", "交期"]), \
        f"top1 不相关: {results[0].content[:50]}"
    print(f"   OK 向量搜索: top1 score={results[0].score:.4f}, "
          f"内容: {results[0].content[:40]}...")

    # 按类型过滤
    results2 = await rag.search("工作中心产能", top_k=5, filter_type="capacity_rule")
    assert len(results2) > 0
    assert all(r.metadata.get("type") == "capacity_rule" for r in results2)
    print(f"   OK 类型过滤: capacity_rule 返回{len(results2)}条")

    # get_context
    ctx = await rag.get_context("插单对现有计划的影响", top_k=2)
    assert len(ctx) > 0
    # 应该检索到插单相关规则
    assert "插单" in ctx or "固定占用" in ctx, f"上下文不相关: {ctx[:80]}"
    print(f"   OK get_context: {len(ctx)}字符, 含插单规则")

    # 词汇表持久化验证
    rag2 = RAGService(collection_name=coll)
    await rag2.initialize()
    assert rag2.embedder._fitted, "重启后词汇表应从 Milvus 加载"
    # 同一查询应返回结果
    results3 = await rag2.search("紧急订单", top_k=1)
    assert len(results3) > 0
    print(f"   OK 词汇表持久化: 重启后搜索正常")

    # 清理
    await rag.clear()
    try:
        client = get_milvus()
        if client.has_collection(coll):
            client.drop_collection(coll)
    except Exception:
        pass
    print(f"   OK 测试集合已清理")


async def test_sample_knowledge_coverage():
    """种子知识覆盖排产核心场景"""
    print("测试4: 种子知识覆盖度...")
    docs = create_sample_knowledge()
    types = {d["metadata"]["type"] for d in docs}
    expected = {"scheduling_rule", "material_rule", "capacity_rule", "due_date_rule", "routing_rule"}
    assert types == expected, f"缺少类型: {expected - types}"

    # 每条知识不应为空
    for d in docs:
        assert len(d["content"]) > 10, f"知识内容过短: {d['content']}"

    print(f"   OK {len(docs)}条知识, 覆盖{len(types)}个类型: {sorted(types)}")


def run_async(coro):
    """同步包装异步测试"""
    return asyncio.get_event_loop().run_until_complete(coro)


if __name__ == "__main__":
    _lock = acquire_test_lock()
    passed = 0

    tests_sync = [test_embedder, test_sample_knowledge_coverage]
    tests_async = [test_memory_mode, test_milvus_mode]

    try:
        for t in tests_sync:
            try:
                t()
                passed += 1
            except AssertionError as e:
                print(f"   FAIL {t.__name__}: {e}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"   ERROR {t.__name__}: {type(e).__name__}: {e}")

        for t in tests_async:
            try:
                run_async(t())
                passed += 1
            except AssertionError as e:
                print(f"   FAIL {t.__name__}: {e}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"   ERROR {t.__name__}: {type(e).__name__}: {e}")

        total = len(tests_sync) + len(tests_async)
        print(f"\n结果: {passed}/{total} 通过")
    finally:
        release_test_lock(_lock)

    sys.exit(0 if passed == len(tests_sync) + len(tests_async) else 1)