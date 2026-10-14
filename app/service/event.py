# -*- coding: utf-8 -*-
"""事件管道 —— 风控检查的统一入口

职责（SPEC 3.2）：
    入参兜底 → 名单前置判定 → 分支裁决 → 调决策引擎 → 审计写入。
    **不实现评分算法**（评分归决策引擎）。

分支裁决（PRD FR-05 / FR-06 / FR-38，SPEC 7.1 时序）：
    ┌ 黑 + 白同时命中 → 人工审核 + 冲突案件（评分仍按正常流程计算）
    ├ 仅黑名单命中   → 拒绝，评分 100 / 等级 极高 —— **不做特征计算与规则匹配**
    ├ 仅白名单命中   → 通过，评分 0 / 等级 低 —— **不做特征计算与规则匹配**
    └ 仅灰名单 / 均未命中 → 交决策引擎走正常流程（灰名单命中时加严 +15）

合规约束：
    · AC-5：名单直接裁决路径**不产生特征快照**（0 条 risk_feature）
    · 每次检查都写 risk_event + risk_assessment，保证可追溯
    · 白名单命中与名单冲突必须落审计（PRD 4.6「滥用防护」/ FR-38 要求 1）

接口约定（SPEC 6.2 API-01）：
    `EventProcessResult.to_dict()` 直接对齐 API-01 的响应结构，
    接口层（T-17）无需再做字段拼装。
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.engine.decision import DecisionOutcome, generate_id, run_risk_check
from app.models import RiskAssessment, RiskAuditLog, RiskEvent
from app.service import audit_service, list_service
from app.service.list_service import ListServiceError, ListVerdict

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = ("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉")

# 名单直接裁决时固定的评分 / 等级（PRD FR-05 名单语义表）
SHORTCUT_SCORE = {"拒绝": 100, "通过": 0}
SHORTCUT_LEVEL = {"拒绝": "极高", "通过": "低"}


class EventServiceError(Exception):
    code = "BIZ_EVENT_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class UserNotFoundError(EventServiceError):
    code = "BIZ_USER_NOT_FOUND"
    http_status = 404


class EventTypeInvalidError(EventServiceError):
    code = "BIZ_EVENT_TYPE_INVALID"


# ============================================================
# 结果结构
# ============================================================
@dataclass
class EventProcessResult:
    """一次风控检查的最终结果"""

    assessment_id: str
    event_id: str
    user_id: str
    event_type: str
    source_id: str | None
    final_score: int
    risk_level: str
    decision: str
    rule_count: int
    list_verdict: ListVerdict
    conflict_flag: int = 0
    case_id: str | None = None
    challenge_id: str | None = None
    features: dict = field(default_factory=dict)
    triggered_rules: list = field(default_factory=list)
    list_shortcut: bool = False
    create_time: datetime = field(default_factory=datetime.now)

    def list_hit_payload(self) -> dict:
        """名单命中情况（API-01 新增字段，便于前端展示与排查）"""
        v = self.list_verdict
        if v.conflict:
            hit_type = "冲突"
        elif v.black:
            hit_type = "黑名单"
        elif v.white:
            hit_type = "白名单"
        elif v.gray:
            hit_type = "灰名单"
        else:
            hit_type = "无"
        return {
            "type": hit_type,
            "entities": [h.to_dict() for h in v.hits],
            "detail": v.describe(),
            "cache_hit": v.cache_hit,
        }

    def to_dict(self) -> dict:
        """对齐 SPEC 6.2 API-01 的响应结构"""
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
            "triggered_rules": self.triggered_rules,
            "features": self.features,
            "list_hit": self.list_hit_payload(),
            # 特征维度的缓存命中统计待特征引擎扩展返回值后补齐（S-11 的缓存命中率
            # 由 /api/health/metrics 统一体现）；此处先暴露名单判定的缓存情况。
            "cache_hit": {"list": self.list_verdict.cache_hit},
            "conflict_flag": self.conflict_flag,
            "case_id": self.case_id,
            "challenge_id": self.challenge_id,
            "create_time": self.create_time.isoformat(timespec="seconds"),
        }


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _audit(db: Session, *, action_desc: str, target_id, after: dict | None = None) -> None:
    """名单相关事件落审计（统一走 audit_service —— T-15 收编）"""
    audit_service.log(
        db, operator="risk_engine", action_type="名单",
        action_desc=action_desc, target_id=target_id, after=after,
    )


# ============================================================
# 入参兜底
# ============================================================
def ensure_user_exists(db: Session, user_id: str) -> None:
    """用户必须存在，否则整条链路无从取数（SPEC API-01：user_id 必须存在）"""
    if not user_id or not str(user_id).strip():
        raise UserNotFoundError("user_id 不能为空", {"user_id": user_id})
    row = db.execute(
        text("SELECT user_id FROM user_info WHERE user_id = :uid"),
        {"uid": user_id},
    ).first()
    if row is None:
        raise UserNotFoundError(f"用户不存在：{user_id}", {"user_id": user_id})


def _normalize_event_type(event_type: str | None) -> str:
    if event_type not in VALID_EVENT_TYPES:
        raise EventTypeInvalidError(
            f"事件类型非法：{event_type}（可选：{'/'.join(VALID_EVENT_TYPES)}）",
            {"event_type": event_type},
        )
    return event_type


# ============================================================
# 名单直接裁决分支
# ============================================================
def _short_circuit(
    db: Session,
    *,
    user_id: str,
    event_type: str,
    source_id: str | None,
    event_data: dict | None,
    verdict: ListVerdict,
    decision: str,
) -> EventProcessResult:
    """黑名单 / 白名单命中时的直接裁决

    · **不做特征计算与规则匹配**（PRD FR-05 名单语义表）
    · 按 AC-5 要求**不写 risk_feature**（0 条快照）
    · 仍写 risk_event + risk_assessment，保证每次检查可追溯
    · 名单直接裁决属短路路径，未做完整评估，因此**不更新用户画像**
    """
    event_id = generate_id("evt")
    assessment_id = generate_id("ast")
    final_score = SHORTCUT_SCORE[decision]
    risk_level = SHORTCUT_LEVEL[decision]

    list_detail = _json({
        "list_hit": {
            "type": "黑名单" if decision == "拒绝" else "白名单",
            "entities": [h.to_dict() for h in verdict.hits],
            "detail": verdict.describe(),
        },
        "shortcut": True,
    })

    db.add(RiskEvent(
        event_id=event_id,
        event_type=event_type,
        event_source_id=source_id or "",
        user_id=user_id,
        event_data=_json(event_data or {}),
    ))
    db.add(RiskAssessment(
        assessment_id=assessment_id,
        event_id=event_id,
        user_id=user_id,
        rule_results=list_detail,
        rule_count=0,
        final_score=final_score,
        risk_level=risk_level,
        decision=decision,
    ))

    if decision == "通过":
        # PRD 4.6「滥用防护」：白名单命中必须可查
        _audit(db, action_desc="白名单命中放行", target_id=assessment_id,
               after={"user_id": user_id, "source_id": source_id,
                      "entities": [h.to_dict() for h in verdict.white]})
    db.commit()

    logger.info("名单直接裁决: user=%s event=%s decision=%s score=%d（未做特征计算）",
                user_id, event_type, decision, final_score)

    return EventProcessResult(
        assessment_id=assessment_id,
        event_id=event_id,
        user_id=user_id,
        event_type=event_type,
        source_id=source_id,
        final_score=final_score,
        risk_level=risk_level,
        decision=decision,
        rule_count=0,
        list_verdict=verdict,
        list_shortcut=True,
    )


# ============================================================
# 主流程
# ============================================================
def process_event(
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
) -> EventProcessResult:
    """处理一次风控事件（API-01 的完整链路）"""
    # ---- 0. 入参兜底 ----
    ensure_user_exists(db, user_id)
    event_type = _normalize_event_type(event_type)

    event_data = event_data or {}
    device_id = device_id or event_data.get("device_id")
    login_ip = login_ip or event_data.get("login_ip")
    receive_id = receive_id or event_data.get("receive_id")

    # ---- 1. 名单前置判定（五类主体 × 三色）----
    subjects = list_service.collect_subjects(
        db, user_id=user_id, receive_id=receive_id,
        device_id=device_id, login_ip=login_ip,
    )
    verdict = list_service.check_lists(db, subjects)

    logger.info("名单前置判定: user=%s type=%s decision=%s",
                user_id, verdict.describe()[:32], verdict.decision)

    # ---- 2. 分支裁决 ----
    if verdict.conflict:
        # 黑 + 白同时命中：不做自动裁决，交人工处理；评分仍按正常流程计算（FR-38）
        out = run_risk_check(
            db, user_id=user_id, event_type=event_type, source_id=source_id,
            event_data=event_data, order_id=order_id, receive_id=receive_id,
            device_id=device_id, login_ip=login_ip,
            graylist_hit=False,
            force_decision="人工审核",
            conflict_flag=1,
        )
        _audit(db, action_desc="名单冲突移交人工审核", target_id=out.assessment_id,
               after={"user_id": user_id, "conflict": verdict.describe()})
        db.commit()
        return _from_decision(out, verdict)

    if verdict.decision in ("拒绝", "通过"):
        return _short_circuit(
            db, user_id=user_id, event_type=event_type, source_id=source_id,
            event_data=event_data, verdict=verdict, decision=verdict.decision,
        )

    # 仅灰名单 / 均未命中 → 正常风控流程（灰名单命中时加严）
    out = run_risk_check(
        db, user_id=user_id, event_type=event_type, source_id=source_id,
        event_data=event_data, order_id=order_id, receive_id=receive_id,
        device_id=device_id, login_ip=login_ip,
        graylist_hit=verdict.graylist_hit,
    )
    return _from_decision(out, verdict)


def _from_decision(out: DecisionOutcome, verdict: ListVerdict) -> EventProcessResult:
    """把决策引擎的输出包成事件管道的统一结果"""
    return EventProcessResult(
        assessment_id=out.assessment_id,
        event_id=out.event_id,
        user_id=out.user_id,
        event_type=out.event_type,
        source_id=out.source_id,
        final_score=out.final_score,
        risk_level=out.risk_level,
        decision=out.decision,
        rule_count=out.rule_count,
        list_verdict=verdict,
        conflict_flag=out.conflict_flag,
        case_id=out.case_id,
        challenge_id=out.challenge_id,
        features=out.features,
        triggered_rules=out.triggered_rules(),
    )


__all__ = [
    "EventProcessResult",
    "EventServiceError",
    "EventTypeInvalidError",
    "ListServiceError",
    "UserNotFoundError",
    "process_event",
    "ensure_user_exists",
]
