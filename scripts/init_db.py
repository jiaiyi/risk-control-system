# -*- coding: utf-8 -*-
"""
一键初始化数据库

按顺序完成：
  1. 建库 ecs（若不存在）
  2. 执行 19 张业务表 DDL
  3. 导入业务测试数据
  4. 执行 9 张风控表 DDL
  5. 导入 41 条预置风控规则
  6. 生成补充数据（登录记录 / 领券记录 / 物流投诉记录）

用法（在项目根目录执行）：
    python scripts/init_db.py              # 全量初始化
    python scripts/init_db.py --tables     # 仅建表，不灌数据

前置条件：MySQL 已启动（D:\\idetools_main\\start-db-services.bat）
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

SQL_DIR = ROOT / "sql"

# 各步骤的 SQL 脚本（顺序不可调换）
SQL_STEPS = [
    ("19 张业务表 DDL", "init_business_tables.sql"),
    ("业务测试数据", "init_business_data.sql"),
    ("9 张风控表 DDL", "init_risk_tables.sql"),
    ("41 条预置规则", "init_risk_data.sql"),
]

# 仅建表时需要跑的
TABLES_ONLY = {"init_business_tables.sql", "init_risk_tables.sql"}


def find_mysql_client() -> str:
    """定位 mysql 命令行客户端"""
    env = os.environ.get("MYSQL_CLIENT")
    if env and Path(env).exists():
        return env
    for candidate in [
        r"D:\idetools_main\mysql\bin\mysql.exe",
        r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return "mysql"  # 回退到 PATH


def run_sql(client: str, database: str, sql_file: Path) -> None:
    """执行一个 SQL 脚本（管道方式，支持多语句）"""
    if not sql_file.exists():
        print(f"    ✗ 找不到 {sql_file.name}")
        sys.exit(1)
    cmd = [
        client,
        "-u", settings.DB_USER,
        "-h", settings.DB_HOST,
        f"--port={settings.DB_PORT}",
        "--default-character-set=utf8mb4",
    ]
    if settings.DB_PASSWORD:
        cmd.append(f"-p{settings.DB_PASSWORD}")
    cmd.append(database)

    with open(sql_file, "rb") as fh:
        proc = subprocess.run(cmd, stdin=fh, capture_output=True)
    if proc.returncode != 0:
        print(f"    ✗ 执行失败：{proc.stderr.decode('utf-8', 'ignore')[:400]}")
        sys.exit(1)


def run_admin_sql(client: str, sql: str) -> None:
    """执行不需要指定库的管理语句"""
    cmd = [
        client,
        "-u", settings.DB_USER,
        "-h", settings.DB_HOST,
        f"--port={settings.DB_PORT}",
        "-e", sql,
    ]
    if settings.DB_PASSWORD:
        cmd.append(f"-p{settings.DB_PASSWORD}")
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        print(f"    ✗ 执行失败：{proc.stderr.decode('utf-8', 'ignore')[:400]}")
        sys.exit(1)


def reset_all_tables(client: str, db: str) -> int:
    """清空库中所有表（保留结构）。

    必要性：业务数据脚本用的是 INSERT（不是 REPLACE），
    表里已有数据时会报 `ERROR 1062 Duplicate entry`。
    初始化脚本的语义就是「清空重建」，所以这里统一先 TRUNCATE。
    外键约束期间被临时关闭（SET FOREIGN_KEY_CHECKS=0）。
    """
    cmd = [
        client,
        "-u", settings.DB_USER,
        "-h", settings.DB_HOST,
        f"--port={settings.DB_PORT}",
        "-N", "-B",
        "-e", f"SELECT table_name FROM information_schema.tables WHERE table_schema='{db}'",
    ]
    if settings.DB_PASSWORD:
        cmd.append(f"-p{settings.DB_PASSWORD}")
    proc = subprocess.run(cmd, capture_output=True)
    names = [t.strip() for t in proc.stdout.decode("utf-8", "ignore").splitlines() if t.strip()]
    if not names:
        return 0

    stmts = ["USE `" + db + "`;", "SET FOREIGN_KEY_CHECKS=0;"]
    stmts += [f"TRUNCATE TABLE `{n}`;" for n in names]
    stmts.append("SET FOREIGN_KEY_CHECKS=1;")

    cmd2 = [
        client,
        "-u", settings.DB_USER,
        "-h", settings.DB_HOST,
        f"--port={settings.DB_PORT}",
        "-e", " ".join(stmts),
    ]
    if settings.DB_PASSWORD:
        cmd2.append(f"-p{settings.DB_PASSWORD}")
    proc2 = subprocess.run(cmd2, capture_output=True)
    if proc2.returncode != 0:
        print(f"    ✗ 清空失败：{proc2.stderr.decode('utf-8', 'ignore')[:400]}")
        sys.exit(1)
    return len(names)


def main() -> None:
    tables_only = "--tables" in sys.argv
    client = find_mysql_client()
    db = settings.DB_NAME

    print("=" * 62)
    print(f"数据库初始化  |  {settings.DB_HOST}:{settings.DB_PORT}  库: {db}")
    print(f"客户端        |  {client}")
    print(f"模式          |  {'仅建表' if tables_only else '全量初始化'}")
    print("=" * 62)

    # --- 1. 建库 + 清空 ---
    print(f"\n[1/6] 创建数据库 {db}")
    run_admin_sql(
        client,
        f"CREATE DATABASE IF NOT EXISTS `{db}` "
        f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
    )
    print("    ✓ 就绪")

    if not tables_only:
        n = reset_all_tables(client, db)
        print(f"    ✓ 清空 {n} 张表（保证脚本可重复执行）")

    # --- 2~5. 依次执行 SQL ---
    steps = [s for s in SQL_STEPS if (s[1] in TABLES_ONLY or not tables_only)]
    for idx, (label, filename) in enumerate(steps, start=2):
        print(f"\n[{idx}/6] {label}  ← {filename}")
        run_sql(client, db, SQL_DIR / filename)
        print("    ✓ 完成")

    if tables_only:
        print("\n仅建表模式完成。")
        return

    # --- 6. 补充数据 ---
    print("\n[6/6] 生成补充数据（登录 / 领券 / 物流投诉）")
    from scripts.gen_extra_data import main as gen_main  # noqa: PLC0415

    gen_main()

    # 数据重灌后必须清空缓存，否则旧特征会污染新数据
    # （SPEC 4.5「写后失效」原则的落地）
    try:
        from app import cache  # noqa: PLC0415

        n = cache.safe_delete_prefix("risk:")
        print(f"\n    ✓ 已清理 Redis 缓存 {n} 个键（避免旧特征污染）")
    except Exception as exc:  # noqa: BLE001
        print(f"\n    ! 缓存清理跳过（Redis 不可用）：{exc}")

    print("\n" + "=" * 62)
    print("初始化完成。")
    print("=" * 62)


if __name__ == "__main__":
    main()
