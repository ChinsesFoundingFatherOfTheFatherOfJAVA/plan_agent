"""
基础设施初始化脚本

用法：
    python scripts/init_infra.py check          # 只做连通性检查
    python scripts/init_infra.py migrate        # 建库 + 建表
    python scripts/init_infra.py seed           # 导入测试数据
    python scripts/init_infra.py milvus         # 创建 Milvus 集合
    python scripts/init_infra.py all            # 全部执行
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402


def _ok(msg):
    print(f"[OK]   {msg}")


def _fail(msg):
    print(f"[FAIL] {msg}")


def ensure_database() -> bool:
    """确保目标数据库存在（连 postgres 默认库执行 CREATE DATABASE）"""
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    try:
        conn = psycopg2.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname="postgres",
            user=settings.postgres_user,
            password=settings.postgres_password,
            connect_timeout=10,
        )
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.postgres_db,))
            if cur.fetchone():
                _ok(f"数据库已存在: {settings.postgres_db}")
            else:
                cur.execute(f'CREATE DATABASE "{settings.postgres_db}" ENCODING \'UTF8\'')
                _ok(f"数据库已创建: {settings.postgres_db}")
        conn.close()
        return True
    except Exception as e:
        _fail(f"创建数据库失败: {type(e).__name__}: {e}")
        return False


def check_postgres() -> bool:
    try:
        from src.services.db import pg_query
        rows = pg_query("SELECT version() AS v")
        _ok(f"PostgreSQL: {rows[0]['v'].split(',')[0]}")
        return True
    except Exception as e:
        _fail(f"PostgreSQL: {type(e).__name__}: {e}")
        return False


def check_redis() -> bool:
    try:
        from src.services.db import get_redis
        r = get_redis()
        r.ping()
        info = r.info("server")
        _ok(f"Redis: v{info.get('redis_version')}")
        return True
    except Exception as e:
        _fail(f"Redis: {type(e).__name__}: {e}")
        return False


def check_milvus(retries: int = 3, delay: float = 3.0) -> bool:
    """检查 Milvus 连通性

    虚拟机网络存在偶发抖动，单次失败不代表服务不可用，故加入重试。
    """
    import time

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            import src.services.db as db
            db._milvus_client = None  # 抖动后重建连接，避免复用坏连接
            cols = db.get_milvus().list_collections()
            _ok('Milvus: 连接正常, 现有集合=' + str(cols))
            return True
        except Exception as e:
            last_err = e
            if attempt < retries:
                print('       Milvus 第' + str(attempt) + '次连接失败(' + type(e).__name__ + ')，' + str(delay) + 's 后重试...')
                time.sleep(delay)
    _fail('Milvus: ' + type(last_err).__name__ + ': ' + str(last_err))
    return False


def run_sql_file(path: Path) -> bool:
    try:
        from src.services.db import pg_execute_script
        pg_execute_script(path.read_text(encoding="utf-8"))
        _ok(f"已执行: {path.name}")
        return True
    except Exception as e:
        _fail(f"执行 {path.name} 失败: {type(e).__name__}: {e}")
        return False


def migrate() -> bool:
    if not ensure_database():
        return False
    d = ROOT / "database" / "migrations"
    files = sorted(d.glob("*.sql"))
    if not files:
        _fail("未找到迁移脚本")
        return False
    return all(run_sql_file(f) for f in files)


def seed() -> bool:
    d = ROOT / "database" / "seeds"
    files = sorted(d.glob("*.sql"))
    if not files:
        _fail("未找到种子脚本")
        return False
    return all(run_sql_file(f) for f in files)


def init_milvus() -> bool:
    try:
        from src.services.milvus_init import create_knowledge_collection, describe_knowledge_collection
        name = create_knowledge_collection()
        info = describe_knowledge_collection(name)
        _ok(f"Milvus 集合就绪: {name} (exists={info['exists']})")
        return True
    except Exception as e:
        _fail(f"Milvus 集合创建失败: {type(e).__name__}: {e}")
        return False


def verify_tables() -> bool:
    try:
        from src.services.db import pg_query
        rows = pg_query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
        names = [r["table_name"] for r in rows]
        print(f"       共 {len(names)} 张表: {', '.join(names)}")
        counts = []
        for t in ("sales_order", "sales_order_item", "material", "inventory",
                  "bill_of_material", "work_center", "capacity_calendar"):
            if t in names:
                c = pg_query(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"]
                counts.append(f"{t}={c}")
        print(f"       行数: {', '.join(counts)}")
        return True
    except Exception as e:
        _fail(f"校验失败: {type(e).__name__}: {e}")
        return False


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    print("=" * 60)
    print(f"基础设施初始化 - 目标: PG={settings.postgres_host}:{settings.postgres_port} "
          f"Redis={settings.redis_host}:{settings.redis_port} "
          f"Milvus={settings.milvus_host}:{settings.milvus_port}")
    print("=" * 60)

    results = {}
    if cmd in ("check", "all"):
        print("\n-- 连通性检查 --")
        results["redis"] = check_redis()
        results["milvus"] = check_milvus()
    if cmd in ("migrate", "all"):
        print("\n-- 建库建表 --")
        results["migrate"] = migrate()
        results["pg"] = check_postgres()
    if cmd in ("seed", "all"):
        print("\n-- 导入测试数据 --")
        results["seed"] = seed()
    if cmd in ("milvus", "all"):
        print("\n-- Milvus 集合 --")
        results["milvus_collection"] = init_milvus()
    if cmd in ("all", "verify"):
        print("\n-- 结果校验 --")
        results["verify"] = verify_tables()

    print("\n" + "=" * 60)
    failed = [k for k, v in results.items() if not v]
    for k, v in results.items():
        print(f"{'[OK]  ' if v else '[FAIL]'} {k}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
