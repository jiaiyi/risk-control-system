# -*- coding: utf-8 -*-
"""冒烟脚本公共件

**为什么需要它**

8 个 `smoke_*.py` 各自抄了一份「检查通过就打 [OK]、最后统计失败数」的样板，
以及一份「先清空哪几张表」的列表。列表一分散就有两个后果：

1. 新增一张风控表时得记得改 6 个脚本，漏一个就会出现「同一份代码、单跑通过、
   连跑失败」这类极难定位的偶发失败；
2. 脚本之间的隐性耦合没人看得见 —— `smoke_agent` 清掉的表恰好是
   `smoke_decision` 要用的业务数据所依赖的，顺序连跑就会失败，
   而失败长得像代码回归。

所以这里把两件事收敛成单一来源：

    check / section / summary   —— 统一的判定与汇总（用法与原来的局部函数完全一致）
    reset_risk_tables()         —— 清空风控**运行**表，保留 risk_rule 的 41 条预置规则
    refresh_scenario_data()     —— 重建 9 个端到端场景依赖的相对时间窗业务数据

**边界**：只动 `risk_*` 运行表，绝不碰 19 张业务只读表（清单见
`app/models.py`，脚本仍以业务表作为「既定事实」来驱动场景）。
"""
import io
import sys
from contextlib import redirect_stdout

OK, BAD = "  [OK]  ", "  [!!]  "
WIDTH = 74

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    """记一条断言结果"""
    print((OK if cond else BAD) + msg)
    if not cond:
        failures.append(msg)


def section(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def summary() -> int:
    """打印汇总并返回退出码：0 全过 / 1 有失败。

    用法：`sys.exit(smoke.summary())`
    """
    print()
    print("=" * WIDTH)
    if failures:
        print(f"结果：{len(failures)} 项未通过")
        for f in failures:
            print("  ✗ " + f)
        print("=" * WIDTH)
        return 1
    print("结果：全部通过")
    print("=" * WIDTH)
    return 0


# ============================================================
# 数据准备
# ============================================================
def auth_client(role_or_user: str = "风控策略师", *, follow_redirects: bool = False):
    """返回一个**已登录**的 TestClient。

    **为什么必须有这个函数**：`app/main.py` 的鉴权中间件默认拦住所有 `/api/*`
    与页面路径。用裸 `TestClient(app)` 转发请求会拿到 401 —— 而这**不是**接口
    坏了，是没登录。脚本里断言 `status_code == 200` 时只会看到「期望 200 实际 401」，
    很容易误判成回归。

    传角色名（如 `风控策略师`）或账号（如 `zhangcx`）都行。默认给策略师 ——
    它是权限最全的角色，除系统配置与健康度外基本都能访问。
    """
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app import auth  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415

    user_id = role_or_user
    for u in auth.all_users():
        if role_or_user in (u.user_id, u.role, u.name):
            user_id = u.user_id
            break

    client = TestClient(app, follow_redirects=follow_redirects)
    resp = client.post(
        "/api/auth/login",
        json={"user_id": user_id, "password": auth._DEMO_PASSWORD},
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"冒烟脚本登录失败（{user_id}）→ {resp.status_code} {resp.text[:200]}")
    return client


def runtime_models():
    """需要清空的风控运行表（**单一来源**，各脚本不要再各抄一份）。

    刻意**不含 `risk_rule`** —— 41 条预置规则是 init_db 灌进去的「既定事实」，
    清掉它们所有场景都无从命中，而报错会表现为「规则没命中」，
    看起来像规则引擎坏了。
    """
    from app.models import (  # noqa: PLC0415
        RiskAssessment,
        RiskAuditLog,
        RiskCase,
        RiskChallenge,
        RiskEvent,
        RiskFeature,
        RiskList,
        RiskListArchive,
        RiskUserProfile,
    )

    return (
        RiskList, RiskListArchive, RiskAuditLog, RiskFeature, RiskAssessment,
        RiskCase, RiskChallenge, RiskEvent, RiskUserProfile,
    )


def reset_risk_tables(db=None) -> int:
    """清空风控运行表，返回清掉的模型数。

    保留业务表与 risk_rule。清完顺手失效名单缓存，否则 `check_lists`
    会拿旧缓存去判定，出现「数据库里删干净了但名单还命中」的假象。

    传入 `db` 时复用调用方的 session（不提交、不关连接）——
    脚本自己已经开了 session 的情况下，用同一个 session 删可以避免
    identity map 与外部的 DELETE 不一致。
    """
    from app import cache  # noqa: PLC0415

    models = runtime_models()
    if db is not None:
        for model in models:
            db.query(model).delete()
        cache.invalidate_list()
        return len(models)

    from app.database import SessionLocal  # noqa: PLC0415

    own = SessionLocal()
    try:
        for model in models:
            own.query(model).delete()
        own.commit()
    finally:
        own.close()
    cache.invalidate_list()
    return len(models)


# 9 个端到端场景依赖「近 1 小时 / 近 24 小时」这类相对时间窗，
# 数据放久了就会滑出窗口 —— 判新标准取最早过期的那条：L001 需要
# 「1002 近 1 小时失败登录 >= 5 次」。
_FRESHNESS_SQL = (
    "SELECT COUNT(*) AS c FROM user_login_record "
    "WHERE user_id = '1002' AND login_result = '失败' "
    "AND login_time >= NOW() - INTERVAL 1 HOUR"
)


def scenario_data_age_check() -> int:
    """返回「1002 近 1 小时失败登录数」，用于判断场景数据是否还新鲜"""
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        row = db.execute(text(_FRESHNESS_SQL)).first()
        return int(row.c) if row and row.c is not None else 0
    finally:
        db.close()


def refresh_scenario_data(quiet: bool = True) -> None:
    """重建场景依赖的业务数据（登录 / 领券 / 物流投诉）。

    复用 init_db 第 6 步的同一个生成器，不另写一套 —— 它是幂等的
    （每次先 DELETE 这三张业务表，再按 `datetime.now()` 重灌），
    所以「发现数据过期自动重建」和「手动跑 init_db」得到的是同一份数据，
    不会随时间漂移出两套口径。

    只重灌这三张业务表，不动 41 条规则、不动其余业务表。
    """
    from scripts.gen_extra_data import main as gen_main  # noqa: PLC0415

    buf = io.StringIO()
    with redirect_stdout(buf):
        gen_main()
    if not quiet:
        sys.stdout.write(buf.getvalue())

    from app import cache  # noqa: PLC0415

    cache.safe_delete_prefix("risk:")   # 数据重灌后必须清缓存，否则旧特征污染新数据


def ensure_scenario_data(auto_refresh: bool = True) -> bool:
    """确保场景数据新鲜；过期则自动重建。返回最终是否新鲜。

    这是 `smoke_decision` 之前硬退出的那个检查的替代版：
    原来是「过期 → 打印请先跑 init_db → exit(2)」，
    现在是「过期 → 用同一个生成器自动重建 → 再判一次」。
    自愈比甩锅给使用者更省事，也让脚本可以任意顺序、任意次数重复跑。
    """
    if scenario_data_age_check() >= 5:
        return True
    if not auto_refresh:
        return False
    print(f"  · 场景数据已滑出时间窗（1002 近 1 小时失败登录 "
          f"{scenario_data_age_check()} 次，需要 >= 5），自动重建…")
    refresh_scenario_data()
    return scenario_data_age_check() >= 5
