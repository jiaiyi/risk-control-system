"""
电商风控系统 - FastAPI 应用入口

当前为项目骨架（SPEC 任务 T-01）：
  - 应用实例与生命周期钩子
  - 静态资源挂载
  - 健康检查接口

各业务路由（10 个 Router / 45 个端点）将随任务 T-17 逐步注册启用。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings

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
    logger.info("=" * 62)
    yield
    logger.info("%s 已停止", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="面向电商业务的实时风险控制系统 —— 规则驱动 · 决策可解释 · 流程可闭环 · 审计可追溯",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    """服务存活探针，同时回显关键配置以便排查环境问题。"""
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
# 业务路由注册（随任务推进逐步启用，见 SPEC 第 10 章 T-17）
# ----------------------------------------------------------------------
# from app.api import (pages, risk, rules, cases, lists, profiles,
#                      dashboard, graph, simulation, audit, agent)
#
# app.include_router(pages.router)                 # 8 个页面
# app.include_router(risk.router, prefix="/api")   # 实时风险检查
# app.include_router(rules.router, prefix="/api")  # 规则管理 + 仿真
# app.include_router(cases.router, prefix="/api")  # 案件管理 + 处置回写
# app.include_router(lists.router, prefix="/api")  # 三色名单 + 批量导入
# app.include_router(profiles.router, prefix="/api")
# app.include_router(dashboard.router, prefix="/api")
# app.include_router(graph.router, prefix="/api")
# app.include_router(simulation.router, prefix="/api")
# app.include_router(audit.router, prefix="/api")
# app.include_router(agent.router, prefix="/api")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
