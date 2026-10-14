# -*- coding: utf-8 -*-
"""决策引擎 —— 串联「特征 → 规则 → 评分 → 决策 → 持久化」。

评分链路（SPEC 11.1 Q5 已定，顺序不可调换）：
    ① max_rule_score = max(命中规则分值)
    ② + RISK_MULTI_RULE_BONUS × (命中规则数 - 1)
    ③ + RISK_GRAYLIST_BONUS（仅当命中灰名单）
    ④ 一票否决：仅由 rule.risk_level == "极高" 触发 → score = max(score, RISK_VETO_MIN_SCORE)
    ⑤ score = min(score, 100)

    ③ 插在「加分后、一票否决前」；④ 只由规则等级触发，不受灰名单加严影响（SPEC Q7：
    一票否决的语义是「命中极高危规则」，灰名单加严是「对可疑主体从严」，两者不可混用）。

决策映射（PRD FR-04）：
    0 ~ 29    低      通过         放行交易
    30 ~ 59   中      标记         记录但放行
    60 ~ 79   高      人工审核 / 二次验证
                                  —— 命中规则动作为「二次验证」时取后者，否则人工审核
    80 ~ 100  极高    拒绝         自动创建案件并阻止交易

用户画像（PRD FR-43）：
    new_score = round(α × 本次评分 + (1 - α) × 历史评分)
    α 按近 30 天订单数分档：≥5 → 0.7；1~4 → 0.6；=0 → 0.4

持久化（单事务）：
    risk_event / risk_feature(N) / risk_assessment /
    risk_case(条件：人工审核、拒绝) / risk_challenge(条件：二次验证) /
    risk_user_profile(upsert)

分层约束（SPEC NFR-16）：引擎层不 import 接口层（app.schemas）。
    入参为关键字参数，出参为 DecisionOutcome 数据类，由调用方自行转换为响应模型。
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import ulid
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.engine.feature import compute_all_features, entity_type_of
from app.engine.rule import RuleHitResult, load_enabled_rules, match_rules
from app.models import (
    RiskAssessment,
    RiskCase,
    RiskChallenge,
    RiskEvent,
    RiskFeature,
    RiskUserProfile,
)

logger = logging.getLogger(__name__)


# ============================================================
# 实体类型 → 主体值取自哪个上下文参数（SPEC Q6：字符串 + 映射字典）
# ============================================================
ENTITY_ID_SOURCE = {
    "用户": "user_id",
    "订单": "order_id",
    "地址": "receive_id",
    "登录": "user_id",
    "领券": "user_id",
    "设备": "device_id",
    "IP": "login_ip",
    "手机号": "receive_id",
}


def generate_id(prefix: str) -> str:
    """生成带前缀的有序唯一 ID（ULID 按时间单调递增，比 UUID 更适合做审计主键）"""
    return f"{prefix}{ulid.new().str.lower()}"


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ============================================================
# 上下文补全
# ============================================================
def resolve_context(
    db: Session,
    *,
    user_id: str,
    event_type: str,
    source_id: str | None,
    order_id: str | None = None,
    receive_id: str | None = None,
    device_id: str | None = None,
    login_ip: str | None = None,
) -> dict[str, str | None]:
    """补全特征计算所需的上下文（SPEC 11.1 Q1 决策）

    · 下单 / 支付事件未传 order_id → 取 source_id（业务单据即订单）
    · 有 order_id 但无 receive_id → 从 order_info 反查收货信息 ID
    · 未传 device_id / login_ip → 取该用户最近一次登录的设备与 IP
      （Q1：仍取不到则相关特征置 0，不参与聚集判定 —— 由特征引擎自行兜底）

    显式传入的值优先，不被反查结果覆盖。
    """
    ctx: dict[str, str | None] = {
        "user_id": user_id,
        "order_id": order_id,
        "receive_id": receive_id,
        "device_id": device_id,
        "login_ip": login_ip,
    }

    if ctx["order_id"] is None and event_type in ("下单", "支付"):
        ctx["order_id"] = source_id

    if ctx["receive_id"] is None and ctx["order_id"]:
        row = db.execute(
            text("SELECT receive_id FROM order_info WHERE order_id = :oid"),
            {"oid": ctx["order_id"]},
        ).first()
        if row and row.receive_id:
            ctx["receive_id"] = row.receive_id

    if ctx["device_id"] is None or ctx["login_ip"] is None:
        row = db.execute(
            text(
                "SELECT device_id, login_ip FROM user_login_record "
                "WHERE user_id = :uid ORDER BY login_time DESC LIMIT 1"
            ),
            {"uid": user_id},
        ).first()
        if row:
            ctx["device_id"] = ctx["device_id"] or row.device_id
            ctx["login_ip"] = ctx["login_ip"] or row.login_ip

    return ctx


def _entity_id_for(entity_type: str, ctx: dict[str, str | None]) -> str:
    """按实体类型取主体值；取不到时回落到 user_id，保证快照行始终有主键值"""
    key = ENTITY_ID_SOURCE.get(entity_type, "user_id")
    return ctx.get(key) or ctx["user_id"]


# ============================================================
# 评分链路
# ============================================================
@dataclass
class ScoreBreakdown:
    """评分链路的逐步快照 —— 供仿真回放与案件详情解释使用"""

    max_rule_score: int = 0      # ①
    multi_rule_bonus: int = 0    # ②
    graylist_bonus: int = 0      # ③
    veto: bool = False           # ④
    final_score: int = 0         # ⑤

    @property
    def base_score(self) -> int:
        """加分后、加严与否决前的基准分（① + ②）"""
        return self.max_rule_score + self.multi_rule_bonus

    def to_dict(self) -> dict:
        return {
            "max_rule_score": self.max_rule_score,
            "multi_rule_bonus": self.multi_rule_bonus,
            "base_score": self.base_score,
            "graylist_bonus": self.graylist_bonus,
            "veto": self.veto,
            "final_score": self.final_score,
        }


def compute_score(hits: list[RuleHitResult], graylist_hit: bool = False) -> ScoreBreakdown:
    """按 SPEC 11.1 Q5 的固定顺序计算最终评分"""
    if not hits:
        return ScoreBreakdown()

    max_rule_score = max(h.risk_score for h in hits)
    multi_rule_bonus = settings.RISK_MULTI_RULE_BONUS * (len(hits) - 1)
    graylist_bonus = settings.RISK_GRAYLIST_BONUS if graylist_hit else 0
    veto = any(h.risk_level == "极高" for h in hits)

    score = max_rule_score + multi_rule_bonus + graylist_bonus
    if veto:
        score = max(score, settings.RISK_VETO_MIN_SCORE)
    score = min(score, 100)

    return ScoreBreakdown(
        max_rule_score=max_rule_score,
        multi_rule_bonus=multi_rule_bonus,
        graylist_bonus=graylist_bonus,
        veto=veto,
        final_score=score,
    )


def score_to_level(score: int) -> str:
    """评分 → 风险等级（PRD FR-04 决策映射表）"""
    if score < settings.RISK_PASS_THRESHOLD:
        return "低"
    elif score < settings.RISK_MARK_THRESHOLD:
        return "中"
    elif score < settings.RISK_REVIEW_THRESHOLD:
        return "高"
    return "极高"


def score_to_decision(score: int, hits: list[RuleHitResult]) -> str:
    """评分 → 决策

    第三档（60~79）细分依据 PRD FR-04：命中规则动作为「二次验证」时输出二次验证。
    与案件分类口径保持一致，都取按 priority 降序的 hits[0]（SPEC Q2）。
    """
    if score < settings.RISK_PASS_THRESHOLD:
        return "通过"
    elif score < settings.RISK_MARK_THRESHOLD:
        return "标记"
    elif score < settings.RISK_REVIEW_THRESHOLD:
        if hits and hits[0].action == "二次验证":
            return "二次验证"
        return "人工审核"
    return "拒绝"


# ============================================================
# 用户画像（PRD FR-43 动态衰减）
# ============================================================
def profile_alpha(orders_30d: float) -> float:
    """按近 30 天订单数判定活跃度分档，返回衰减系数 α"""
    if orders_30d >= settings.PROFILE_ACTIVE_THRESHOLD:
        return settings.PROFILE_ALPHA_HIGH
    if orders_30d >= 1:
        return settings.PROFILE_ALPHA_MID
    return settings.PROFILE_ALPHA_LOW


def blend_profile_score(current: int, history: int | None, alpha: float) -> int:
    """动态衰减加权；history 为 None 表示首次评估，直接采用本次评分"""
    if history is None:
        return current
    return round(alpha * current + (1 - alpha) * history)


# ============================================================
# 决策结果
# ============================================================
@dataclass
class DecisionOutcome:
    """一次风控检查的完整结果（含持久化产生的 ID）"""

    event_id: str
    assessment_id: str
    user_id: str
    event_type: str
    source_id: str | None
    final_score: int
    risk_level: str
    decision: str
    rule_count: int
    features: dict[str, float]
    breakdown: ScoreBreakdown
    graylist_hit: bool
    hits: list[RuleHitResult] = field(default_factory=list)
    context: dict[str, str | None] = field(default_factory=dict)
    case_id: str | None = None
    challenge_id: str | None = None

    def triggered_rules(self) -> list[dict]:
        return [h.to_dict() for h in self.hits]

    def to_dict(self) -> dict:
        return {
            "assessment_id": self.assessment_id,
            "event_id": self.event_id,
            "user_id": self.user_id,
            "event_type": self.event_type,
            "source_id": self.source_id,
            "final_score": self.final_score,
            "risk_level": self.risk_level,
            "decision": self.decision,
            "rule_count": self.rule_count,
            "triggered_rules": self.triggered_rules(),
            "features": self.features,
            "score_breakdown": self.breakdown.to_dict(),
            "graylist_hit": self.graylist_hit,
            "case_id": self.case_id,
            "challenge_id": self.challenge_id,
            "create_time": datetime.now().isoformat(timespec="seconds"),
        }


# ============================================================
# 持久化（全部在同一事务内完成）
# ============================================================
def _persist_event(
    db: Session, event_id: str, event_type: str, source_id: str | None,
    user_id: str, event_data: dict | None,
) -> RiskEvent:
    event = RiskEvent(
        event_id=event_id,
        event_type=event_type,
        event_source_id=source_id or "",
        user_id=user_id,
        event_data=_json(event_data or {}),
    )
    db.add(event)
    return event


def _persist_features(
    db: Session, event_id: str, features: dict[str, float], ctx: dict[str, str | None],
) -> int:
    """批量写特征快照

    SPEC NFR-01：用 bulk_save_objects 替代逐条 add —— 43 条特征减少 43 次往返。
    entity_type 由 feature.entity_type_of() 按前缀推断（SPEC Q6）。
    """
    rows = []
    for name, value in features.items():
        entity_type = entity_type_of(name)
        rows.append(
            RiskFeature(
                event_id=event_id,
                entity_type=entity_type,
                entity_id=_entity_id_for(entity_type, ctx),
                feature_name=name,
                feature_value=round(float(value), 4),
            )
        )
    db.bulk_save_objects(rows)
    return len(rows)


def _build_case(
    db: Session, outcome_hits: list[RuleHitResult], assessment_id: str,
    user_id: str, final_score: int, source_id: str | None, event_type: str,
    breakdown: ScoreBreakdown, graylist_hit: bool, conflict_flag: int = 0,
) -> RiskCase:
    """创建案件。分类取 hits[0].rule_category —— hits 已按 priority 降序（SPEC Q2）"""
    case = RiskCase(
        case_id=generate_id("cas"),
        assessment_id=assessment_id,
        user_id=user_id,
        case_status="待审核",
        case_category=outcome_hits[0].rule_category if outcome_hits else "未知",
        conflict_flag=conflict_flag,
        risk_detail=_json({
            "final_score": final_score,
            "score_breakdown": breakdown.to_dict(),
            "graylist_hit": graylist_hit,
            "rule_results": [h.to_dict() for h in outcome_hits],
        }),
        # 冗余存储原始业务 ID 与事件类型，支撑「重做检查」一键跳回
        source_id=source_id,
        event_type=event_type,
    )
    db.add(case)
    return case


def _build_challenge(
    db: Session, assessment_id: str, user_id: str,
) -> RiskChallenge:
    """创建二次验证记录

    模拟实现（PRD 10.4 已知取舍）：不接短信/验证码通道，仅落库并返回凭据占位。
    """
    challenge = RiskChallenge(
        challenge_id=generate_id("chl"),
        assessment_id=assessment_id,
        user_id=user_id,
        verify_token=generate_id("vt"),
        status="待验证",
        expire_time=datetime.now() + timedelta(minutes=settings.CHALLENGE_TIMEOUT_MINUTES),
    )
    db.add(challenge)
    return challenge


def _upsert_profile(
    db: Session, user_id: str, features: dict[str, float],
    final_score: int, breakdown: ScoreBreakdown,
) -> dict:
    """用户画像 upsert（PRD FR-43 动态衰减加权）

    返回本次的衰减计算细节，便于响应体回显与单测断言。
    """
    profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
    orders_30d = features.get("user_orders_30d", 0)
    alpha = profile_alpha(orders_30d)

    if profile is None:
        profile = RiskUserProfile(user_id=user_id, assessment_count=0)
        db.add(profile)
        history = None
    else:
        # assessment_count 为 0 视同无历史，避免早期脏数据把首次评估当成衰减
        history = profile.risk_score if profile.assessment_count else None

    blended = blend_profile_score(final_score, history, alpha)

    profile.risk_score = blended
    profile.risk_level = score_to_level(blended)
    profile.total_orders = int(features.get("user_total_orders", 0))
    profile.total_refunds = int(features.get("user_refund_count", 0))
    profile.refund_rate = round(float(features.get("user_refund_rate", 0)), 4)
    profile.avg_order_amount = round(float(features.get("user_avg_order_amount", 0)), 2)
    profile.address_count = int(features.get("user_address_count", 0))
    profile.complaint_count = int(features.get("user_complaint_count", 0))
    profile.assessment_count = (profile.assessment_count or 0) + 1
    profile.last_assessment_time = datetime.now()
    profile.profile_data = _json({
        "alpha": alpha,
        "orders_30d": orders_30d,
        "history_score": history,
        "this_score": final_score,
        "veto": breakdown.veto,
    })

    return {
        "alpha": alpha,
        "history_score": history,
        "profile_score": blended,
        "assessment_count": profile.assessment_count,
    }


# ============================================================
# 主流程
# ============================================================
def run_risk_check(
    db: Session,
    *,
    user_id: str,
    event_type: str,
    source_id: str | None = None,
    event_data: dict | None = None,
    order_id: str | None = None,
    receive_id: str | None = None,
    device_id: str | None = None,
    login_ip: str | None = None,
    graylist_hit: bool = False,
) -> DecisionOutcome:
    """执行一次完整风控检查并落库

    调用方（事件管道 T-11）负责名单前置裁决：命中黑/白名单时不进入本函数。
    仅灰名单命中时，通过 graylist_hit=True 传入以施加加严系数。

    全流程在**单事务**内完成；异常时由调用方回滚。
    """
    # ---- 0. 上下文补全 ----
    ctx = resolve_context(
        db,
        user_id=user_id,
        event_type=event_type,
        source_id=source_id,
        order_id=order_id,
        receive_id=receive_id,
        device_id=device_id,
        login_ip=login_ip,
    )

    event_id = generate_id("evt")
    assessment_id = generate_id("ast")

    # ---- 1. 事件留痕 ----
    _persist_event(db, event_id, event_type, source_id, user_id, event_data)

    # ---- 2. 特征计算（订单/商品维度强制实时，不受缓存影响） ----
    features = compute_all_features(
        db,
        user_id=user_id,
        order_id=ctx["order_id"],
        receive_id=ctx["receive_id"],
        device_id=ctx["device_id"],
        login_ip=ctx["login_ip"],
    )

    # ---- 3. 特征快照 ----
    feature_count = _persist_features(db, event_id, features, ctx)

    # ---- 4. 规则匹配 ----
    rules = load_enabled_rules(db, event_type=event_type)
    hits = match_rules(rules, features)

    # ---- 5. 评分与决策 ----
    breakdown = compute_score(hits, graylist_hit=graylist_hit)
    final_score = breakdown.final_score
    risk_level = score_to_level(final_score)
    decision = score_to_decision(final_score, hits)

    # ---- 6. 评估结果 ----
    db.add(RiskAssessment(
        assessment_id=assessment_id,
        event_id=event_id,
        user_id=user_id,
        rule_results=_json([h.to_dict() for h in hits]),
        rule_count=len(hits),
        final_score=final_score,
        risk_level=risk_level,
        decision=decision,
    ))

    # ---- 7. 案件 / 二次验证 ----
    case_id = None
    challenge_id = None
    if decision in ("人工审核", "拒绝"):
        case = _build_case(
            db, hits, assessment_id, user_id, final_score,
            source_id, event_type, breakdown, graylist_hit,
        )
        case_id = case.case_id
    elif decision == "二次验证":
        challenge = _build_challenge(db, assessment_id, user_id)
        challenge_id = challenge.challenge_id

    # ---- 8. 画像 upsert ----
    profile_info = _upsert_profile(db, user_id, features, final_score, breakdown)

    # ---- 9. 单事务提交 ----
    db.commit()

    logger.info(
        "风控检查完成: user=%s event=%s score=%d(%s) decision=%s rules=%d features=%d "
        "graylist=%s veto=%s case=%s challenge=%s profile=%d(α=%.1f)",
        user_id, event_type, final_score, risk_level, decision, len(hits), feature_count,
        graylist_hit, breakdown.veto, case_id, challenge_id,
        profile_info["profile_score"], profile_info["alpha"],
    )

    return DecisionOutcome(
        event_id=event_id,
        assessment_id=assessment_id,
        user_id=user_id,
        event_type=event_type,
        source_id=source_id,
        final_score=final_score,
        risk_level=risk_level,
        decision=decision,
        rule_count=len(hits),
        features=features,
        breakdown=breakdown,
        graylist_hit=graylist_hit,
        hits=hits,
        context=ctx,
        case_id=case_id,
        challenge_id=challenge_id,
    )
