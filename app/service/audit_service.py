# -*- coding: utf-8 -*-
"""审计服务 —— 操作审计流水的追加写入与多条件查询

合规约束（NFR-20 / NFR-21）：
    · 规则 / 名单 / 案件 / 配置 四类关键操作**强制留痕**
    · `risk_audit_log` **仅提供 INSERT 与 SELECT**，不暴露更新与删除接口，
      模型层也不定义变更方法 —— 审计不可篡改

事务约定：
    `log()` 只做 add + flush（拿到 log_id），**不自行 commit** ——
    它几乎所有时候都是在别人的事务里被调用的（名单变更、案件审核…），
    由调用方决定提交时机。确需独立提交的场景显式传 `auto_commit=True`。
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import RiskAuditLog

logger = logging.getLogger(__name__)

VALID_ACTION_TYPES = ("规则", "名单", "案件", "配置")


class AuditServiceError(Exception):
    code = "BIZ_AUDIT_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _dump(value) -> str | None:
    """把变更前后值序列化成 JSON 文本；已是字符串则原样保留"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def log(
    db: Session,
    *,
    operator: str | None,
    action_type: str,
    action_desc: str,
    target_id,
    before=None,
    after=None,
    auto_commit: bool = False,
) -> RiskAuditLog:
    """写一条审计流水

    `action_type` 限定为 规则 / 名单 / 案件 / 配置 四类（对应 DB 枚举）。
    `before` / `after` 接受 dict / list / str，内部统一序列化。
    """
    if action_type not in VALID_ACTION_TYPES:
        raise AuditServiceError(
            f"审计类别非法：{action_type}（可选：{'/'.join(VALID_ACTION_TYPES)}）",
            {"action_type": action_type},
        )
    if not action_desc:
        raise AuditServiceError("审计描述不能为空")

    row = RiskAuditLog(
        operator=operator or "system",
        action_type=action_type,
        action_desc=action_desc[:100],
        target_id=str(target_id),
        before_value=_dump(before),
        after_value=_dump(after),
    )
    db.add(row)
    db.flush()      # 立刻拿到自增 log_id，供响应体回传

    if auto_commit:
        db.commit()

    logger.info("审计: [%s] %s target=%s by=%s", action_type, action_desc, target_id, operator)
    return row


def query(
    db: Session,
    *,
    action_type: str | None = None,
    operator: str | None = None,
    target_id: str | None = None,
    keyword: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskAuditLog]]:
    """多条件查询审计流水（API-26 / FR-39），返回 (总数, 当前页)"""
    if action_type and action_type not in VALID_ACTION_TYPES:
        raise AuditServiceError(f"审计类别非法：{action_type}")

    q = db.query(RiskAuditLog)
    if action_type:
        q = q.filter(RiskAuditLog.action_type == action_type)
    if operator:
        q = q.filter(RiskAuditLog.operator == operator)
    if target_id:
        q = q.filter(RiskAuditLog.target_id == str(target_id))
    if keyword:
        q = q.filter(RiskAuditLog.action_desc.like(f"%{keyword}%"))
    if start_time:
        q = q.filter(RiskAuditLog.create_time >= start_time)
    if end_time:
        q = q.filter(RiskAuditLog.create_time <= end_time)

    total = q.count()
    items = (q.order_by(RiskAuditLog.log_id.desc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


def statistics(db: Session) -> dict:
    """审计统计：四类操作各自的流水量、最近一次操作时间"""
    from sqlalchemy import text

    by_type = {t: 0 for t in VALID_ACTION_TYPES}
    for r in db.execute(text(
        "SELECT action_type, COUNT(*) AS c FROM risk_audit_log GROUP BY action_type"
    )).fetchall():
        by_type[r.action_type] = r.c

    total = db.execute(text("SELECT COUNT(*) AS c FROM risk_audit_log")).first()
    latest = db.execute(text(
        "SELECT create_time FROM risk_audit_log ORDER BY log_id DESC LIMIT 1"
    )).first()

    return {
        "total": total.c if total else 0,
        "by_action_type": by_type,
        "last_time": latest.create_time.isoformat(sep=" ", timespec="seconds") if latest else None,
    }


def to_dict(row: RiskAuditLog) -> dict:
    """把审计记录转成响应结构（前后值解析回对象，便于前端展示 diff）"""
    def _load(value):
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    return {
        "log_id": row.log_id,
        "operator": row.operator,
        "action_type": row.action_type,
        "action_desc": row.action_desc,
        "target_id": row.target_id,
        "before_value": _load(row.before_value),
        "after_value": _load(row.after_value),
        "create_time": row.create_time.isoformat(sep=" ", timespec="seconds") if row.create_time else None,
    }
