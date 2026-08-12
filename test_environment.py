"""
环境测试脚本
验证Python环境和依赖包是否正常工作
"""
import sys
import os

def test_python_version():
    """测试Python版本"""
    print(f"Python版本: {sys.version}")
    print(f"Python路径: {sys.executable}")
    return True

def test_imports():
    """测试关键依赖包导入"""
    try:
        import langgraph
        print("[OK] LangGraph导入成功")
        
        import langchain_core
        print(f"[OK] LangChain Core版本: {langchain_core.__version__}")
        
        import openai
        print(f"[OK] OpenAI版本: {openai.__version__}")
        
        import psycopg2
        print(f"[OK] psycopg2版本: {psycopg2.__version__}")
        
        import redis
        print(f"[OK] Redis版本: {redis.__version__}")
        
        import celery
        print(f"[OK] Celery版本: {celery.__version__}")
        
        import pymilvus
        print(f"[OK] PyMilvus版本: {pymilvus.__version__}")
        
        from ortools.sat.python import cp_model
        print("[OK] OR-Tools导入成功")
        
        import pandas
        print(f"[OK] Pandas版本: {pandas.__version__}")
        
        import pydantic
        print(f"[OK] Pydantic版本: {pydantic.__version__}")
        
        from loguru import logger
        print("[OK] Loguru导入成功")
        
        return True
    except ImportError as e:
        print(f"[FAIL] 导入失败: {e}")
        return False

def test_config():
    """测试配置模块"""
    try:
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from config.settings import Settings
        settings = Settings()
        print("[OK] 配置模块加载成功")
        print(f"  - LLM提供商: {settings.llm_provider}")
        print(f"  - 数据库: {settings.postgres_db}")
        print(f"  - Redis: {settings.redis_host}:{settings.redis_port}")
        return True
    except Exception as e:
        print(f"[FAIL] 配置加载失败: {e}")
        return False

def main():
    """主测试函数"""
    print("=" * 50)
    print("智能排产计划Agent - 环境测试")
    print("=" * 50)
    
    tests = [
        ("Python版本", test_python_version),
        ("依赖包导入", test_imports),
        ("配置模块", test_config),
    ]
    
    results = []
    for name, test_func in tests:
        print(f"\n[测试] {name}")
        print("-" * 30)
        try:
            result = test_func()
            results.append((name, result))
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
        print("\n环境测试全部通过！可以开始开发了。")
        return 0
    else:
        print("\n部分测试失败，请检查环境配置。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
