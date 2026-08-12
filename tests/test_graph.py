"""
智能排产计划Agent - 图结构测试
测试LangGraph状态图的构建和基本功能
"""
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.state import create_initial_state, PlanAgentState
from src.agent.graphs.plan_graph import build_plan_graph


def test_state_creation():
    """测试状态创建"""
    print("测试状态创建...")
    
    state = create_initial_state("安排下周的生产计划")
    
    assert state["user_input"] == "安排下周的生产计划"
    assert state["request_type"] is None
    assert state["retry_count"] == 0
    assert state["max_retry"] == 3
    assert state["quick_path"] is False
    assert state["validation_passed"] is False
    
    print("[OK] 状态创建测试通过")


def test_graph_structure():
    """测试图结构"""
    print("测试图结构...")
    
    # 创建一个模拟的LLM（用于测试图结构）
    class MockLLM:
        def invoke(self, messages):
            class MockResponse:
                content = '{"request_type": "new_plan", "params": {}, "confidence": 0.9, "reasoning": "test"}'
            return MockResponse()
    
    mock_llm = MockLLM()
    
    try:
        graph = build_plan_graph(mock_llm)
        print("[OK] 图结构构建成功")
        return True
    except Exception as e:
        print(f"[FAIL] 图结构构建失败: {e}")
        return False


def test_graph_execution():
    """测试图执行"""
    print("测试图执行...")
    
    # 创建一个模拟的LLM
    class MockLLM:
        def invoke(self, messages):
            class MockResponse:
                content = '{"request_type": "new_plan", "params": {}, "confidence": 0.9, "reasoning": "test"}'
            return MockResponse()
    
    mock_llm = MockLLM()
    
    try:
        graph = build_plan_graph(mock_llm)
        initial_state = create_initial_state("安排下周的生产计划")
        
        # 使用invoke执行图（同步）
        result = graph.invoke(initial_state)
        
        print(f"[OK] 图执行成功")
        print(f"  - 请求类型: {result.get('request_type')}")
        print(f"  - 生产任务数: {len(result.get('production_tasks', []))}")
        print(f"  - 计划ID: {result.get('plan_id')}")
        
        return True
    except Exception as e:
        print(f"[FAIL] 图执行失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主测试函数"""
    print("=" * 50)
    print("智能排产计划Agent - 图结构测试")
    print("=" * 50)
    
    tests = [
        ("状态创建", test_state_creation),
        ("图结构", test_graph_structure),
        ("图执行", test_graph_execution),
    ]
    
    results = []
    for name, test_func in tests:
        print(f"\n[测试] {name}")
        print("-" * 30)
        try:
            result = test_func()
            results.append((name, True))
        except Exception as e:
            print(f"[FAIL] 测试异常: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 50)
    print("测试结果汇总")
    print("=" * 50)
    
    passed = 0
    for name, result in results:
        status = "[OK] 通过" if result else "[FAIL] 失败"
        print(f"{status} - {name}")
        if result:
            passed += 1
    
    print(f"\n总计: {passed}/{len(results)} 项测试通过")
    
    if passed == len(results):
        print("\n所有测试通过！")
        return 0
    else:
        print("\n部分测试失败。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
