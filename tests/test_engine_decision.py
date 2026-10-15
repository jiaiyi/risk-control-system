# -*- coding: utf-8 -*-
"""决策引擎单测 —— `app/engine/decision.py`

覆盖：**AC-01/02/03**（评分链路与一票否决）、**AC-10**（高风险自动建案）、
**AC-24**（画像动态衰减）、**AC-33**（灰名单加严）、**AC-37**（二次验证档），
以及 SPEC 11.1 **Q5**（评分顺序）与 **Q7**（加严不改变否决语义）两条已确认决策。

纯函数部分（评分/等级/决策/衰减）不碰数据库；端到端部分用真实业务数据构造已知场景。
"""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.engine.decision import (
    blend_profile_score,
    compute_score,
    profile_alpha,
    run_risk_check,
    score_to_decision,
    score_to_level,
)
from app.engine.rule import RuleHitResult


def _hit(score: int, level: str = "高", action: str = "人工审核",
         rule_id: str = "T999") -> RuleHitResult:
    """构造命中结果 —— 评分链路只读这几个属性，用 SimpleNamespace 足够

    （比造一个不入库的 RiskRule 更直白：SQLAlchemy 对象容易让人误以为需要会话）
    """
    rule = SimpleNamespace(
        rule_id=rule_id, rule_name=rule_id, rule_category="测试场景",
        risk_level=level, risk_score=score, action=action,
        description="", priority=1,
    )
    return RuleHitResult(rule)


# ============================================================
# 一、评分链路（顺序固定：max → 多规则加分 → 灰名单加严 → 否决 → 截断）
# ============================================================
def test_no_hit_scores_zero():
    b = compute_score([])
    assert b.final_score == 0
    assert b.max_rule_score == 0
    assert b.multi_rule_bonus == 0
    assert b.veto is False


def test_single_hit_is_its_own_score():
    b = compute_score([_hit(60)])
    assert b.final_score == 60
    assert b.multi_rule_bonus == 0, "单条命中不叠加多规则加分"


def test_multi_rule_bonus_matches_ac02():
    """AC-02：评分 = max(各分值) + 3×(命中数-1)，且 ≤100"""
    b = compute_score([_hit(60), _hit(50), _hit(40)])
    assert b.max_rule_score == 60
    assert b.multi_rule_bonus == settings.RISK_MULTI_RULE_BONUS * 2 == 6
    assert b.final_score == 66


def test_score_is_capped_at_100():
    """AC-02 的「且 ≤100」—— 高分多命中不能溢出边界"""
    b = compute_score([_hit(98) for _ in range(4)])
    assert b.final_score == 100, "98 + 3×3 = 107 → 必须截断到 100"


def test_veto_forces_minimum_score():
    """AC-03：命中「极高」等级规则 → 一票否决，最终评分 ≥90 且决策为拒绝"""
    b = compute_score([_hit(10, level="极高", action="拒绝")])
    assert b.veto is True
    assert b.final_score == settings.RISK_VETO_MIN_SCORE == 90
    assert score_to_decision(b.final_score, []) == "拒绝"


def test_veto_does_not_lower_a_higher_score():
    """否决是「抬到下限」而不是「截到下限」—— 已超过下限的分数不该被拉低"""
    b = compute_score([_hit(95, level="极高", action="拒绝")])
    assert b.veto is True
    assert b.final_score == 95


def test_graylist_bonus_matches_ac33():
    """AC-33：灰名单不直接拦截，最终评分 = min(正常评分 + 15, 100)"""
    b = compute_score([_hit(60)], graylist_hit=True)
    assert b.graylist_bonus == settings.RISK_GRAYLIST_BONUS == 15
    assert b.final_score == 75
    assert b.veto is False

    b = compute_score([_hit(95)], graylist_hit=True)
    assert b.final_score == 100, "95 + 15 = 110 → 截断 100"


def test_graylist_does_not_trigger_veto():
    """SPEC Q7：一票否决只由 risk_level='极高' 触发，加严不改变否决语义"""
    b = compute_score([_hit(50, level="高")], graylist_hit=True)
    assert b.veto is False
    assert b.final_score == 65, "50+15=65，不应被抬到 90"


def test_graylist_applies_before_veto():
    """SPEC Q5：顺序是「规则加分后、一票否决前」——
    否决取 max(加严后的分, 90)，而不是取 max(未加严的分, 90)
    """
    b = compute_score([_hit(70, level="极高", action="拒绝")], graylist_hit=True)
    assert b.veto is True
    assert b.final_score == 90, "70+15=85 → 未到 90 → 否决抬到 90"


# ============================================================
# 二、评分 → 等级 / 决策（PRD FR-04 四档）
# ============================================================
@pytest.mark.parametrize(
    ("score", "level"),
    [(0, "低"), (29, "低"), (30, "中"), (59, "中"),
     (60, "高"), (79, "高"), (80, "极高"), (100, "极高")],
)
def test_score_to_level_boundaries(score, level):
    assert score_to_level(score) == level


@pytest.mark.parametrize(
    ("score", "decision"),
    [(0, "通过"), (29, "通过"), (30, "标记"), (59, "标记"),
     (60, "人工审核"), (79, "人工审核"), (80, "拒绝"), (100, "拒绝")],
)
def test_score_to_decision_boundaries(score, decision):
    assert score_to_decision(score, []) == decision


def test_challenge_branch_in_review_band():
    """AC-37：60~79 档若命中规则动作是「二次验证」，决策输出二次验证而非人工审核"""
    assert score_to_decision(70, [_hit(70, action="二次验证")]) == "二次验证"
    assert score_to_decision(70, [_hit(70, action="人工审核")]) == "人工审核"


def test_challenge_branch_uses_top_priority_hit():
    """档位细分看 hits[0] —— 即 priority 降序后的首条（SPEC Q2 口径）"""
    hits = [_hit(70, action="人工审核", rule_id="P"), _hit(70, action="二次验证", rule_id="S")]
    assert score_to_decision(70, hits) == "人工审核"

    hits_rev = [_hit(70, action="二次验证", rule_id="S"), _hit(70, action="人工审核", rule_id="P")]
    assert score_to_decision(70, hits_rev) == "二次验证"


# ============================================================
# 三、画像动态衰减（PRD FR-43 / AC-24）
# ============================================================
@pytest.mark.parametrize(
    ("orders_30d", "alpha"),
    [(0, 0.4), (0.9, 0.4), (1, 0.6), (4, 0.6), (5, 0.7), (50, 0.7)],
)
def test_profile_alpha_bands(orders_30d, alpha):
    assert profile_alpha(orders_30d) == alpha


def test_profile_decay_matches_ac24():
    """AC-24 的两个期望值：高活跃 0.7×90+0.3×20=69；低活跃 0.4×90+0.6×20=48"""
    assert blend_profile_score(90, 20, 0.7) == 69
    assert blend_profile_score(90, 20, 0.4) == 48


def test_first_assessment_takes_current_score():
    assert blend_profile_score(90, None, 0.7) == 90


def test_higher_alpha_moves_faster():
    """高活跃用户的画像更贴近本次评分（衰减更弱）—— 这条是 α 分档的存在意义"""
    high = blend_profile_score(90, 20, 0.7)
    low = blend_profile_score(90, 20, 0.4)
    assert high > low


# ============================================================
# 四、端到端：真实数据下的已知场景（含落库）
# ============================================================
# (user_id, device_id, login_ip, event_type, 期望命中规则)
KNOWN_SCENARIOS = [
    ("1002", "D-9002", "203.0.113.7", "登录", ["L001"]),          # 撞库试登
    ("1003", "D-SHARED-0001", "172.16.0.10", "登录", ["L002"]),   # 同设备养号
    ("1006", "D-7700", "10.99.99.99", "登录", ["L003", "A004"]),  # 同 IP 批量
    ("1003", "D-8003", "172.16.0.20", "营销领券", ["C001"]),       # 短时批量领券
    ("1006", "D-SHARED-0002", "172.16.0.20", "营销领券", ["C002"]),  # 同设备多账号领券
]


@pytest.mark.parametrize(
    ("user_id", "device_id", "login_ip", "event_type", "expected"),
    KNOWN_SCENARIOS,
)
def test_known_scenario_hits_expected_rules(db, user_id, device_id, login_ip,
                                            event_type, expected):
    out = run_risk_check(
        db, user_id=user_id, event_type=event_type,
        source_id=f"PYTEST-{user_id}-{event_type}",
        event_data={"device_id": device_id, "login_ip": login_ip},
        device_id=device_id, login_ip=login_ip,
    )
    hit_ids = [h.rule_id for h in out.hits]
    for rule_id in expected:
        assert rule_id in hit_ids, f"期望命中 {rule_id}，实际命中 {hit_ids}"

    assert 0 <= out.final_score <= 100
    assert out.rule_count == len(out.hits)
    assert out.decision in ("通过", "标记", "人工审核", "二次验证", "拒绝")


def test_veto_scenario_scores_at_least_90(db):
    """含「极高」规则的场景必须触发否决并落到拒绝档（AC-03 的端到端形态）

    样本取自 scripts/smoke_decision.py 的场景②（新设备异地登录）—— 该场景命中的
    规则里含 `risk_level='极高'` 的一条，是「一票否决」在真实数据上的可复现形态。
    """
    out = run_risk_check(
        db, user_id="1002", event_type="登录",
        source_id="PYTEST-VETO",
        device_id="D-NEW-8888", login_ip="203.0.113.7",
    )
    assert out.breakdown.veto, f"场景应触发一票否决，命中 {[h.rule_id for h in out.hits]}"
    assert out.final_score >= settings.RISK_VETO_MIN_SCORE
    assert out.decision == "拒绝"


def test_high_risk_creates_case(db):
    """AC-10：决策为人工审核或拒绝 → 自动创建案件，状态=待审核，且带原始业务 ID 与事件类型"""
    from app.models import RiskCase

    out = run_risk_check(
        db, user_id="1003", event_type="登录",
        source_id="PYTEST-CASE-TRIGGER",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    assert out.decision in ("人工审核", "拒绝"), "该场景应落进需建案的档位"
    assert out.case_id, "高风险必须自动建案"

    case = db.query(RiskCase).filter(RiskCase.case_id == out.case_id).first()
    assert case is not None
    assert case.case_status == "待审核"
    assert case.user_id == "1003"
    assert case.source_id == "PYTEST-CASE-TRIGGER"
    assert case.event_type == "登录"
    assert case.assessment_id == out.assessment_id


def test_repeated_check_updates_profile_with_decay(db):
    """连续评估同一用户：画像走加权衰减，且评估次数累加（FR-43）"""
    from app.models import RiskUserProfile

    user = "1005"
    out1 = run_risk_check(db, user_id=user, event_type="营销领券",
                          source_id="PYTEST-PROFILE-1",
                          device_id="D-8005", login_ip="172.16.0.20")
    p1 = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user).first()
    assert p1 is not None
    assert p1.assessment_count >= 1

    out2 = run_risk_check(db, user_id=user, event_type="营销领券",
                          source_id="PYTEST-PROFILE-2",
                          device_id="D-8005", login_ip="172.16.0.20")
    db.expire_all()
    p2 = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user).first()
    assert p2.assessment_count >= 2

    # 衰减公式：画像分必须落在「历史分」与「本次分」之间（含端点）
    lo, hi = sorted((out1.final_score, out2.final_score))
    assert lo <= p2.risk_score <= max(hi, settings.RISK_VETO_MIN_SCORE)
