# -*- coding: utf-8 -*-
"""API-20~22 仪表盘

聚合走数据库侧（规则命中 TOP5 用 MySQL `JSON_TABLE` 库内展开），
结果缓存 60s —— 目标是仪表盘加载 < 2s（NFR-03）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.service import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["仪表盘"])


@router.get("/overview", summary="大盘概览（API-22）",
            description="今日概览（评估数 / 高风险数 / 待审数 / 通过率 / **拦截率**）"
                        " + 近 N 天趋势 + 规则命中 TOP5 + 风险等级分布")
def overview(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict:
    return dashboard_service.overview(db, days=days)


@router.get("/loss-prevention", summary="资损防护统计（API-20）",
            description="被拦截的案件数 / 账号数 / **拦截金额** —— 回答「风控挡下了多少钱」")
def loss_prevention(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
) -> dict:
    return dashboard_service.loss_prevention(db, days=days)


@router.get("/realtime", summary="实时事件流（API-21）",
            description="最近 N 条风控检查（事件 + 评估结果），供前端滚屏展示")
def realtime(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    events = dashboard_service.realtime_events(db, limit=limit)
    return {"total": len(events), "items": events}
