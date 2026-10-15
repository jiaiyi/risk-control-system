# -*- coding: utf-8 -*-
"""API-02~08 规则管理

覆盖：列表 / 详情 / 新增 / 编辑（部分更新）/ 启停 / **逻辑删除** / 条件校验。
规则变更都会失效规则缓存，下一次检查立即生效（NFR-17 热更新）。
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import operator_of
from app.database import get_db
from app.engine.rule import validate_condition
from app.schemas import (
    RuleConditionValidateRequest,
    RuleCreateRequest,
    RuleToggleRequest,
    RuleUpdateRequest,
)
from app.service import rule_service

router = APIRouter(prefix="/rules", tags=["规则管理"])


@router.get("", summary="规则列表（API-02）")
def list_rules(
    rule_category: str | None = Query(None, description="风险场景分类"),
    event_type: str | None = Query(None, description="适用事件类型"),
    risk_level: str | None = Query(None, description="风险等级"),
    is_enabled: int | None = Query(None, ge=0, le=1),
    keyword: str | None = Query(None, description="按规则 ID / 名称模糊搜索"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """默认排除逻辑删除的规则（FR-12）"""
    total, items = rule_service.list_rules(
        db, category=rule_category, event_type=event_type, risk_level=risk_level,
        is_enabled=is_enabled, keyword=keyword, page=page, page_size=page_size,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [rule_service.to_dict(r) for r in items],
        "statistics": rule_service.statistics(db),
    }


@router.get("/statistics", summary="规则统计（供规则页头部）")
def rule_statistics(db: Session = Depends(get_db)) -> dict:
    return rule_service.statistics(db)


@router.post("/validate", summary="校验条件表达式（API-08）",
             description="只校验结构合法性，不落库。供前端编辑规则时实时提示。")
def validate_rule_condition(payload: RuleConditionValidateRequest) -> dict:
    ok, message = validate_condition(payload.rule_condition)
    return {"valid": ok, "message": message}


@router.get("/{rule_id}", summary="规则详情（API-03）")
def get_rule(rule_id: str, db: Session = Depends(get_db)) -> dict:
    return rule_service.to_dict(rule_service.get_rule(db, rule_id))


@router.post("", status_code=201, summary="新增规则（API-04）")
def create_rule(payload: RuleCreateRequest, request: Request,
                db: Session = Depends(get_db)) -> dict:
    # 操作人取登录用户（鉴权落地后审计字段自动生效，见 PRD.md:110）
    rule = rule_service.create_rule(db, payload.model_dump(),
                                    operator=operator_of(request))
    return rule_service.to_dict(rule)


@router.put("/{rule_id}", summary="编辑规则（API-05，部分更新）")
def update_rule(rule_id: str, payload: RuleUpdateRequest, request: Request,
                db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_none=True)
    # 请求体里的 operator 优先（脚本/测试可显式指定），否则用登录用户
    operator = data.pop("operator", None) or operator_of(request)
    rule = rule_service.update_rule(db, rule_id, data, operator=operator)
    return rule_service.to_dict(rule)


@router.put("/{rule_id}/toggle", summary="启停规则（API-06）")
def toggle_rule(rule_id: str, payload: RuleToggleRequest, request: Request,
                db: Session = Depends(get_db)) -> dict:
    rule = rule_service.toggle_rule(db, rule_id, payload.is_enabled,
                                    operator=payload.operator or operator_of(request))
    return rule_service.to_dict(rule)


@router.delete("/{rule_id}", summary="逻辑删除规则（API-07）",
               description="FR-12：不物理删除 —— 历史评估的命中明细仍引用这些规则 ID。")
def delete_rule(rule_id: str, request: Request, operator: str | None = Query(None),
                db: Session = Depends(get_db)) -> dict:
    rule = rule_service.delete_rule(db, rule_id, operator=operator or operator_of(request))
    return {"rule_id": rule.rule_id, "is_deleted": rule.is_deleted,
            "message": "已逻辑删除，历史案件仍可追溯该规则"}
