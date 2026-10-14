# -*- coding: utf-8 -*-
"""规则管理服务 —— 规则 CRUD / 启停 / 逻辑删除 / 条件校验

审计要求（NFR-20）：规则类操作**强制留痕** —— 新增 / 修改 / 删除 / 启停
均写 `risk_audit_log`，且记录变更前后值。

缓存一致性（NFR-17 规则热更新）：
    任何规则变更后调 `cache.invalidate_rules()`，下一次风控检查立即使用新规则。

删除语义（FR-12）：
    **逻辑删除**（`is_deleted=1`），不物理删除 —— 历史评估的命中明细里还引用着这些规则 ID，
    物理删除会让历史案件无法解释。
"""
import json
import logging
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import cache
from app.engine.rule import validate_condition
from app.errors import ServiceError
from app.models import RiskRule
from app.service import audit_service

logger = logging.getLogger(__name__)


class RuleServiceError(ServiceError):
    code = "BIZ_RULE_ERROR"


class RuleNotFoundError(RuleServiceError):
    code = "BIZ_RULE_NOT_FOUND"
    http_status = 404


class RuleConditionInvalidError(RuleServiceError):
    code = "BIZ_RULE_CONDITION_INVALID"


def _next_rule_id(db: Session) -> str:
    """生成下一个自定义规则 ID：R 前缀 + 三位递增（避开 R001~R024 等预置规则）"""
    rows = db.query(RiskRule.rule_id).filter(RiskRule.rule_id.like("R%")).all()
    used = set()
    for (rid,) in rows:
        suffix = rid[1:]
        if suffix.isdigit():
            used.add(int(suffix))
    n = 1
    while n in used:
        n += 1
    return f"R{n:03d}"


def _rule_payload(rule: RiskRule) -> dict:
    return {
        "rule_id": rule.rule_id,
        "rule_name": rule.rule_name,
        "rule_category": rule.rule_category,
        "event_type": rule.event_type,
        "rule_condition": rule.rule_condition,
        "condition_parsed": rule.condition_dict,
        "risk_level": rule.risk_level,
        "risk_score": rule.risk_score,
        "action": rule.action,
        "is_enabled": rule.is_enabled,
        "priority": rule.priority,
        "is_deleted": rule.is_deleted,
        "description": rule.description,
        "create_time": rule.create_time.isoformat(timespec="seconds") if rule.create_time else None,
        "update_time": rule.update_time.isoformat(timespec="seconds") if rule.update_time else None,
    }


def to_dict(rule: RiskRule) -> dict:
    return _rule_payload(rule)


# ============================================================
# 查询
# ============================================================
def list_rules(
    db: Session,
    *,
    category: str | None = None,
    event_type: str | None = None,
    risk_level: str | None = None,
    is_enabled: int | None = None,
    keyword: str | None = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskRule]]:
    """规则列表（API-02 / FR-08）：默认排除逻辑删除的规则"""
    q = db.query(RiskRule)
    if not include_deleted:
        q = q.filter(RiskRule.is_deleted == 0)
    if category:
        q = q.filter(RiskRule.rule_category == category)
    if event_type:
        q = q.filter(RiskRule.event_type == event_type)
    if risk_level:
        q = q.filter(RiskRule.risk_level == risk_level)
    if is_enabled is not None:
        q = q.filter(RiskRule.is_enabled == is_enabled)
    if keyword:
        q = q.filter(or_(RiskRule.rule_name.like(f"%{keyword}%"),
                         RiskRule.rule_id.like(f"%{keyword}%")))

    total = q.count()
    items = (q.order_by(RiskRule.priority.desc(), RiskRule.rule_id.asc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


def get_rule(db: Session, rule_id: str) -> RiskRule:
    """规则详情（API-03）"""
    rule = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    if rule is None or rule.is_deleted:
        raise RuleNotFoundError(f"规则不存在：{rule_id}", {"rule_id": rule_id})
    return rule


def _ensure_condition_valid(condition: dict) -> None:
    ok, msg = validate_condition(condition)
    if not ok:
        raise RuleConditionInvalidError(f"条件表达式非法：{msg}", {"reason": msg})


# ============================================================
# 写入
# ============================================================
def create_rule(db: Session, payload: dict, operator: str | None = None) -> RiskRule:
    """新增规则（API-04 / FR-09）"""
    condition = payload["rule_condition"]
    _ensure_condition_valid(condition)

    rule_id = payload.get("rule_id") or _next_rule_id(db)
    if db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first() is not None:
        raise RuleServiceError(f"规则 ID 已存在：{rule_id}", {"rule_id": rule_id})

    rule = RiskRule(
        rule_id=rule_id,
        rule_name=payload["rule_name"],
        rule_category=payload["rule_category"],
        event_type=payload.get("event_type", "通用"),
        rule_condition=json.dumps(condition, ensure_ascii=False),
        risk_level=payload["risk_level"],
        risk_score=payload["risk_score"],
        action=payload["action"],
        priority=payload.get("priority", 0),
        description=payload.get("description"),
        is_enabled=payload.get("is_enabled", 1),
        is_deleted=0,
    )
    db.add(rule)
    db.flush()

    audit_service.log(
        db, operator=operator, action_type="规则", action_desc=f"新增规则 {rule_id}",
        target_id=rule_id, after=_rule_payload(rule),
    )
    db.commit()
    cache.invalidate_rules()

    logger.info("规则新增: %s (%s) 分值=%d 等级=%s", rule_id, rule.rule_name,
                rule.risk_score, rule.risk_level)
    return rule


def update_rule(db: Session, rule_id: str, payload: dict, operator: str | None = None) -> RiskRule:
    """编辑规则（API-05 / FR-10）—— 部分更新，只改传入的字段"""
    rule = get_rule(db, rule_id)
    before = _rule_payload(rule)

    fields = [k for k in (
        "rule_name", "rule_category", "event_type", "rule_condition",
        "risk_level", "risk_score", "action", "priority", "description",
    ) if k in payload and payload[k] is not None]

    if not fields:
        raise RuleServiceError("没有需要更新的字段", {"rule_id": rule_id})

    if "rule_condition" in fields:
        _ensure_condition_valid(payload["rule_condition"])
        rule.rule_condition = json.dumps(payload["rule_condition"], ensure_ascii=False)
        fields.remove("rule_condition")
    for k in fields:
        setattr(rule, k, payload[k])

    rule.update_time = datetime.now()
    db.flush()

    audit_service.log(
        db, operator=operator, action_type="规则", action_desc=f"修改规则 {rule_id}",
        target_id=rule_id, before=before, after=_rule_payload(rule),
    )
    db.commit()
    cache.invalidate_rules()

    logger.info("规则修改: %s 字段=%s", rule_id, fields + (["rule_condition"] if "rule_condition" not in fields and payload.get("rule_condition") else []))
    return rule


def toggle_rule(db: Session, rule_id: str, is_enabled: int, operator: str | None = None) -> RiskRule:
    """启用 / 停用规则（API-06 / FR-11）"""
    rule = get_rule(db, rule_id)
    before = {"is_enabled": rule.is_enabled}
    rule.is_enabled = is_enabled
    rule.update_time = datetime.now()
    db.flush()

    audit_service.log(
        db, operator=operator, action_type="规则",
        action_desc=f"{'启用' if is_enabled else '停用'}规则 {rule_id}",
        target_id=rule_id, before=before, after={"is_enabled": is_enabled},
    )
    db.commit()
    cache.invalidate_rules()

    logger.info("规则%s: %s", "启用" if is_enabled else "停用", rule_id)
    return rule


def delete_rule(db: Session, rule_id: str, operator: str | None = None) -> RiskRule:
    """逻辑删除规则（API-07 / FR-12）

    不物理删除：历史评估的 `rule_results` 里仍引用这些规则 ID，
    物理删除会让历史案件失去解释依据。
    """
    rule = get_rule(db, rule_id)
    before = _rule_payload(rule)
    rule.is_deleted = 1
    rule.is_enabled = 0
    rule.update_time = datetime.now()
    db.flush()

    audit_service.log(
        db, operator=operator, action_type="规则", action_desc=f"删除规则 {rule_id}（逻辑删除）",
        target_id=rule_id, before=before, after={"is_deleted": 1, "is_enabled": 0},
    )
    db.commit()
    cache.invalidate_rules()

    logger.info("规则逻辑删除: %s", rule_id)
    return rule


def statistics(db: Session) -> dict:
    """规则统计（供仪表盘 / 规则页头部）"""
    from sqlalchemy import text

    rows = db.execute(text(
        "SELECT rule_category, COUNT(*) AS c FROM risk_rule WHERE is_deleted = 0 "
        "GROUP BY rule_category"
    )).fetchall()
    by_category = {r.rule_category: r.c for r in rows}
    total = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_rule WHERE is_deleted = 0")).first()
    enabled = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_rule WHERE is_deleted = 0 AND is_enabled = 1")).first()
    return {
        "total": total.c if total else 0,
        "enabled": enabled.c if enabled else 0,
        "by_category": by_category,
    }
