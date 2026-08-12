"""
数据库连接模块
提供 PostgreSQL 连接池、Redis 客户端、Milvus 客户端的统一入口
"""
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from config.settings import settings

# ============ PostgreSQL ============
_pg_pool = None


def get_pg_pool():
    """获取（惰性初始化）PostgreSQL 连接池"""
    global _pg_pool
    if _pg_pool is None:
        from psycopg2.pool import ThreadedConnectionPool
        _pg_pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            connect_timeout=10,
        )
    return _pg_pool


@contextmanager
def pg_connection():
    """从连接池借出连接，退出时归还；异常自动回滚"""
    pool = get_pg_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def pg_query(sql: str, params: tuple = None) -> List[Dict[str, Any]]:
    """执行查询并返回字典列表"""
    from psycopg2.extras import RealDictCursor
    with pg_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def pg_execute(sql: str, params: tuple = None) -> int:
    """执行写操作，返回受影响行数"""
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def pg_execute_script(sql_text: str) -> None:
    """执行多语句 SQL 脚本（用于建表/种子数据）"""
    with pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)


# ============ Redis ============
_redis_client = None


def get_redis():
    """获取（惰性初始化）Redis 客户端"""
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


# ============ Milvus ============
_milvus_client = None


def get_milvus():
    """获取（惰性初始化）Milvus 客户端"""
    global _milvus_client
    if _milvus_client is None:
        from pymilvus import MilvusClient
        _milvus_client = MilvusClient(
            uri=f"http://{settings.milvus_host}:{settings.milvus_port}",
            timeout=30,
        )
    return _milvus_client


def close_all() -> None:
    """关闭所有连接（进程退出时调用）"""
    global _pg_pool, _redis_client, _milvus_client
    if _pg_pool is not None:
        _pg_pool.closeall()
        _pg_pool = None
    if _redis_client is not None:
        _redis_client.close()
        _redis_client = None
    _milvus_client = None
