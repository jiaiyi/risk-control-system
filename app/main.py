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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    agent as agent_api,
    audit as audit_api,
    auth_api,
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
from app.auth import SESSION_COOKIE, current_user
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
# 鉴权：页面守卫（未登录跳登录页；已登录但角色不够 → 403 页）
# ----------------------------------------------------------------------
# 无需登录即可访问的路径。**必须用精确匹配或明确前缀**，不要图省事写成
# `startswith("/api")` —— 那等于把整个接口层放行。
#
# ⚠ `/api/auth/login` 与 `/api/auth/logout` 必须在这里：登录接口本身当然
#   不能要求「先登录」。漏掉的症状很隐蔽 —— 表单登录（POST /login）仍然
#   正常，只有前端 JS 走的 JSON 登录会拿到 401「未登录或登录已过期」，
#   而这条错误文案会让人以为是会话过期，不会怀疑到中间件白名单上。
#   （这个坑真的踩过：CDP 里 fetch JSON 登录报 401，查了半天会话。）
_PUBLIC_PATHS = {
    "/login", "/logout", "/health", "/favicon.ico",
    "/api/auth/login", "/api/auth/logout",
}
_PUBLIC_PREFIXES = ("/static/", "/docs", "/redoc", "/openapi.json")

# 页面路径 → 所需权限。**不在表里的页面一律要求先登录**（白名单式放行，
# 新增页面若忘了登记权限，默认是「要登录」而不是「人人可进」）。
PAGE_PERMISSIONS: dict[str, str] = {
    "/":                  "dashboard:view",
    "/dashboard/loss":    "dashboard:view",
    "/risk-check":        "risk:check",
    "/rules":             "rules:view",
    "/rules/new":         "rules:manage",
    "/simulation":        "simulation:run",
    "/lists":             "lists:view",
    "/cases":             "cases:view",
    "/graph":             "graph:view",
    "/chat":              "agent:use",
    "/audit":             "audit:view",
    "/settings":          "settings:manage",
}

# 接口前缀 → (读权限, 写权限)。
#
# ⚠ 为什么写权限要单独列出而不是由读权限推导 `:view` → `:manage`：
#   1. 有些写操作有独立权限码（`lists:import` 与 `lists:manage` 在 PRD 里是
#      两个格子：策略师能导入，但「审核员仅拒绝时新增黑名单」是不导入的）；
#   2. 有些读接口的服务端码本身就是 manage（`/api/settings/*` 在 PRD 里
#      「系统配置」整块只给系统管理员，审计人员只读——所以读也得走 settings:manage）；
#   3. 有些接口读写同码（`/api/health/*` 只给系统管理员，读写都是 health:view）。
# 推导式映射在这三种情况上都会错，所以显式写死。
API_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("/api/rules",       "rules:view",      "rules:manage"),
    ("/api/cases",       "cases:view",      "cases:review"),
    ("/api/lists",       "lists:view",      "lists:manage"),
    ("/api/graph",       "graph:view",      "graph:view"),
    # ⚠ 是 /api/profile **单数**（路由注册为 /api/profile/{user_id}），
    #   写成 /api/profiles 会让这条映射永远匹配不上，权限静默退化成
    #   「登录即可访问」——所有角色都能查任意用户的画像。
    ("/api/profile",     "profiles:view",   "profiles:view"),
    ("/api/dashboard",   "dashboard:view",  "dashboard:view"),
    ("/api/simulation",  "simulation:run",  "simulation:run"),
    ("/api/audit-logs",  "audit:view",      "audit:view"),
    ("/api/health",      "health:view",     "health:view"),
    ("/api/settings",    "settings:view",   "settings:manage"),
    ("/api/agent",       "agent:use",       "agent:use"),
    ("/api/risk",        "risk:check",      "risk:check"),
    # 二次验证：只有做风险检查的角色会碰到。不登记的话会落到
    # `__authenticated__`，变成「任何登录角色都能核销任意验证码」。
    ("/api/challenge",   "risk:check",      "risk:check"),
)

# 某些子路径的写权限比前缀默认值更严（更专门的权限码）。
# 名单导入就是典型：它是「批量写」，PRD 单独给了 `lists:import`，
# 只有策略师有；审核员即使能新增单条黑名单也不能批量导入。
API_PERMISSION_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("/api/lists/import", "lists:import"),
)


def _required_permission(path: str, method: str) -> str | None:
    """判定该请求所需的权限码；返回 None 表示无需鉴权"""
    # 写操作取写权限，读操作取读权限 —— 权限矩阵里的「只读」角色
    # （如审计人员看规则、审阅名单）正是靠这一条生效。
    write = method not in ("GET", "HEAD", "OPTIONS")

    for prefix, perm in API_PERMISSION_OVERRIDES:
        if path.startswith(prefix):
            return perm

    for prefix, read_perm, write_perm in API_PERMISSIONS:
        if path.startswith(prefix):
            return write_perm if write else read_perm

    if path.startswith("/api"):
        return "__authenticated__"          # 未知接口：至少要求登录

    # 页面
    if path in PAGE_PERMISSIONS:
        return PAGE_PERMISSIONS[path]
    if path.startswith("/rules/"):          # /rules/{rule_id}
        return "rules:manage" if path != "/rules/" else "rules:view"
    if path.startswith("/cases/"):          # /cases/{case_id}
        return "cases:view"
    return "__authenticated__"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """页面与接口的鉴权守卫。

    分两种拦截方式，因为浏览器和前端 JS 对失败的反应不同：
      · 页面请求未登录 → 302 跳 /login?next=...（用户看到登录页，符合直觉）
      · 页面请求权限不足 → 渲染 403 页（**不跳登录**，否则用户会以为掉登录了）
      · 接口请求 → 一律 JSON 401/403，由前端 toast 提示
    """
    if not settings.AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    need = _required_permission(path, request.method)
    if need is None:
        return await call_next(request)

    is_api = path.startswith("/api")

    try:
        user = current_user(request)
    except Exception as exc:                      # noqa: BLE001  鉴权后端故障
        logger.error("鉴权后端异常：%s", exc)
        if is_api:
            return JSONResponse(
                status_code=503,
                content={"code": "AUTH_BACKEND_UNAVAILABLE",
                         "message": "鉴权服务不可用，请稍后重试", "detail": {}},
            )
        return HTMLResponse(
            _error_page("鉴权服务不可用", "登录状态校验失败，请稍后重试或联系系统管理员。"),
            status_code=503,
        )

    if user is None:
        if is_api:
            return JSONResponse(
                status_code=401,
                content={"code": "AUTH_REQUIRED",
                         "message": "未登录或登录已过期", "detail": {}},
            )
        target = path
        if request.url.query:
            target += "?" + request.url.query
        from urllib.parse import quote
        return RedirectResponse(f"/login?next={quote(target)}", status_code=302)

    # 已登录：检查权限（`__authenticated__` 表示只要求登录）
    if need != "__authenticated__" and not user.has(need):
        if is_api:
            return JSONResponse(
                status_code=403,
                content={"code": "AUTH_FORBIDDEN",
                         "message": f"当前角色「{user.role}」无权访问该资源",
                         "detail": {"role": user.role, "required": need}},
            )
        return HTMLResponse(
            _error_page(
                "无权访问",
                f"当前登录角色「{user.role}」没有访问该页面的权限。"
                f"如需查看，请切换为具备相应权限的账号登录。",
                user=user, required=need,
            ),
            status_code=403,
        )

    # 把用户挂到 request.state，供后续接口取真实操作人（审计日志用）
    request.state.user = user
    return await call_next(request)


def _error_page(title: str, message: str, user=None, required: str = "") -> str:
    """极简错误页（不依赖 base.html，避免守卫自身再触发鉴权）"""
    from app.assets import asset_version  # noqa: PLC0415

    v = asset_version()
    extra = ""
    if user is not None:
        extra = (f'<p class="hint">当前账号：{user.name}（{user.role}）'
                 f'{"　需要权限：" + required if required else ""}</p>')
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{title} · 电商风控系统</title>
<link rel="stylesheet" href="/static/vendor/bootstrap.min.css?v={v}">
<link rel="stylesheet" href="/static/css/app.css?v={v}">
<link rel="stylesheet" href="/static/css/login.css?v={v}"></head>
<body class="auth-error-body">
<div class="auth-error-card">
  <div class="auth-error-code">{title}</div>
  <p class="auth-error-msg">{message}</p>
  {extra}
  <a class="btn btn-primary btn-sm" href="/login">重新登录</a>
</div>
</body></html>"""


# ----------------------------------------------------------------------
# 静态资源（前端 T-19 落地后再挂；目录不存在时不报错）
# ----------------------------------------------------------------------
_static_dir = Path(__file__).resolve().parent.parent / "static"


class _NoCacheStaticFiles(StaticFiles):
    """静态资源强制每次校验新鲜度（`Cache-Control: no-cache` + ETag 复用）。

    默认 StaticFiles 不发 Cache-Control，浏览器便按启发式规则缓存 JS/CSS。
    后果是「改了代码、刷新页面仍拿旧文件」：修复已经上线，用户看到的还是
    坏页面，且报错信息与当前代码对不上（例如 `qs is not defined`），
    排查方向会完全跑偏。这里加 `no-cache`：**仍允许浏览器缓存**（配合 ETag
    返回 304，不浪费带宽），但每次必须先向服务端校验，避免拿到过期资源。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if _static_dir.is_dir():
    app.mount("/static", _NoCacheStaticFiles(directory=str(_static_dir)), name="static")


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
app.include_router(auth_api.router)                       # 登录 / 登出 / 当前用户
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
