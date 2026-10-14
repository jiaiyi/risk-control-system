"""
电商风控系统 - 数据库连接层

提供 SQLAlchemy 引擎、Session 工厂与 FastAPI 依赖注入。
连接池参数来自配置（NFR-02：基础 10 + 溢出 20 = 最多 30 个并发连接）。
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# ---- 主数据库引擎 ----
engine = create_engine(
    settings.get_database_url(),
    pool_pre_ping=True,          # NFR-13：MySQL 重启后无需重启应用
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=3600,
    echo=False,
)

# ---- Session 工厂 ----
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类"""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI 依赖注入：每个请求一个 Session，请求结束自动关闭。

    **事务边界说明**：本函数不负责 commit。业务层需显式提交
    （SPEC 5.1 要求「单次风控检查写 5 张表在同一事务内提交」）。
    发生异常时回滚，未提交的变更随 Session 关闭一并丢弃。
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_test_engine(db_name: str | None = None):
    """创建测试数据库引擎（不使用连接池）"""
    name = db_name or settings.TEST_DB_NAME
    return create_engine(
        settings.get_database_url(name),
        pool_pre_ping=True,
        echo=False,
    )
