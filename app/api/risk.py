# -*- coding: utf-8 -*-
"""API-01 实时风险检查

接口层职责边界（SPEC 3.2）：只做参数校验与路由分发，**不含业务逻辑**。
全部判定交给 `event.process_event()`。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import RiskCheckRequest
from app.service.event import process_event

router = APIRouter(prefix="/risk", tags=["实时风控"])


@router.post("/check", summary="实时风险检查（API-01）",
             description="六类事件统一入口。响应结构对齐 SPEC 6.2 API-01。")
def risk_check(payload: RiskCheckRequest, db: Session = Depends(get_db)) -> dict:
    """链路：名单前置裁决 → 特征计算 → 规则匹配 → 评分决策 → 落库

    · 命中黑/白名单走**短路路径**（不做特征计算与规则匹配，AC-5 零特征快照）
    · 黑白同时命中 → 人工审核 + 冲突案件（评分仍按正常流程计算）
    · 仅灰名单命中 → 正常流程 + 评分加严
    """
    outcome = process_event(
        db,
        user_id=payload.user_id,
        event_type=payload.event_type,
        source_id=payload.source_id,
        event_data=payload.event_data,
        order_id=payload.order_id,
        receive_id=payload.receive_id,
    )

    from app.service import dashboard_service
    dashboard_service.invalidate_overview_cache()   # 新评估会影响大盘聚合

    return outcome.to_dict()
