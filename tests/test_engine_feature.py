# -*- coding: utf-8 -*-
"""特征引擎单测 —— `app/engine/feature.py`

覆盖：**AC-08**（特征快照条数）、**AC-21/22**（缓存生效与降级）、
**NFR-05**（订单/商品维度禁止缓存）、**SPEC Q6**（entity_type 用字符串 + 前缀映射）。

> 说明：PRD 的 AC-08 仍写着「恰好 37 项（用户 14 / 订单 8 / 地址 3 / 登录 6 / 领券 6）」，
> 那是补齐 G-14/G-15/G-19 之前的旧口径。本项目实际实现 **43 项**，见
> `feature.py::EXPECTED_FEATURE_COUNT`，维度分布与 README「核心指标」一致。
"""
import pytest

from app.engine.feature import (
    EXPECTED_FEATURE_COUNT,
    PREFIX_ENTITY,
    compute_all_features,
    entity_type_of,
)

# 维度前缀 → 特征个数（README「核心指标」口径）
FEATURES_BY_PREFIX = {
    "user_": 14,
    "order_": 8,
    "addr_": 4,
    "login_": 4,
    "coupon_": 6,
    "device_": 2,
    "ip_": 2,
    "sku_": 2,
    "phone_": 1,
}


def _redis():
    from app import cache

    if not cache.is_available():
        pytest.skip("Redis 不可用 —— 缓存相关用例跳过（降级路径由 AC-22 用例覆盖）")
    return cache.get_client()


def _feature_keys(client) -> set[str]:
    return {k.decode() if isinstance(k, bytes) else str(k)
            for k in client.scan_iter("risk:feature:*")}


def _any_order_id(db) -> str | None:
    from sqlalchemy import text

    row = db.execute(text("SELECT order_id FROM order_info LIMIT 1")).first()
    return row[0] if row else None


# ============================================================
# 一、特征数量与维度分布
# ============================================================
def test_feature_count_matches_constant(db):
    """43 个特征 —— 常量与实际计算结果必须一致（两者漂移说明有一处漏改）"""
    features = compute_all_features(db, user_id="1002")
    assert len(features) == EXPECTED_FEATURE_COUNT == 43


@pytest.mark.parametrize(("prefix", "expected"), sorted(FEATURES_BY_PREFIX.items()))
def test_feature_distribution_by_dimension(db, prefix, expected):
    features = compute_all_features(db, user_id="1002")
    got = sum(1 for name in features if name.startswith(prefix))
    assert got == expected, f"{prefix}* 维度应有 {expected} 个特征，实际 {got}"


def test_all_feature_values_are_numeric(db):
    """特征统一返回 dict[str, float]（SPEC 4.3）—— 规则求值会直接做数值比较"""
    features = compute_all_features(db, user_id="1002")
    for name, value in features.items():
        assert isinstance(value, (int, float)), f"{name} 不是数值：{value!r}"


def test_feature_snapshot_row_count(db):
    """AC-08：一次检查落库的特征快照条数 = 特征总数（且引擎与快照不会各算一套）"""
    from app.engine.decision import run_risk_check
    from app.models import RiskFeature

    out = run_risk_check(
        db, user_id="1002", event_type="登录", source_id="PYTEST-FEAT",
        device_id="D-9002", login_ip="203.0.113.7",
    )
    rows = db.query(RiskFeature).filter(RiskFeature.event_id == out.event_id).count()
    assert rows == EXPECTED_FEATURE_COUNT


# ============================================================
# 二、前哨：实体类型映射（SPEC Q6 —— 字符串 + 应用层字典）
# ============================================================
@pytest.mark.parametrize(
    ("feature", "entity"),
    [
        ("user_total_orders", "用户"),
        ("order_total_amount", "订单"),
        ("sku_buy_count_24h", "订单"),
        ("addr_account_count", "地址"),
        ("login_fail_count_1h", "登录"),
        ("coupon_count_1h", "领券"),
        ("device_account_count_24h", "设备"),
        ("ip_account_count_24h", "IP"),
        ("phone_refund_rate_24h", "手机号"),
    ],
)
def test_entity_type_mapping(feature, entity):
    assert entity_type_of(feature) == entity


def test_unknown_prefix_falls_back_to_user():
    """未匹配前缀不能抛异常 —— 新增特征时忘登记只会归到「用户」，不会中断链路"""
    assert entity_type_of("brand_new_feature") == "用户"


def test_prefix_map_covers_all_dimensions():
    """前缀表必须覆盖全部 9 个维度（新增维度只需在此字典加一行，SPEC Q6）"""
    assert set(PREFIX_ENTITY) == set(FEATURES_BY_PREFIX)
    assert set(PREFIX_ENTITY.values()) == {"用户", "订单", "地址", "登录", "领券", "设备", "IP", "手机号"}


# ============================================================
# 三、缓存行为（AC-21 / NFR-05）
# ============================================================
def test_cacheable_dimensions_are_written(db):
    """AC-21：用户 / 地址 / 登录 / 领券 等维度命中缓存后应留下缓存键"""
    from app import cache

    cache.safe_delete_prefix("risk:feature:")
    compute_all_features(db, user_id="1002")
    keys = _feature_keys(_redis())

    for prefix in ("risk:feature:user:", "risk:feature:login:", "risk:feature:addr:",
                   "risk:feature:coupon:"):
        assert any(k.startswith(prefix) for k in keys), f"缺少 {prefix}* 缓存键"


def test_order_and_sku_dimensions_are_never_cached(db):
    """NFR-05：订单维度与商品维度**禁止缓存**（含本次订单实时金额）"""
    from app import cache

    order_id = _any_order_id(db)
    assert order_id, "业务数据缺少订单，无法验证订单维度缓存行为"

    cache.safe_delete_prefix("risk:feature:")
    compute_all_features(db, user_id="1002", order_id=order_id)
    keys = _feature_keys(_redis())

    assert not any(k.startswith("risk:feature:order:") for k in keys), \
        "订单维度特征被写进了缓存（违反 NFR-05）"
    assert not any(k.startswith("risk:feature:sku:") for k in keys), \
        "商品维度特征被写进了缓存（违反 NFR-05）"


def test_cached_result_equals_recomputed_result(db):
    """AC-21：第二次读缓存与首次直算结果完全一致（缓存只加速读、不改变结果）"""
    from app import cache

    cache.safe_delete_prefix("risk:feature:")
    first = compute_all_features(db, user_id="1002")
    second = compute_all_features(db, user_id="1002")

    assert first == second


def test_second_call_does_not_hit_database(db, monkeypatch):
    """AC-21 的强断言：命中缓存后**不应再执行**用户维度 SQL。

    用 `Event.listen` 数 SQL 条数 —— 只看「两次结果相同」是不够的，
    结果相同也可能是每次都直查数据库（缓存根本没生效）。
    """
    from sqlalchemy import event

    from app import cache

    cache.safe_delete_prefix("risk:feature:")
    compute_all_features(db, user_id="1002")           # 首次：回源并回填

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", _record)
    try:
        compute_all_features(db, user_id="1002")       # 二次：应全部命中缓存
    finally:
        event.remove(db.bind, "before_cursor_execute", _record)

    touched_user = [s for s in statements if "user_info" in s or "order_info" in s]
    assert not touched_user, f"二次调用仍在查数据库：{touched_user[:2]}"


def test_write_cache_false_leaves_no_keys(db):
    """AC-30 的一半：仿真要求零副作用，`write_cache=False` 时不得写入任何特征缓存"""
    from app import cache

    cache.safe_delete_prefix("risk:feature:")
    compute_all_features(db, user_id="1002", write_cache=False)
    assert _feature_keys(_redis()) == set(), "write_cache=False 仍写入了缓存键"
