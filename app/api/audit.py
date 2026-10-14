# -*- coding: utf-8 -*-
"""API-26 操作审计流水查询

审计表**只提供 INSERT 与 SELECT**（NFR-21 不可篡改）——
因此这里只有 GET，没有 PUT / DELETE。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.service import audit_service

router = APIRouter(prefix="/audit-logs", tags=["操作审计"])


@router.get("", summary="审计流水查询（API-26）")
def query_audit_logs(
    action_type: str | None = Query(None, description="规则 / 名单 / 案件 / 配置"),
    operator: str | None = Query(None),
    target_id: str | None = Query(None, description="操作对象 ID"),
    keyword: str | None = Query(None, description="按操作描述模糊搜索"),
    start_time: datetime | None = Query(None),
    end_time: datetime | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """前后值会解析回对象，前端可直接做 diff 展示"""
    total, items = audit_service.query(
        db, action_type=action_type, operator=operator, target_id=target_id,
        keyword=keyword, start_time=start_time, end_time=end_time,
        page=page, page_size=page_size,
    )
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [audit_service.to_dict(r) for r in items],
        "statistics": audit_service.statistics(db),
    }
