# -*- coding: utf-8 -*-
"""API-23~25 规则仿真回放

⚠️ 仿真必须**零副作用**（FR-14 验收 2）：
不写任何风控表、不回填 Redis。响应体带 `side_effects`，
数值来自**实测各表行数变化**，而不是口头声明。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import SimulationReplayRequest, SimulationRunRequest
from app.service import simulation_service

router = APIRouter(prefix="/simulation", tags=["规则仿真"])


@router.post("/run", summary="单条事件仿真（API-23）",
             description="分步展示：特征提取 → 逐条规则求值（命中/未命中 + 引用的特征实际值）"
                         " → 评分 → 决策输出。全程只读。")
def run_simulation(payload: SimulationRunRequest, db: Session = Depends(get_db)) -> dict:
    return simulation_service.run_single(
        db,
        user_id=payload.user_id,
        event_type=payload.event_type,
        source_id=payload.source_id,
        order_id=payload.order_id,
        receive_id=payload.receive_id,
        device_id=payload.device_id,
        login_ip=payload.login_ip,
        event_data=payload.event_data,
        graylist_hit=payload.graylist_hit,
    )


@router.post("/replay", summary="批量回放（API-24）",
             description="对一组历史事件批量回放，统计规则命中数 / 命中率 / 影响用户数。"
                         "改动前后各跑一次即可做「改前 vs 改后」对比。")
def replay(payload: SimulationReplayRequest, db: Session = Depends(get_db)) -> dict:
    return simulation_service.replay(
        db, event_type=payload.event_type, limit=payload.limit,
        rule_id=payload.rule_id, include_detail=payload.include_detail,
    )


@router.get("/samples", summary="作弊样本库列表（API-25）",
            description="样本从历史案件反推 —— 有案件即代表该事件曾被评为高风险。")
def list_samples(
    event_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    return simulation_service.list_samples(db, limit=limit, event_type=event_type)
