# -*- coding: utf-8 -*-
"""API-18 关联图谱查询

四维度（设备 / IP / 手机号 / 地址）多跳扩散。
跳数默认 2、上限 3；每跳节点上限 200，超限截断并返回 `truncated=true`（NFR-06 / SPEC Q3）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.service import graph_service

router = APIRouter(prefix="/graph", tags=["关联图谱"])


@router.get("", summary="关联图谱查询（API-18）")
def query_graph(
    entity_type: str = Query(..., description="用户 / 手机号 / IP / 设备 / 地址"),
    entity_value: str = Query(..., description="主体值"),
    hops: int = Query(settings.GRAPH_DEFAULT_HOPS, ge=1, le=settings.GRAPH_MAX_HOPS,
                      description=f"跳数，默认 {settings.GRAPH_DEFAULT_HOPS}、上限 {settings.GRAPH_MAX_HOPS}"),
    max_nodes: int = Query(settings.GRAPH_MAX_NODES, ge=1, le=1000,
                           description="节点数上限，触顶即截断"),
    db: Session = Depends(get_db),
) -> dict:
    """返回 `{center, nodes, edges, truncated}`，可直接喂给 ECharts graph 渲染。

    ⚠️ 前端渲染上限为 100 节点（SPEC Q8）：超过时前端会提示缩小范围或降为 1 跳。
    """
    return graph_service.query_graph(
        db, entity_type=entity_type, entity_value=entity_value,
        hops=hops, max_nodes=max_nodes,
    )


@router.get("/related-accounts", summary="聚集判定辅助",
            description="某主体下一跳关联的去重账号数是否达到阈值（FR-07 验收 3）")
def related_accounts(
    entity_type: str = Query(..., description="通常传 设备 / IP / 手机号 / 地址"),
    entity_value: str = Query(...),
    min_accounts: int = Query(3, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return graph_service.related_accounts(
        db, entity_type=entity_type, entity_value=entity_value,
        min_accounts=min_accounts,
    )
