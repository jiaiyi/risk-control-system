# -*- coding: utf-8 -*-
"""API-13~17 名单管理（三色 × 五类主体）

额外提供两个本项目的增强端点：
    · `GET /lists/archived`  历史名单独立检索（归档表）
    · `GET /lists/subject-history`  某主体的完整名单履历
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth import operator_of
from app.database import get_db
from app.schemas import ListItemCreateRequest
from app.service import list_service

router = APIRouter(prefix="/lists", tags=["名单管理"])


@router.get("", summary="名单列表（API-13）")
def list_items(
    list_type: str | None = Query(None, description="黑名单 / 白名单 / 灰名单"),
    entity_type: str | None = Query(None, description="用户 / 手机号 / IP / 设备 / 地址"),
    keyword: str | None = Query(None, description="按主体值模糊搜索"),
    include_expired: bool = Query(False, description="是否包含已过期条目"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    total, items = list_service.list_items(
        db, list_type=list_type, entity_type=entity_type, keyword=keyword,
        include_expired=include_expired, page=page, page_size=page_size,
    )
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "list_id": i.list_id, "list_type": i.list_type, "entity_type": i.entity_type,
            "entity_value": i.entity_value, "reason": i.reason,
            "expire_time": i.expire_time.isoformat(sep=" ", timespec="seconds") if i.expire_time else None,
            "created_by": i.created_by,
            "create_time": i.create_time.isoformat(sep=" ", timespec="seconds") if i.create_time else None,
            "expired": i.expire_time is not None and i.expire_time <= datetime.now(),
        } for i in items],
        "statistics": list_service.list_statistics(db),
    }


@router.get("/statistics", summary="名单统计（三色 × 五类分布）")
def list_statistics(db: Session = Depends(get_db)) -> dict:
    return list_service.list_statistics(db)


@router.get("/archived", summary="历史名单检索（归档表）",
            description="按主体 / 类型 / 归档时间范围检索已归档的历史名单条目。")
def list_archived(
    list_type: str | None = Query(None),
    entity_type: str | None = Query(None),
    entity_value: str | None = Query(None, description="精确匹配主体值"),
    start_time: datetime | None = Query(None, description="归档时间下限"),
    end_time: datetime | None = Query(None, description="归档时间上限"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    total, items = list_service.list_archived(
        db, list_type=list_type, entity_type=entity_type, entity_value=entity_value,
        start_time=start_time, end_time=end_time, page=page, page_size=page_size,
    )
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "archive_id": a.archive_id, "original_list_id": a.original_list_id,
            "list_type": a.list_type, "entity_type": a.entity_type,
            "entity_value": a.entity_value, "reason": a.reason,
            "expire_time": a.expire_time.isoformat(sep=" ", timespec="seconds") if a.expire_time else None,
            "archive_time": a.archive_time.isoformat(sep=" ", timespec="seconds") if a.archive_time else None,
            "archived_by": a.archived_by, "archive_reason": a.archive_reason,
        } for a in items],
    }


@router.get("/subject-history", summary="某主体的完整名单履历",
            description="当前生效条目 + 历史归档，回答「这个主体被处理过几次」。")
def subject_history(
    entity_type: str = Query(..., description="用户 / 手机号 / IP / 设备 / 地址"),
    entity_value: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    return list_service.subject_history(db, entity_type, entity_value)


@router.get("/template", summary="下载导入模板（API-17）")
def download_template() -> dict:
    """返回模板的 base64 内容（前端可直接落成 .xlsx 文件）

    直接返回字节流需要 `Response(content=..., media_type=...)`，
    这里同时给出 base64 与提示，便于前端两种用法都能走通。
    """
    import base64

    content = list_service.build_import_template()
    return {
        "filename": "名单导入模板.xlsx",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "columns": ["list_type", "entity_type", "entity_value", "reason", "expire_time", "created_by"],
        "max_rows": list_service.IMPORT_MAX_ROWS,
    }


@router.post("/import", summary="名单批量导入（API-16）",
             description="全量校验 → 一次性返回全部错误行 → 全部通过才整批入库（SPEC Q9）")
async def import_lists(
    request: Request,
    file: UploadFile = File(..., description=".xlsx 或 .csv"),
    operator: str | None = Form(None),
    force: bool = Form(False, description="白名单超 90 天时的二次确认"),
    db: Session = Depends(get_db),
) -> dict:
    content = await file.read()
    rows = list_service.parse_import_file(file.filename or "", content)
    report = list_service.import_items(db, rows,
                                       operator=operator or operator_of(request),
                                       force=force)

    payload = report.to_dict()
    if not report.ok:
        # 整批拒绝：HTTP 200 + success=false + 全部错误行，供前端一次性展示
        payload["code"] = "BIZ_LIST_IMPORT_INVALID"
        payload["message"] = f"{len(report.errors)} 行校验未通过，本次未导入任何数据，请修正后重传"
    return payload


@router.post("", status_code=201, summary="新增名单项（API-14）")
def add_list_item(payload: ListItemCreateRequest, request: Request,
                  db: Session = Depends(get_db)) -> dict:
    """白名单硬约束（PRD 4.6）：必须有有效期、≤90 天（超出需 force）、与黑名单互斥"""
    item = list_service.add_list_item(
        db, list_type=payload.list_type, entity_type=payload.entity_type,
        entity_value=payload.entity_value, reason=payload.reason,
        expire_time=payload.expire_time,
        created_by=payload.created_by or operator_of(request),
        force=payload.force,
    )
    return {
        "list_id": item.list_id, "list_type": item.list_type,
        "entity_type": item.entity_type, "entity_value": item.entity_value,
        "reason": item.reason,
        "expire_time": item.expire_time.isoformat(sep=" ", timespec="seconds") if item.expire_time else None,
    }


@router.delete("/{list_id}", summary="移除名单项（API-15）")
def remove_list_item(list_id: int, request: Request, operator: str | None = Query(None),
                     db: Session = Depends(get_db)) -> dict:
    removed = list_service.remove_list_item(db, list_id,
                                            operator=operator or operator_of(request))
    return {"removed": True, **removed}
