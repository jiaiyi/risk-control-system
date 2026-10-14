# -*- coding: utf-8 -*-
"""画像服务 —— 用户风险画像查询 / 评分趋势 / 运维重算

职责边界（分层约束 SPEC NFR-16）：
    画像的**写入**留在决策引擎 `decision._upsert_profile()` —— 它必须与
    `risk_assessment` 在同一事务里，属决策链路的一部分；引擎层不得反向依赖服务层。
    本模块只做**查询**与**运维重算**。重算复用引擎层的纯函数
    （`profile_alpha` / `blend_profile_score`），依赖方向仍是 服务层 → 引擎层。

画像评分口径（PRD FR-43）：
    new_score = round(α × 本次评分 + (1-α) × 历史评分)
    α 按用户活跃度分档：近 30 天订单数 ≥5 → 0.7 ／ 1~4 → 0.6 ／ =0 → 0.4
"""
import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.engine.decision import blend_profile_score, profile_alpha, score_to_level
from app.models import RiskAssessment, RiskUserProfile
from app.service import list_service

logger = logging.getLogger(__name__)


class ProfileServiceError(Exception):
    code = "BIZ_PROFILE_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ProfileNotFoundError(ProfileServiceError):
    code = "BIZ_PROFILE_NOT_FOUND"
    http_status = 404


def _profile_payload(profile: RiskUserProfile) -> dict:
    extra = {}
    if profile.profile_data:
        try:
            extra = json.loads(profile.profile_data)
        except (json.JSONDecodeError, TypeError):
            extra = {}
    return {
        "user_id": profile.user_id,
        "risk_score": profile.risk_score,
        "risk_level": profile.risk_level,
        "total_orders": profile.total_orders,
        "total_refunds": profile.total_refunds,
        "refund_rate": float(profile.refund_rate) if profile.refund_rate is not None else 0,
        "avg_order_amount": float(profile.avg_order_amount) if profile.avg_order_amount is not None else 0,
        "address_count": profile.address_count,
        "complaint_count": profile.complaint_count,
        "assessment_count": profile.assessment_count,
        "last_assessment_time": (
            profile.last_assessment_time.isoformat(timespec="seconds")
            if profile.last_assessment_time else None
        ),
        # 最近一次衰减计算的细节（α / 历史分 / 本次分）
        "decay": {
            "alpha": extra.get("alpha"),
            "orders_30d": extra.get("orders_30d"),
            "history_score": extra.get("history_score"),
            "this_score": extra.get("this_score"),
            "veto": extra.get("veto"),
        } if extra else None,
        "update_time": profile.update_time.isoformat(timespec="seconds") if profile.update_time else None,
    }


def get_profile(db: Session, user_id: str, *, trend_limit: int = 10) -> dict:
    """画像详情（FR-25）：基础画像 + 评分趋势 + 名单履历"""
    profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
    if profile is None:
        raise ProfileNotFoundError(f"用户 {user_id} 尚无风险画像（未发生过风控评估）",
                                   {"user_id": user_id})

    payload = _profile_payload(profile)
    payload["trend"] = profile_trend(db, user_id, limit=trend_limit)
    payload["list_history"] = list_service.subject_history(db, "用户", user_id)
    return payload


def profile_trend(db: Session, user_id: str, limit: int = 20) -> list[dict]:
    """评分趋势：该用户最近 N 次评估（按时间降序返回）"""
    rows = (db.query(RiskAssessment)
            .filter(RiskAssessment.user_id == user_id)
            .order_by(RiskAssessment.create_time.desc())
            .limit(limit).all())
    return [{
        "assessment_id": r.assessment_id,
        "final_score": r.final_score,
        "risk_level": r.risk_level,
        "decision": r.decision,
        "rule_count": r.rule_count,
        "create_time": r.create_time.isoformat(timespec="seconds") if r.create_time else None,
    } for r in rows]


def rebuild_profile(db: Session, user_id: str, *, orders_30d: float | None = None) -> dict:
    """按历史评估序列**重放动态衰减**，重算画像评分（运维任务用）

    ⚠️ 已知近似：α 依赖「当时的近 30 天订单数」，历史时刻的这个值无法回溯，
    因此统一使用**当前** α。重放结果与决策时的逐次累加可能有细微差异，
    用于发现「画像被异常写入搞脏」的情况，不用于替代实时链路。
    """
    if orders_30d is None:
        profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
        extra = {}
        if profile and profile.profile_data:
            try:
                extra = json.loads(profile.profile_data)
            except (json.JSONDecodeError, TypeError):
                extra = {}
        orders_30d = extra.get("orders_30d", 0)

    alpha = profile_alpha(orders_30d)

    rows = (db.query(RiskAssessment)
            .filter(RiskAssessment.user_id == user_id)
            .order_by(RiskAssessment.create_time.asc(), RiskAssessment.assessment_id.asc())
            .all())
    if not rows:
        raise ProfileNotFoundError(f"用户 {user_id} 无历史评估记录，无法重算",
                                   {"user_id": user_id})

    score: int | None = None
    for r in rows:
        score = blend_profile_score(r.final_score, score, alpha)

    profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
    old_score = profile.risk_score if profile else None
    if profile is None:
        profile = RiskUserProfile(user_id=user_id, assessment_count=0)
        db.add(profile)

    profile.risk_score = score
    profile.risk_level = score_to_level(score)
    profile.assessment_count = len(rows)
    profile.profile_data = json.dumps(
        {"alpha": alpha, "orders_30d": orders_30d, "rebuilt": True,
         "this_score": rows[-1].final_score},
        ensure_ascii=False,
    )
    db.commit()

    logger.info("画像重算: user=%s %s → %s（重放 %d 次评估，α=%.1f）",
                user_id, old_score, score, len(rows), alpha)
    return {
        "user_id": user_id,
        "replayed": len(rows),
        "alpha": alpha,
        "orders_30d": orders_30d,
        "old_score": old_score,
        "new_score": score,
        "changed": old_score != score,
    }


def list_profiles(
    db: Session,
    *,
    risk_level: str | None = None,
    min_score: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskUserProfile]]:
    """画像列表（按风险分降序），返回 (总数, 当前页)"""
    q = db.query(RiskUserProfile)
    if risk_level:
        q = q.filter(RiskUserProfile.risk_level == risk_level)
    if min_score is not None:
        q = q.filter(RiskUserProfile.risk_score >= min_score)

    total = q.count()
    items = (q.order_by(RiskUserProfile.risk_score.desc(), RiskUserProfile.user_id.asc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


def statistics(db: Session) -> dict:
    """画像分布统计（供仪表盘「风险分布」使用）"""
    by_level = dict(db.execute(text(
        "SELECT risk_level, COUNT(*) AS c FROM risk_user_profile GROUP BY risk_level"
    )).fetchall())
    total = db.execute(text("SELECT COUNT(*) AS c FROM risk_user_profile")).first()
    high = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_user_profile WHERE risk_score >= 60")).first()
    return {
        "total": total.c if total else 0,
        "high_risk": high.c if high else 0,
        "by_level": by_level,
    }
