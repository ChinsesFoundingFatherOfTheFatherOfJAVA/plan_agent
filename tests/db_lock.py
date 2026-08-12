"""
测试互斥锁

背景：
    test_plan_writer 与 test_integration 共享同一套 PostgreSQL 库表，
    且各自会 DELETE 计划数据、重置 capacity_calendar。
    若两者交叠执行（并发运行或 CI 并行），会互相破坏数据，
    产生与产品逻辑无关的假失败（实测可让 9/9 降为 5/9）。

方案：
    使用 PostgreSQL 会话级 advisory lock 做跨进程互斥，
    同一时刻只允许一个依赖真实库的测试进程运行。
"""
import time
from typing import Any, List

_LOCK_KEY = 918273645  # 本项目测试专用固定键


def acquire_test_lock(timeout_seconds: int = 180) -> List[Any]:
    """
    获取测试互斥锁。

    超时抛错而非默默并发，避免又退化成互相破坏的状态。
    """
    from src.services.db import get_pg_pool

    pool = get_pg_pool()
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
                got = cur.fetchone()[0]
            conn.commit()
            if got:
                return [conn]
        except Exception:
            pool.putconn(conn)
            raise
        pool.putconn(conn)
        time.sleep(1.0)

    raise RuntimeError(
        "获取测试互斥锁超时：疑有另一个依赖真实数据库的测试进程正在运行"
    )


def release_test_lock(holder: List[Any]) -> None:
    """释放锁并归还连接"""
    from src.services.db import get_pg_pool

    if not holder:
        return
    conn = holder[0]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        conn.commit()
    finally:
        get_pg_pool().putconn(conn)