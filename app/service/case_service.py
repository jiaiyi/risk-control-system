# -*- coding: utf-8 -*-
"""案件服务 —— 查询 / 详情组装 / 审核处置 / 处置回写 / 统计

职责边界（SPEC 3.2）：
    案件查询、详情组装、审核处置、**处置回写业务系统**、统计。
    **不负责案件创建**（由决策引擎在判定时自动创建）。

处置回写（FR-48 / SPEC 6.5）：
    · 回写动作与目标状态**以映射表集中定义**（`WRITEBACK_MAP`），
      不允许散落在业务分支的 if/else 里 —— 便于审计与扩展
    · 回写与案件状态更新在**同一事务**（ADR-07：同步事务，不用 MQ）
    · 回写前**加行锁二次校验当前状态**，避免并发下基于过期状态改写
    · 回写失败 → 事务回滚（案件状态不推进），HTTP 仍返回 200，
      由响应体 `writeback.success=false` 表达（SPEC 6.3 约定 2）

⚠️ 状态取值口径（重要）：
    SPEC 6.5 用的是**口语化表述**，落到代码必须换成业务字典表的**实际值**，
    否则会写入业务系统不认识的状态（`order_status` / `postsale_status` 都是 varchar，
    写错值不报错 → 典型隐性故障）。对照表：

    | SPEC 6.5 表述        | 字典表实际值                    |
    |---------------------|--------------------------------|
    | 未支付               | `待支付`                        |
    | 已支付、未发货        | `待发货`                        |
    | 已发货 / 已完成       | `已发货` / `已签收` / `已完成`    |
    | 待审核 / 处理中（售后）| `审核中` / `退款中` / `退货中` / `换发货` / `换退货` |
    | 已驳回（售后）        | **`审核未通过`**（字典里没有「已驳回」） |
    | 已作废（券）          | `已作废`（本期为 FR-48 新扩的枚举值） |
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cache
from app.models import (
    RiskAssessment,
    RiskAuditLog,
    RiskCase,
    RiskFeature,
    RiskUserProfile,
)
from app.service import audit_service, list_service
from app.service.list_service import ListServiceError

logger = logging.getLogger(__name__)

# 案件可审核的状态（已终结的案件不可再处置）
REVIEWABLE_STATUS = ("待审核", "审核中")
CASE_DECISIONS = ("已通过", "已拒绝")


class CaseServiceError(Exception):
    code = "BIZ_CASE_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class CaseNotFoundError(CaseServiceError):
    code = "BIZ_CASE_NOT_FOUND"
    http_status = 404


class CaseStateError(CaseServiceError):
    """案件已终结（已通过 / 已拒绝 / 已关闭），不可重复处置"""
    code = "BIZ_CASE_STATE_INVALID"
    http_status = 409


# ============================================================
# 处置回写映射表（SPEC 6.5）
# ============================================================
@dataclass(frozen=True)
class WritebackSpec:
    """一个事件类型的回写规则

    `transitions`: 当前状态 → (目标状态, 动作说明)。
    **不在 transitions 里的状态一律不回写** —— 对应 SPEC 6.5 表中标 ✗ 的行
    （单据已终结，风控不越权改写业务终态）。
    """

    table: str
    key_column: str
    status_column: str
    transitions: dict

    def describe(self) -> str:
        return " / ".join(f"{k}→{v[0]}" for k, v in self.transitions.items())


WRITEBACK_MAP: dict[str, WritebackSpec] = {
    "下单": WritebackSpec(
        table="order_info", key_column="order_id", status_column="order_status",
        transitions={
            "待支付": ("已取消", "取消未支付订单"),
            "待发货": ("售后中", "标记风控拦截并转入退款流程（不直接退款，转人工确认）"),
        },
    ),
    "支付": WritebackSpec(
        table="order_info", key_column="order_id", status_column="order_status",
        transitions={
            "待支付": ("已取消", "标记风控拦截：阻止支付"),
            "待发货": ("售后中", "标记订单为风控拦截，阻止后续发货并转入退款流程"),
        },
    ),
    "售后申请": WritebackSpec(
        table="postsale", key_column="postsale_id", status_column="postsale_status",
        transitions={
            "审核中": ("审核未通过", "驳回售后申请"),
            "退款中": ("审核未通过", "驳回售后申请（退款流程中止）"),
            "退货中": ("审核未通过", "驳回售后申请（退货流程中止）"),
            "换发货": ("审核未通过", "驳回售后申请（换货流程中止）"),
            "换退货": ("审核未通过", "驳回售后申请（换货流程中止）"),
        },
    ),
    "营销领券": WritebackSpec(
        table="coupon_receive_record", key_column="receive_id", status_column="use_status",
        transitions={
            "未使用": ("已作废", "作废已领取未使用的券"),
        },
    ),
}

# 无对应业务表可改的事件类型：只记录处置结论
NO_TABLE_ACTIONS = {
    "登录": "强制该会话下线并要求重新验证（模拟实现：无会话表，仅记录处置结论）",
}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _audit(db: Session, *, operator: str, action_desc: str, target_id,
           before=None, after=None) -> RiskAuditLog:
    """写案件类审计流水（统一走 audit_service —— T-15 收编）"""
    return audit_service.log(
        db, operator=operator, action_type="案件", action_desc=action_desc,
        target_id=target_id, before=before, after=after,
    )


# ============================================================
# 处置回写执行器
# ============================================================
def execute_writeback(
    db: Session, *, event_type: str | None, source_id: str | None, decision: str,
) -> dict:
    """按映射表回写业务系统（在调用方的事务内执行，不自行 commit）

    返回 `{attempted, success, action, target, affected_rows, error}`，
    结构对齐 SPEC 6.2 API-12 的 `writeback` 字段。
    """
    result = {
        "attempted": False, "success": True, "action": None,
        "target": source_id, "affected_rows": 0, "error": None,
    }

    if decision != "拒绝":
        result["action"] = "处置结论非「拒绝」，无回写动作"
        return result

    # 「无表可写」是事件类型的**固有属性**，要先于 source_id 判断 ——
    # 登录事件本就不产生业务单据，不该被报成「缺少业务单据 ID」。
    if event_type in NO_TABLE_ACTIONS:
        result["attempted"] = True
        result["action"] = NO_TABLE_ACTIONS[event_type]
        return result

    if not source_id:
        result["action"] = "缺少业务单据 ID，无回写动作"
        return result

    spec = WRITEBACK_MAP.get(event_type or "")
    if spec is None:
        result["action"] = f"{event_type or '未知'} 事件无对应回写规则"
        return result

    result["attempted"] = True

    # 二次校验：FOR UPDATE 加行锁读取当前状态（SPEC 6.5 约束 2）
    row = db.execute(
        text(f"SELECT {spec.status_column} AS st FROM {spec.table} "
             f"WHERE {spec.key_column} = :k FOR UPDATE"),
        {"k": source_id},
    ).first()

    if row is None:
        result["success"] = False
        result["error"] = f"业务单据不存在：{spec.table}.{spec.key_column}={source_id}"
        logger.warning("回写失败（单据不存在）: %s", result["error"])
        return result

    current = row.st
    transition = spec.transitions.get(current)
    if transition is None:
        # SPEC 6.5：不在允许范围内的状态一律不回写，仅记录处置结论。
        # 这是**预期内的跳过**（单据已终结，风控不越权），不是失败 ——
        # 否则「已发货订单」的审核将永远无法结案。
        result["action"] = (f"不回写：当前状态「{current}」已超出允许范围"
                            f"（可回写：{spec.describe()}）")
        result["current_status"] = current
        logger.info("回写跳过（状态不在允许范围）: %s.%s=%s 当前=%s",
                    spec.table, spec.key_column, source_id, current)
        return result

    target_status, action_desc = transition
    updated = db.execute(
        text(f"UPDATE {spec.table} SET {spec.status_column} = :ts "
             f"WHERE {spec.key_column} = :k AND {spec.status_column} = :cs"),
        {"ts": target_status, "k": source_id, "cs": current},
    ).rowcount

    result["action"] = action_desc
    result["from_status"] = current
    result["to_status"] = target_status
    result["affected_rows"] = updated

    if updated != 1:
        result["success"] = False
        result["error"] = (f"回写未生效：期望更新 1 行，实际 {updated} 行"
                           f"（并发下状态可能已被其他流程改写）")
        logger.warning("回写失败: %s", result["error"])
        return result

    logger.info("回写成功: %s %s: %s → %s（%s）",
                spec.table, source_id, current, target_status, action_desc)
    return result


# ============================================================
# 查询
# ============================================================
def list_cases(
    db: Session,
    *,
    case_status: str | None = None,
    case_category: str | None = None,
    conflict_only: bool = False,
    user_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskCase]]:
    """案件列表（API-09 / FR-15），支持状态 / 分类 / 冲突筛选 + 分页"""
    query = db.query(RiskCase)
    if case_status:
        query = query.filter(RiskCase.case_status == case_status)
    if case_category:
        query = query.filter(RiskCase.case_category == case_category)
    if conflict_only:
        query = query.filter(RiskCase.conflict_flag == 1)
    if user_id:
        query = query.filter(RiskCase.user_id == user_id)

    total = query.count()
    items = (query.order_by(RiskCase.create_time.desc(), RiskCase.case_id.desc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


def case_detail(db: Session, case_id: str) -> dict:
    """案件详情（API-11 / FR-16）

    组装：案件 + 评估 + 命中规则明细 + 特征快照 + 用户画像 + 业务单据。
    关联主体（图谱，FR-07）由 T-14 提供，此处先留空占位。
    """
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        raise CaseNotFoundError(f"案件不存在：{case_id}", {"case_id": case_id})

    assessment = (db.query(RiskAssessment)
                  .filter(RiskAssessment.assessment_id == case.assessment_id).first())

    risk_detail = {}
    if case.risk_detail:
        try:
            risk_detail = json.loads(case.risk_detail)
        except (json.JSONDecodeError, TypeError):
            logger.warning("案件 %s 的 risk_detail 解析失败", case_id)

    profile = (db.query(RiskUserProfile)
               .filter(RiskUserProfile.user_id == case.user_id).first())

    features = []
    if assessment is not None:
        rows = (db.query(RiskFeature)
                .filter(RiskFeature.event_id == assessment.event_id).all())
        features = [{"entity_type": r.entity_type, "entity_id": r.entity_id,
                     "feature_name": r.feature_name,
                     "feature_value": float(r.feature_value) if r.feature_value is not None else None}
                    for r in rows]

    return {
        "case": {
            "case_id": case.case_id,
            "case_status": case.case_status,
            "case_category": case.case_category,
            "conflict_flag": case.conflict_flag,
            "user_id": case.user_id,
            # 「重做检查」靠这两个字段一键跳回检查页
            "source_id": case.source_id,
            "event_type": case.event_type,
            "reviewer": case.reviewer,
            "review_comment": case.review_comment,
            "review_time": case.review_time.isoformat(timespec="seconds") if case.review_time else None,
            "create_time": case.create_time.isoformat(timespec="seconds") if case.create_time else None,
        },
        "assessment": {
            "assessment_id": assessment.assessment_id,
            "final_score": assessment.final_score,
            "risk_level": assessment.risk_level,
            "decision": assessment.decision,
            "rule_count": assessment.rule_count,
            "create_time": assessment.create_time.isoformat(timespec="seconds") if assessment.create_time else None,
        } if assessment else None,
        "triggered_rules": risk_detail.get("rule_results", []),
        "score_breakdown": risk_detail.get("score_breakdown"),
        "graylist_hit": risk_detail.get("graylist_hit", False),
        "conflict_detail": risk_detail.get("conflict", {}).get("detail") if isinstance(risk_detail.get("conflict"), dict) else None,
        "writeback": risk_detail.get("writeback"),
        "features": features,
        "profile": {
            "user_id": profile.user_id,
            "risk_score": profile.risk_score,
            "risk_level": profile.risk_level,
            "total_orders": profile.total_orders,
            "refund_rate": float(profile.refund_rate) if profile.refund_rate is not None else 0,
            "complaint_count": profile.complaint_count,
            "assessment_count": profile.assessment_count,
        } if profile else None,
        "business_doc": _fetch_business_doc(db, case.event_type, case.source_id),
        "related_entities": [],     # T-14 图谱接入
    }


def _fetch_business_doc(db: Session, event_type: str | None, source_id: str | None) -> dict | None:
    """按事件类型 + 业务单据 ID 反查业务单据（案件详情用）"""
    if not source_id:
        return None
    if event_type in ("下单", "支付"):
        row = db.execute(text(
            "SELECT oi.order_id, oi.order_status, oi.create_time, oi.payment_time, "
            "oi.user_id, oi.receive_id, "
            "(SELECT COALESCE(SUM(od.final_amount),0) FROM order_detail od "
            " WHERE od.order_id = oi.order_id) AS total_amount "
            "FROM order_info oi WHERE oi.order_id = :k"), {"k": source_id}).first()
        return {"type": "订单", **dict(row._mapping)} if row else None
    if event_type == "售后申请":
        row = db.execute(text(
            "SELECT postsale_id, order_detail_id, postsale_status, receive_id "
            "FROM postsale WHERE postsale_id = :k"), {"k": source_id}).first()
        return {"type": "售后单", **dict(row._mapping)} if row else None
    if event_type == "营销领券":
        row = db.execute(text(
            "SELECT receive_id, user_id, coupon_id, coupon_type, coupon_amount, "
            "use_status, receive_time, device_id "
            "FROM coupon_receive_record WHERE receive_id = :k"), {"k": source_id}).first()
        return {"type": "领券记录", **dict(row._mapping)} if row else None
    if event_type == "物流投诉":
        row = db.execute(text(
            "SELECT record_id, logistics_id, logistics_complaint, complaint_time, user_id "
            "FROM logistics_complaints_record WHERE record_id = :k"), {"k": source_id}).first()
        return {"type": "投诉记录", **dict(row._mapping)} if row else None
    return None


def case_statistics(db: Session) -> dict:
    """案件统计（API-10 / FR-19）"""
    by_status = dict(db.execute(text(
        "SELECT case_status, COUNT(*) AS c FROM risk_case GROUP BY case_status"
    )).fetchall())
    by_category = dict(db.execute(text(
        "SELECT COALESCE(case_category,'未分类'), COUNT(*) AS c FROM risk_case "
        "GROUP BY case_category"
    )).fetchall())
    total = db.execute(text("SELECT COUNT(*) AS c FROM risk_case")).first()
    conflict = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_case WHERE conflict_flag = 1")).first()
    today = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_case WHERE create_time >= CURDATE()")).first()

    return {
        "total": total.c if total else 0,
        "today": today.c if today else 0,
        "conflict_cases": conflict.c if conflict else 0,
        "by_status": {k: v for k, v in by_status.items()},
        "by_category": by_category,
        "pending_review": by_status.get("待审核", 0),
    }


# ============================================================
# 审核处置（API-12 / FR-17 / FR-18 / FR-48）
# ============================================================
@dataclass
class ReviewResult:
    """审核结果（结构对齐 SPEC 6.2 API-12 响应）"""

    case_id: str
    case_status: str
    reviewer: str | None
    review_time: datetime | None
    case_category: str | None = None
    source_id: str | None = None
    event_type: str | None = None
    blacklist_added: list = field(default_factory=list)
    writeback: dict = field(default_factory=dict)
    audit_log_id: int | None = None
    message: str | None = None

    @property
    def success(self) -> bool:
        return self.writeback.get("success", True)

    def to_dict(self) -> dict:
        payload = {
            "case_id": self.case_id,
            "case_status": self.case_status,
            "reviewer": self.reviewer,
            "review_time": self.review_time.isoformat(timespec="seconds") if self.review_time else None,
            "case_category": self.case_category,
            "source_id": self.source_id,
            "event_type": self.event_type,
            "blacklist_added": self.blacklist_added,
            "writeback": self.writeback,
            "audit_log_id": self.audit_log_id,
        }
        if self.message:
            payload["message"] = self.message
        return payload


def _resolve_entity_value(
    db: Session, entity_type: str, *, user_id: str, source_id: str | None, event_type: str | None,
) -> str | None:
    """把「主体类型」解析成实际主体值（审核加黑名单时需要）"""
    if entity_type == "用户":
        return user_id
    if entity_type == "设备":
        row = db.execute(text(
            "SELECT device_id FROM user_login_record WHERE user_id = :u "
            "AND device_id IS NOT NULL ORDER BY login_time DESC LIMIT 1"),
            {"u": user_id}).first()
        return row.device_id if row else None
    if entity_type == "IP":
        row = db.execute(text(
            "SELECT login_ip FROM user_login_record WHERE user_id = :u "
            "AND login_ip IS NOT NULL ORDER BY login_time DESC LIMIT 1"),
            {"u": user_id}).first()
        return row.login_ip if row else None
    if entity_type == "手机号":
        row = db.execute(text(
            "SELECT receiver_phone FROM receive_info WHERE user_id = :u LIMIT 1"),
            {"u": user_id}).first()
        return row.receiver_phone if row else None
    if entity_type == "地址":
        row = db.execute(text(
            "SELECT receive_province, receive_city, receive_district, receive_street_address "
            "FROM receive_info WHERE user_id = :u LIMIT 1"), {"u": user_id}).first()
        return list_service.format_address(row) if row else None
    return None


def review_case(
    db: Session,
    *,
    case_id: str,
    decision: str,
    reviewer: str,
    review_comment: str | None = None,
    add_to_blacklist: bool = False,
    blacklist_entity_types: list[str] | None = None,
    blacklist_reason: str | None = None,
) -> ReviewResult:
    """审核处置（含处置回写）

    全部动作在**同一事务**内：更新案件 → 可选加黑名单 → 回写业务表 → 审计。
    回写失败时回滚整个事务，返回 `writeback.success=false`（HTTP 仍为 200）。
    """
    if decision not in CASE_DECISIONS:
        raise CaseServiceError(f"处置结论非法：{decision}（可选：{'/'.join(CASE_DECISIONS)}）",
                               {"decision": decision})

    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        raise CaseNotFoundError(f"案件不存在：{case_id}", {"case_id": case_id})
    if case.case_status not in REVIEWABLE_STATUS:
        raise CaseStateError(
            f"案件当前状态为「{case.case_status}」，不可重复处置",
            {"case_id": case_id, "case_status": case.case_status},
        )

    before = {"case_status": case.case_status, "reviewer": case.reviewer,
              "review_comment": case.review_comment}

    # ---- 1. 加黑名单（拒绝 + 显式要求时才做）----
    blacklist_added: list[dict] = []
    entity_types = blacklist_entity_types or (["用户"] if add_to_blacklist else [])
    blacklist_error = None
    if decision == "已拒绝" and add_to_blacklist:
        for et in entity_types:
            value = _resolve_entity_value(
                db, et, user_id=case.user_id,
                source_id=case.source_id, event_type=case.event_type)
            if not value:
                logger.info("加黑名单跳过：%s 主体值解析不到（user=%s）", et, case.user_id)
                continue
            try:
                # auto_commit=False：事务交给本函数统一控制
                list_service.add_list_item(
                    db, list_type="黑名单", entity_type=et, entity_value=value,
                    reason=blacklist_reason or f"案件 {case_id} 审核拒绝",
                    created_by=reviewer, auto_commit=False,
                )
                blacklist_added.append({"entity_type": et, "entity_value": value})
            except ListServiceError as exc:
                # 已在黑名单中等业务冲突不阻断结案，记录后继续
                blacklist_error = exc.message
                logger.info("加黑名单跳过：%s", exc.message)

    # ---- 2. 处置回写（拒绝时才有动作）----
    writeback = execute_writeback(
        db, event_type=case.event_type, source_id=case.source_id,
        decision="拒绝" if decision == "已拒绝" else "通过",
    )

    if writeback["attempted"] and not writeback["success"]:
        # 回写失败 → 整个事务回滚，案件状态不推进（API-12 约定）
        db.rollback()
        logger.warning("案件 %s 处置回写失败，事务已回滚：%s", case_id, writeback.get("error"))
        return ReviewResult(
            case_id=case_id, case_status=case.case_status, reviewer=None,
            review_time=None, case_category=case.case_category,
            source_id=case.source_id, event_type=case.event_type,
            writeback=writeback,
            message="处置回写失败，案件状态未推进，请人工处理后再试",
        )

    # ---- 3. 更新案件状态 ----
    case.case_status = decision
    case.reviewer = reviewer
    case.review_comment = review_comment
    case.review_time = datetime.now()

    # 回写结果写进案件详情（SPEC 6.5 约束 3）
    risk_detail = {}
    if case.risk_detail:
        try:
            risk_detail = json.loads(case.risk_detail)
        except (json.JSONDecodeError, TypeError):
            risk_detail = {}
    risk_detail["writeback"] = writeback
    if blacklist_added:
        risk_detail["blacklist_added"] = blacklist_added
    case.risk_detail = _json(risk_detail)

    # ---- 4. 审计（案件审核 + 回写动作）----
    log = _audit(
        db, operator=reviewer, action_desc=f"案件审核：{decision}", target_id=case_id,
        before=before,
        after={"case_status": decision, "review_comment": review_comment,
               "blacklist_added": blacklist_added, "writeback": writeback},
    )
    if writeback["attempted"] and writeback["action"]:
        _audit(db, operator=reviewer,
               action_desc=f"处置回写：{writeback['action'][:40]}", target_id=case.source_id or case_id,
               after=writeback)

    db.commit()

    # 写后失效：案件审核会改名单 / 画像 / 特征，缓存必须清
    if blacklist_added:
        cache.invalidate_list()
    cache.invalidate_features(case.user_id)
    cache.invalidate_profile(case.user_id)

    logger.info("案件审核完成: %s → %s（审核人=%s，加黑=%d，回写=%s）",
                case_id, decision, reviewer, len(blacklist_added),
                writeback.get("action"))

    return ReviewResult(
        case_id=case_id, case_status=case.case_status, reviewer=reviewer,
        review_time=case.review_time, case_category=case.case_category,
        source_id=case.source_id, event_type=case.event_type,
        blacklist_added=blacklist_added, writeback=writeback,
        audit_log_id=log.log_id,
        message=blacklist_error,
    )
