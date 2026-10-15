# -*- coding: utf-8 -*-
"""pytest 公共夹具 —— 测试隔离的唯一入口

## 为什么要隔离三层

1. **数据库**：所有测试跑在独立库 `ecs_test`（`settings.TEST_DB_NAME`），与
   演示库 `ecs` 物理隔离。这里有一个**不可动摇的前提**：覆写 `DB_NAME` 环境变量
   必须发生在 `import app.config` **之前** —— `settings` 是模块级单例、
   `app.database.engine` 同样在模块级创建，一旦 import 就固定了连接目标，
   之后再改就晚了。所以本文件顶部那几行 `os.environ` 看着突兀，但那是唯一正确的位置。

2. **Redis**：切到 db 2（`.env` 里 `REDIS_DB=1` 是主库缓存），每个用例结束后
   清空 `risk:*` 前缀的全部键（含 `risk:auth:sess:*` 会话）。

3. **数据**：service 层**自己 commit**（`list_service.add_list_item`、
   `decision.run_risk_check` 末行、`event.process_event` 等），外层事务回滚拦不住
   已提交的写入 —— 所以隔离策略是「每用例结束后清空风控运行表」，
   **不是**常见的「每用例开事务再回滚」。保留 41 条预置规则与 19 张业务只读表，
   它们是各场景赖以成立的「既定事实」。

> 与 `scripts/smoke_*.py` 的关系：那些脚本是**破坏性**的（会清空 9 张风控运行表），
> 历史上出现过「单跑通过、连跑失败」的隐性耦合。pytest 用例**不要**直接调用
> 它们，需要复用逻辑时只 import `scripts/_smoke.py` 里的工具函数。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ============================================================
# ⚠️ 必须排在 import app.* 之前（原因见模块 docstring 第 1 条）
# ============================================================
TEST_DB = os.environ.get("RC_TEST_DB", "ecs_test")
TEST_REDIS_DB = os.environ.get("RC_TEST_REDIS_DB", "2")
os.environ["DB_NAME"] = TEST_DB
os.environ["REDIS_DB"] = TEST_REDIS_DB

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402


# ============================================================
# 内部工具
# ============================================================
def _admin_engine():
    """连到「不带库名」的 DSN —— 库还不存在时连 `ecs_test` 是连不上的"""
    from app.config import settings

    url = (
        f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/?charset=utf8mb4"
    )
    return create_engine(url, pool_pre_ping=True)


def _table_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables "
                 "WHERE table_schema = :s"),
            {"s": TEST_DB},
        ).fetchall()
    return {r[0] for r in rows}


def _count(engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0)


def _required_tables() -> set[str]:
    """以 ORM 元数据为准（29 张表），避免在测试里再抄一份表名清单"""
    os.environ.setdefault("DB_NAME", TEST_DB)
    from app.models import Base

    return set(Base.metadata.tables)


# ============================================================
# 会话级：准备测试库（幂等）
# ============================================================
@pytest.fixture(scope="session", autouse=True)
def _prepared_db():
    """把测试库准备到「可跑测试」的状态，已就绪就走最快路径。

    复用 `scripts/init_db.py` 的 `run_sql` / `find_mysql_client`，不另写一套 DDL —— 
    否则测试库的表结构与生产库会随 ORM 变更而漂移。
    """
    from app.config import settings
    from app.database import get_test_engine
    from scripts._smoke import refresh_scenario_data
    from scripts.init_db import SQL_DIR, find_mysql_client, run_sql

    client = find_mysql_client()

    # --- 1. 建库 ---
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{TEST_DB}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"))
            conn.commit()
    finally:
        admin.dispose()

    eng = get_test_engine(TEST_DB)
    try:
        # --- 2. 表结构（缺表才建）---
        if not _required_tables() <= _table_names(eng):
            run_sql(client, TEST_DB, SQL_DIR / "init_business_tables.sql")
            run_sql(client, TEST_DB, SQL_DIR / "init_risk_tables.sql")

        # --- 3. 数据（分表判空，避免「部分初始化」后被迫全量重来）---
        if _count(eng, "user_info") == 0:
            run_sql(client, TEST_DB, SQL_DIR / "init_business_data.sql")
        # 规则数不是 41 就说明有残留（用例新建的规则没清干净）。
        # init_risk_data.sql 会先 TRUNCATE 再灌 41 条，正好用于归零。
        if _count(eng, "risk_rule") != 41:
            run_sql(client, TEST_DB, SQL_DIR / "init_risk_data.sql")
    finally:
        eng.dispose()

    # --- 4. 时间窗数据（登录 / 领券 / 物流投诉）---
    # 场景规则依赖「近 1 小时」「近 24 小时」这类相对窗口，放久了就滑出去。
    # 这里**无条件重建**而不是复用 `ensure_scenario_data` —— 后者的新鲜度判据
    # 只看 L001（1002 近 1 小时失败登录 ≥5），而 C001/C002 依赖的是
    # 「1003/1006 近 1 小时领券次数」，比登录那条更早滑出窗口。
    # 只靠单一判据会出现「L001 还新鲜、领券场景已经失效」的假回归。
    refresh_scenario_data()

    # --- 5. 全局缓存口径：清一次，避免上一次运行留下的键干扰首个用例 ---
    _flush_cache()

    yield TEST_DB


# ============================================================
# 用例级：数据与缓存清理
# ============================================================
def _flush_cache() -> None:
    try:
        from app import cache

        cache.safe_delete_prefix("risk:")
    except Exception:  # noqa: BLE001 —— Redis 不可用时测试仍应能跑（走直查路径）
        pass


@pytest.fixture(autouse=True)
def _clean_state(_prepared_db):
    """每个用例跑完后把状态恢复到干净基线。

    · 清空 9 张风控运行表（`risk_rule` 除外 —— 41 条预置规则是场景前提）
    · **删除本用例新增的规则**：规则是用例最容易留下的残留物，不删的话
      `GET /api/rules?keyword=...` 会数到上一次运行留下的同名规则 —— 表现为
      「同一份代码、单跑通过、连跑失败」这类极难定位的假回归。
    · 复位被临时改动的规则状态（is_enabled / is_deleted）
    """
    from app.database import SessionLocal
    from app.models import RiskRule

    baseline = SessionLocal()
    try:
        before_ids = {rid for (rid,) in baseline.query(RiskRule.rule_id).all()}
    finally:
        baseline.close()

    yield

    from scripts._smoke import reset_risk_tables

    reset_risk_tables()

    session = SessionLocal()
    try:
        if before_ids:
            (session.query(RiskRule)
             .filter(~RiskRule.rule_id.in_(before_ids))
             .delete(synchronize_session=False))
        session.query(RiskRule).filter(RiskRule.is_enabled != 1).update({"is_enabled": 1})
        session.query(RiskRule).filter(RiskRule.is_deleted != 0).update({"is_deleted": 0})
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    finally:
        session.close()

    _flush_cache()


# ============================================================
# 常用夹具
# ============================================================
@pytest.fixture()
def db():
    """直连测试库的 Session（引擎层 / 服务层单测用）"""
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    """**已登录**的 TestClient（风控策略师 —— 权限最全的角色）。

    直接用裸 TestClient 会拿到 401：`app/main.py` 的鉴权中间件默认拦住所有
    `/api/*` 与页面路径。那不代表接口坏了，只代表没登录。
    """
    from scripts._smoke import auth_client

    return auth_client("风控策略师")


@pytest.fixture()
def anon_client():
    """未登录的 TestClient（用于验证 401 与登录页跳转）"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def client_factory():
    """按角色取已登录的 TestClient：`client_factory("风控审核员")`"""
    from scripts._smoke import auth_client

    return auth_client


@pytest.fixture()
def reviewer_client():
    """已登录的「风控审核员」。

    **案件审核（API-12）只属于审核员** —— 权限矩阵里「风控策略师」没有案件审核权
    （策略师管规则与名单，审核员管案件处置），用错角色会拿到 403 而不是 200。
    """
    from scripts._smoke import auth_client

    return auth_client("风控审核员")


@pytest.fixture()
def admin_client():
    """已登录的「系统管理员」（系统配置与健康度页唯一可访问的角色）"""
    from scripts._smoke import auth_client

    return auth_client("系统管理员")
