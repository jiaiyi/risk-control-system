# -*- coding: utf-8 -*-
"""登录 / 登出 接口

> 本模块是**超出 specs/PRD.md 本期范围**的新增项（PRD.md:110 原文声明本期不做
> 登录鉴权），因用户明确要求而补上。权限矩阵照抄 PRD.md:91-108。

页面登录（表单 POST → 302 跳转）与接口登录（JSON POST → 返回用户信息）
分成两条路径：浏览器直接提交表单时不该看到 JSON，而前端 JS 调接口时需要 JSON。
"""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.assets import register_template_globals
from app.errors import AuthBackendUnavailableError, AuthForbiddenError, AuthRequiredError, ServiceError
from app.schemas import LoginRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["鉴权"])

templates = Jinja2Templates(directory="templates")
register_template_globals(templates)


def _safe_next(target: str | None) -> str:
    """只允许跳回站内路径，防止 `?next=https://evil.example` 变成开放重定向"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@router.get("/login", response_class=HTMLResponse, summary="登录页")
def page_login(request: Request, next: str | None = None):
    """渲染登录页；已登录则直接跳走，避免重复登录"""
    try:
        user = auth.current_user(request)
    except auth.AuthBackendError as exc:
        return _login_page(request, next=next, error=f"鉴权后端不可用：{exc}")

    if user is not None:
        return RedirectResponse(_safe_next(next), status_code=302)
    return _login_page(request, next=next)


def _login_page(request: Request, next: str | None = None,
                error: str = "", user_id: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": _safe_next(next) if next else "/",
            "error": error,
            "user_id": user_id,
            # 登录页展示可选的演示账号（本系统为本地演示环境）
            "demo_accounts": [
                {"user_id": u.user_id, "name": u.name, "role": u.role,
                 "initial": u.initial, "title": u.title}
                for u in auth.all_users()
            ],
            "password_hint": auth._DEMO_PASSWORD,
        },
    )


@router.post("/login", summary="提交登录（表单）")
def do_login_form(
    request: Request,
    user_id: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    """表单登录：成功 302 跳转，失败重渲染登录页并带错误提示"""
    uid = (user_id or "").strip()
    user = auth.verify_credentials(uid, password)
    if user is None:
        # 不区分「账号不存在」与「口令错误」
        return _login_page(request, next=next,
                           error="账号或密码不正确", user_id=uid)

    try:
        token = auth.create_session(user)
    except auth.AuthBackendError as exc:
        return _login_page(request, next=next, error=f"登录失败：{exc}", user_id=uid)

    logger.info("用户登录 %s（%s）", user.user_id, user.role)
    resp = RedirectResponse(_safe_next(next), status_code=302)
    auth.set_session_cookie(resp, token)
    return resp


@router.post("/api/auth/login", summary="提交登录（JSON）")
def do_login_json(payload: LoginRequest):
    """接口登录：返回用户信息与权限码，供前端按权限渲染"""
    user = auth.verify_credentials(payload.user_id, payload.password)
    if user is None:
        raise AuthRequiredError("账号或密码不正确")
    try:
        token = auth.create_session(user)
    except auth.AuthBackendError as exc:
        raise AuthBackendUnavailableError(str(exc)) from exc
    logger.info("用户登录 %s（%s）", user.user_id, user.role)
    resp = JSONResponse({"ok": True, "user": user.to_dict()})
    auth.set_session_cookie(resp, token)
    return resp


@router.post("/logout", summary="退出登录（表单）")
def do_logout_form(request: Request):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=302)
    auth.clear_session_cookie(resp)
    return resp


@router.post("/api/auth/logout", summary="退出登录（JSON）")
def do_logout_json(request: Request):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


@router.get("/api/auth/me", summary="当前登录用户")
def whoami(request: Request) -> dict:
    user = auth.current_user(request)
    if user is None:
        raise AuthRequiredError("未登录或登录已过期")
    return {"ok": True, "user": user.to_dict()}


@router.get("/api/auth/roles", summary="角色与权限矩阵（供前端/文档核对）")
def roles(request: Request) -> dict:
    """公开权限矩阵。不含账号口令，仅用于说明各角色能力边界。"""
    return {
        "roles": list(auth.ROLES),
        "permissions": {
            role: sorted(auth.role_permissions(role)) for role in auth.ROLES
        },
    }
