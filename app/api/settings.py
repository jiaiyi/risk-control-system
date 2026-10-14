# -*- coding: utf-8 -*-
"""API-46~52 系统配置与健康度

两个前缀（`/settings` 与 `/health`）分属两个 router，
在 `main.py` 里分别注册（FastAPI 的 APIRouter 只能带一个 prefix）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import CachePolicyUpdateRequest, ThresholdUpdateRequest
from app.service import settings_service

# ---- /api/settings：阈值与缓存策略 ----
router = APIRouter(prefix="/settings", tags=["系统配置"])

# ---- /api/health：健康度与运维任务 ----
health_router = APIRouter(prefix="/health", tags=["健康度与运维"])


@router.get("/thresholds", summary="读取决策阈值与引擎参数（API-46）")
def get_thresholds() -> dict:
    return {
        "thresholds": settings_service.get_thresholds(),
        "note": "三个分界点必须满足 通过 ≤ 标记 ≤ 人工审核；"
                "一票否决仅由规则等级「极高」触发，不受灰名单加严影响（SPEC Q7）",
    }


@router.put("/thresholds", summary="在线调整阈值与引擎参数（API-47）",
            description="FR-56：变更**强制写审计**（记录前后值），并持久化到 "
                        "`data/runtime_settings.json`（重启后仍然生效）")
def update_thresholds(payload: ThresholdUpdateRequest,
                      db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_none=True)
    operator = data.pop("operator", "admin")
    return settings_service.update_thresholds(db, data, operator=operator)


@router.get("/cache-policy", summary="读取缓存与降级策略（API-48）")
def get_cache_policy() -> dict:
    return settings_service.get_cache_policy()


@router.put("/cache-policy", summary="调整缓存开关与 TTL（API-49）")
def update_cache_policy(payload: CachePolicyUpdateRequest,
                        db: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude_none=True)
    operator = data.pop("operator", "admin")
    return settings_service.update_cache_policy(db, data, operator=operator)


@router.get("/runtime-overrides", summary="查看当前运行时覆盖项",
            description="列出被在线调整过、与 .env 默认值不同的配置项，便于排查")
def runtime_overrides() -> dict:
    return {"overrides": settings_service._load_overrides()}


@health_router.get("/metrics", summary="健康度指标（API-50）",
                   description="P95 / 调用量 / 错误率 / 缓存命中率 / 依赖状态 / 引擎参数。"
                               "S-11 性能可验证。")
def health_metrics(db: Session = Depends(get_db)) -> dict:
    return settings_service.health_metrics(db)


@health_router.get("/tasks", summary="运维任务列表（API-51）")
def list_tasks() -> dict:
    return {"tasks": settings_service.list_tasks()}


@health_router.post("/tasks/{task}/run", summary="手动触发运维任务（API-52）",
                    description="可用任务：归档过期名单 / 预热缓存 / 健康检查 / 清理缓存")
def run_task(task: str, operator: str = Query("admin"),
             db: Session = Depends(get_db)) -> dict:
    result = settings_service.run_task(db, task, operator=operator)
    if task in ("归档过期名单", "清理缓存"):
        from app.service import dashboard_service
        dashboard_service.invalidate_overview_cache()
    return result


@health_router.post("/perf/reset", summary="重置性能采样（压测前清零）")
def reset_perf() -> dict:
    settings_service.perf.reset()
    return {"reset": True}
