# -*- coding: utf-8 -*-
"""API-09~12 案件管理

审核接口（API-12）含**处置回写业务系统**，是本项目与参考实现差异最大的地方之一。
回写失败时 HTTP 仍返回 **200**，由响应体 `writeback.success=false` 表达（SPEC 6.3 约定 2）。
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.auth import operator_of
from app.database import get_db
from app.schemas import CaseReviewRequest
from app.service import case_service

router = APIRouter(prefix="/cases", tags=["案件管理"])


@router.get("", summary="案件列表（API-09）")
def list_cases(
    case_status: str | None = Query(None, description="待审核 / 审核中 / 已通过 / 已拒绝 / 已关闭"),
    case_category: str | None = Query(None, description="案件分类（取最高优先级命中规则的场景）"),
    conflict_only: bool = Query(False, description="仅看名单冲突案件"),
    user_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    total, items = case_service.list_cases(
        db, case_status=case_status, case_category=case_category,
        conflict_only=conflict_only, user_id=user_id, page=page, page_size=page_size,
    )
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "case_id": c.case_id, "case_status": c.case_status,
            "case_category": c.case_category, "conflict_flag": c.conflict_flag,
            "user_id": c.user_id, "source_id": c.source_id, "event_type": c.event_type,
            "reviewer": c.reviewer,
            "create_time": c.create_time.isoformat(timespec="seconds") if c.create_time else None,
            "review_time": c.review_time.isoformat(timespec="seconds") if c.review_time else None,
        } for c in items],
    }


@router.get("/statistics", summary="案件统计（API-10）")
def case_statistics(db: Session = Depends(get_db)) -> dict:
    return case_service.case_statistics(db)


@router.get("/{case_id}", summary="案件详情（API-11）",
            description="组装：案件 + 评估 + 命中规则明细 + 特征快照 + 用户画像 + 业务单据")
def case_detail(case_id: str, db: Session = Depends(get_db)) -> dict:
    return case_service.case_detail(db, case_id)


@router.post("/{case_id}/review", summary="案件审核（API-12，含处置回写）")
def review_case(case_id: str, payload: CaseReviewRequest, request: Request,
                db: Session = Depends(get_db)) -> dict:
    """处置流程：更新案件 → 可选加黑名单 → **回写业务表** → 审计（同一事务）

    回写失败 → 事务回滚、案件状态不推进，HTTP 仍 200 + `writeback.success=false`。

    审核人优先取请求体（脚本/测试可显式指定），否则从会话取真实登录用户。
    """
    result = case_service.review_case(
        db, case_id=case_id,
        decision=payload.decision,
        reviewer=payload.reviewer or operator_of(request),
        review_comment=payload.review_comment,
        add_to_blacklist=payload.add_to_blacklist,
        blacklist_entity_types=payload.blacklist_entity_types,
        blacklist_reason=payload.blacklist_reason,
    )

    from app.service import dashboard_service
    dashboard_service.invalidate_overview_cache()

    return result.to_dict()
