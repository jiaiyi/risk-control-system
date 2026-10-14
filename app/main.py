"""
电商风控系统 - FastAPI 应用入口

接口层职责（SPEC 3.2）：
    · 应用实例、生命周期钩子、中间件
    · **统一异常处理**：服务层 `ServiceError` → `{code, message, detail}`
    · 路由注册（12 个 Router）
    **不含任何业务逻辑** —— 全部交给服务层。

错误码体系见 SPEC 6.3；服务层所有异常的 `code` / `http_status` 都已按该体系定义，
所以这里一个处理器就够了，不用在每个 Router 里逐个 try/except。
"""
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    agent as agent_api,
    audit as audit_api,
    cases,
    challenge,
    dashboard,
    graph,
    lists,
    profiles,
    risk,
    rules,
    simulation,
)
from app.api import pages as pages_api
from app.api import settings as settings_api
from app.config import settings
from app.errors import ServiceError
from app.service import settings_service

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 62)
    logger.info("%s v%s 启动中", settings.APP_NAME, settings.APP_VERSION)
    logger.info("  MySQL  %s:%s/%s", settings.DB_HOST, settings.DB_PORT, settings.DB_NAME)
    logger.info("  Redis  %s:%s/%s  (enabled=%s)",
                settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB, settings.REDIS_ENABLED)
    logger.info("  LLM    %s @ %s", settings.LLM_MODEL_NAME, settings.LLM_BASE_URL)
    logger.info("  决策阈值  通过<%d · 标记<%d · 人工审核<%d · 拒绝",
                settings.RISK_PASS_THRESHOLD,
                settings.RISK_MARK_THRESHOLD,
                settings.RISK_REVIEW_THRESHOLD)

    # 回灌在线调整过、且已持久化的配置（FR-56）
    applied = settings_service.apply_overrides()
    if applied:
        logger.info("  运行时覆盖  %d 项：%s", len(applied), applied)

    logger.info("=" * 62)
    yield
    logger.info("%s 已停止", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="面向电商业务的实时风险控制系统 —— 规则驱动 · 决策可解释 · 流程可闭环 · 审计可追溯",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------
# 中间件：接口耗时采样（供 /api/health/metrics 的 P95 —— S-11 性能可验证）
# ----------------------------------------------------------------------
@app.middleware("http")
async def perf_middleware(request: Request, call_next):
    started = time.perf_counter()
    ok = True
    try:
        response = await call_next(request)
        ok = response.status_code < 500
        return response
    except Exception:
        ok = False
        raise
    finally:
        path = request.url.path
        if path.startswith("/api") or path == "/health":
            settings_service.perf.record(path, (time.perf_counter() - started) * 1000, ok)


# ----------------------------------------------------------------------
# 统一异常处理：服务层业务异常 → HTTP 响应
# ----------------------------------------------------------------------
@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """把服务层异常转成 SPEC 6.3 规定的统一错误响应体

    注意：`BIZ_WRITEBACK_FAILED` 这类场景**不抛异常**，而是返回 200 +
    `writeback.success=false`（由服务层直接返回结果表达），走不到这里。
    """
    logger.warning("业务异常 %s %s → %s: %s",
                   request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


# ----------------------------------------------------------------------
# 静态资源（前端 T-19 落地后再挂；目录不存在时不报错）
# ----------------------------------------------------------------------
_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/health", tags=["运维"], summary="服务存活探针")
def health() -> dict:
    """轻量探针（不含 DB 往返）—— 依赖健康度请用 `/api/health/metrics`"""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "redis_enabled": settings.REDIS_ENABLED,
        "decision_thresholds": {
            "pass": settings.RISK_PASS_THRESHOLD,
            "mark": settings.RISK_MARK_THRESHOLD,
            "review": settings.RISK_REVIEW_THRESHOLD,
        },
    }


# ----------------------------------------------------------------------
# 路由注册（SPEC 6.1 接口清单）
# ----------------------------------------------------------------------
API_PREFIX = "/api"

# 页面路由无 /api 前缀，必须最先注册，否则 /cases/{case_id} 会被 API 路由抢先匹配
app.include_router(pages_api.router)                     # API-30~37 + 2 个子页

app.include_router(risk.router, prefix=API_PREFIX)                  # API-01
app.include_router(rules.router, prefix=API_PREFIX)                 # API-02~08
app.include_router(cases.router, prefix=API_PREFIX)                 # API-09~12
app.include_router(lists.router, prefix=API_PREFIX)                 # API-13~17
app.include_router(graph.router, prefix=API_PREFIX)                 # API-18
app.include_router(profiles.router, prefix=API_PREFIX)              # API-19
app.include_router(dashboard.router, prefix=API_PREFIX)             # API-20~22
app.include_router(simulation.router, prefix=API_PREFIX)            # API-23~25
app.include_router(audit_api.router, prefix=API_PREFIX)             # API-26
app.include_router(challenge.router, prefix=API_PREFIX)             # API-27
app.include_router(agent_api.router, prefix=API_PREFIX)             # API-28/29
app.include_router(settings_api.router, prefix=API_PREFIX)          # API-46~49
app.include_router(settings_api.health_router, prefix=API_PREFIX)   # API-50~52


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
