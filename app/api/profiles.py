# -*- coding: utf-8 -*-
"""API-19 用户风险画像

FR-25 画像查询 + FR-43 动态衰减。另提供运维用的「历史重放重算」。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.service import profile_service

router = APIRouter(prefix="/profile", tags=["用户画像"])


@router.get("", summary="画像列表（按风险分降序）")
def list_profiles(
    risk_level: str | None = Query(None, description="低 / 中 / 高 / 极高"),
    min_score: int | None = Query(None, ge=0, le=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    total, items = profile_service.list_profiles(
        db, risk_level=risk_level, min_score=min_score, page=page, page_size=page_size)
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "user_id": p.user_id, "risk_score": p.risk_score, "risk_level": p.risk_level,
            "total_orders": p.total_orders, "refund_rate": float(p.refund_rate or 0),
            "complaint_count": p.complaint_count, "assessment_count": p.assessment_count,
            "last_assessment_time": (
                p.last_assessment_time.isoformat(timespec="seconds")
                if p.last_assessment_time else None),
        } for p in items],
        "statistics": profile_service.statistics(db),
    }


@router.get("/{user_id}", summary="用户画像详情（API-19）",
            description="基础画像 + 评分趋势 + 名单履历（当前生效 + 历史归档）")
def get_profile(
    user_id: str,
    trend_limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return profile_service.get_profile(db, user_id, trend_limit=trend_limit)


@router.post("/{user_id}/rebuild", summary="按历史评估重放重算画像（运维）",
             description="⚠️ 已知近似：α 依赖「当时的近 30 天订单数」，历史值无法回溯，"
                         "统一使用当前 α。用于排查画像被写脏，不替代实时链路。")
def rebuild_profile(user_id: str, db: Session = Depends(get_db)) -> dict:
    return profile_service.rebuild_profile(db, user_id)
