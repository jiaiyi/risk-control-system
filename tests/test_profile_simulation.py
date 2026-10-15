# -*- coding: utf-8 -*-
"""画像服务 + 仿真服务单测

覆盖：**AC-24**（画像动态衰减的重放一致性）、**AC-30**（仿真回放零副作用）、
**FR-43**（评估后画像 upsert）、**FR-14**（分步链路与批量回放）。
"""
import pytest

from app.engine.decision import blend_profile_score
from app.service import event as event_svc
from app.service import profile_service
from app.service import simulation_service as sim


# ============================================================
# 一、画像（FR-43 / AC-24）
# ============================================================
def test_profile_not_found(db):
    with pytest.raises(profile_service.ProfileNotFoundError):
        profile_service.get_profile(db, "T-NO-PROFILE")


def test_profile_created_and_counted_after_check(db):
    """一次风控检查后，画像被 upsert，评估次数累加"""
    event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-P1",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    profile = profile_service.get_profile(db, "1003")

    assert profile["user_id"] == "1003"
    assert profile["assessment_count"] >= 1
    assert 0 <= profile["risk_score"] <= 100
    assert profile["risk_level"] in ("低", "中", "高", "极高")


def test_profile_score_is_blended_between_history_and_current(db):
    """AC-24 的端到端形态：画像分必须落在「本次评分」与「上次画像分」之间"""
    user = "1005"
    event_svc.process_event(db, user_id=user, event_type="营销领券",
                            source_id="PYTEST-B1", device_id="D-8005",
                            login_ip="172.16.0.20")
    first = profile_service.get_profile(db, user)["risk_score"]

    out2 = event_svc.process_event(db, user_id=user, event_type="营销领券",
                                   source_id="PYTEST-B2", device_id="D-8005",
                                   login_ip="172.16.0.20")
    second = profile_service.get_profile(db, user)["risk_score"]

    lo, hi = sorted((first, out2.final_score))
    assert lo <= second <= hi, f"画像分 {second} 未落在 [{lo}, {hi}] 区间内"


def test_rebuild_profile_replays_decay_formula(db):
    """`rebuild_profile` 的重放结果必须与衰减公式逐步套用的结果一致

    这是 AC-24 的**数值级**校验：不走近似，直接把公式算一遍对答案。
    """
    user = "1005"
    o1 = event_svc.process_event(db, user_id=user, event_type="营销领券",
                                 source_id="PYTEST-R1", device_id="D-8005",
                                 login_ip="172.16.0.20")
    o2 = event_svc.process_event(db, user_id=user, event_type="营销领券",
                                 source_id="PYTEST-R2", device_id="D-8005",
                                 login_ip="172.16.0.20")

    result = profile_service.rebuild_profile(db, user, orders_30d=0)   # 强制低活跃 α=0.4

    assert result["alpha"] == 0.4
    assert result["replayed"] == 2
    expected = blend_profile_score(o2.final_score, o1.final_score, 0.4)
    assert result["new_score"] == expected

    # 首次评估直接取本次分：单独验一遍
    assert blend_profile_score(o2.final_score, None, 0.4) == o2.final_score


def test_rebuild_profile_without_history_raises(db):
    with pytest.raises(profile_service.ProfileNotFoundError):
        profile_service.rebuild_profile(db, "T-NO-HISTORY")


def test_profile_trend_lists_assessments(db):
    user = "1005"
    event_svc.process_event(db, user_id=user, event_type="营销领券",
                            source_id="PYTEST-T1", device_id="D-8005",
                            login_ip="172.16.0.20")

    trend = profile_service.profile_trend(db, user, limit=10)
    assert isinstance(trend, list)
    assert trend, "应有评估趋势点"


def test_profile_statistics_shape(db):
    event_svc.process_event(db, user_id="1003", event_type="登录",
                            source_id="PYTEST-ST", device_id="D-SHARED-0001",
                            login_ip="172.16.0.10")
    stats = profile_service.statistics(db)
    assert isinstance(stats, dict)
    assert stats


# ============================================================
# 二、仿真（AC-30 —— 零副作用是红线）
# ============================================================
def test_run_single_has_zero_side_effects(db):
    """AC-30：仿真回放不写 risk_event / risk_feature / risk_case 等表"""
    before = sim._snapshot_counts(db)

    result = sim.run_single(
        db, user_id="1003", event_type="登录",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )

    after = sim._snapshot_counts(db)

    assert after == before, f"仿真产生了写入：{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }"
    assert result["steps"], "应返回逐步判定链路"


def test_run_single_returns_step_by_step_trace(db):
    """FR-14：单步链路要能逐条看到规则命中情况与实际特征值"""
    result = sim.run_single(
        db, user_id="1003", event_type="登录",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )

    assert "features" in result
    assert len(result["features"]) == 43, "仿真也要给出完整特征快照"

    steps = result["steps"]
    for step in steps:
        assert "rule_id" in step
        assert "matched" in step, "每步要标明是否命中"
        # 没命中的规则也要带出所引用特征的实际取值，前端才能解释「为什么没命中」
        assert "actual_values" in step
    assert result["feature_count"] == 43
    assert result["side_effects"]["db_writes"] == 0


def test_simulation_does_not_write_feature_cache(db):
    """仿真连 Redis 特征缓存也不能写（`write_cache=False`）"""
    from app import cache

    if not cache.is_available():
        pytest.skip("Redis 不可用")

    cache.safe_delete_prefix("risk:feature:")
    sim.run_single(db, user_id="1003", event_type="登录",
                   device_id="D-SHARED-0001", login_ip="172.16.0.10")

    keys = {k.decode() if isinstance(k, bytes) else str(k)
            for k in cache.get_client().scan_iter("risk:feature:*")}
    assert keys == set(), f"仿真写入了特征缓存：{list(keys)[:3]}"


def test_run_single_invalid_event_type(db):
    with pytest.raises(sim.SimulationServiceError):
        sim.run_single(db, user_id="1003", event_type="不存在的类型")


def test_run_single_accepts_all_six_event_types(db):
    for event_type in ("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉"):
        result = sim.run_single(db, user_id="1003", event_type=event_type,
                                device_id="D-SHARED-0001", login_ip="172.16.0.10")
        assert result["steps"], f"{event_type} 未返回判定链路"


def test_replay_with_explicit_samples_has_zero_side_effects(db):
    """批量回放同样零副作用（FR-14 能力 3）"""
    before = sim._snapshot_counts(db)

    result = sim.replay(db, samples=[
        {"user_id": "1003", "event_type": "登录", "source_id": "PYTEST-RP1"},
        {"user_id": "1005", "event_type": "营销领券", "source_id": "PYTEST-RP2"},
    ])

    after = sim._snapshot_counts(db)

    assert after == before, "批量回放产生了写入"
    assert result["sample_count"] == 2
    assert result["zero_side_effect"] is True


def test_replay_reports_rule_stats(db):
    """回放要输出规则命中统计，才能用于「改规则前后对比」"""
    event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-SAMPLE",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )

    result = sim.replay(db, limit=5)

    assert "rule_stats" in result or "rule_hits" in result


def test_load_samples_returns_history(db):
    """样本库来自历史案件（F-05 / G-27 的历史作弊样本）"""
    event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-LOAD",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )

    samples = sim.load_samples(db, limit=5)
    assert isinstance(samples, list)
    assert samples, "历史案件应能作为回放样本"
    assert "user_id" in samples[0] and "event_type" in samples[0]
