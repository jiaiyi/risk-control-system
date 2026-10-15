# -*- coding: utf-8 -*-
"""页面路由（API-30~37）

侧边栏 10 个导航页 + 2 个子页（案件详情 / 规则编辑）= **12 个功能页**，
清单与 `design/prototype-v2.pen` 一致（原型 13 张图 = 封面 + 12 页）。

页面只负责渲染骨架，数据一律由前端调 `/api/**` 获取 ——
这样首屏快，且前端组件可以直接复用接口层的响应结构。
"""
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.assets import register_template_globals
from app.auth import current_user
from app.database import SessionLocal

router = APIRouter(tags=["页面"])

templates = Jinja2Templates(directory="templates")
register_template_globals(templates)


def _render(request: Request, template: str, active: str, **extra):
    """渲染页面，并把当前登录用户注入模板。

    鉴权守卫（app/main.py 的 auth_middleware）已保证走到这里的一定是
    已登录且有权限的用户，所以这里取用户是为了**渲染**（侧边栏按权限过滤、
    右上角显示真实姓名角色），不再做校验。
    """
    try:
        user = getattr(request.state, "user", None) or current_user(request)
    except Exception:                             # noqa: BLE001
        user = None
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "active": active,
            "user": user,
            "user_perms": sorted(user.permissions) if user else [],
            **extra,
        },
    )


# ---------------- 监控 ----------------
@router.get("/", response_class=HTMLResponse, summary="仪表盘")
def page_dashboard(request: Request):
    return _render(request, "pages/dashboard.html", "dashboard",
                   page_title="风控仪表盘",
                   page_subtitle="实时反映今日风控态势，数据每 5 秒刷新")


@router.get("/risk-check", response_class=HTMLResponse, summary="风险检查")
def page_risk_check(request: Request):
    return _render(request, "pages/risk-check.html", "risk-check",
                   page_title="风险检查",
                   page_subtitle="对六类事件发起实时风控评估，输出可解释的判定结果")


# ---------------- 策略 ----------------
@router.get("/rules", response_class=HTMLResponse, summary="规则管理")
def page_rules(request: Request):
    return _render(request, "pages/rules.html", "rules",
                   page_title="规则管理",
                   page_subtitle="41 条预置规则 · JSON 条件表达式 · 白名单运算符求值")


@router.get("/rules/new", response_class=HTMLResponse, summary="新建规则")
def page_rule_new(request: Request):
    return _render(request, "pages/rule-edit.html", "rules",
                   page_title="新建规则",
                   page_subtitle="填写规则要素与条件表达式，保存后立即生效")


@router.get("/rules/{rule_id}", response_class=HTMLResponse, summary="编辑规则")
def page_rule_edit(request: Request, rule_id: str):
    return _render(request, "pages/rule-edit.html", "rules",
                   page_title=f"编辑规则 {rule_id}",
                   page_subtitle="部分更新：只提交需要修改的字段",
                   rule_id=rule_id)


@router.get("/simulation", response_class=HTMLResponse, summary="规则仿真回放")
def page_simulation(request: Request):
    return _render(request, "pages/simulation.html", "simulation",
                   page_title="规则仿真回放",
                   page_subtitle="分步展示命中链路 · 批量回放零副作用")


@router.get("/lists", response_class=HTMLResponse, summary="名单管理")
def page_lists(request: Request):
    return _render(request, "pages/lists.html", "lists",
                   page_title="名单管理",
                   page_subtitle="三色名单 × 五类主体 · 白名单需书面凭据与有效期")


# ---------------- 运营 ----------------
@router.get("/cases", response_class=HTMLResponse, summary="案件管理")
def page_cases(request: Request):
    return _render(request, "pages/cases.html", "cases",
                   page_title="案件管理",
                   page_subtitle="待审核案件优先处理 · 名单冲突案件有标记")


@router.get("/cases/{case_id}", response_class=HTMLResponse, summary="案件详情")
def page_case_detail(request: Request, case_id: str):
    return _render(request, "pages/case-detail.html", "cases",
                   page_title=f"案件 {case_id}",
                   page_subtitle="命中规则 + 特征快照 + 用户画像 + 业务单据",
                   case_id=case_id)


@router.get("/graph", response_class=HTMLResponse, summary="关联图谱")
def page_graph(request: Request):
    return _render(request, "pages/graph.html", "graph",
                   page_title="关联图谱",
                   page_subtitle="四维度关联 · 默认 2 跳 · 前端渲染上限 100 节点")


@router.get("/chat", response_class=HTMLResponse, summary="AI 助手")
def page_chat(request: Request):
    from app.config import settings
    return _render(request, "pages/chat.html", "chat",
                   page_title="AI 风控助手",
                   page_subtitle="自然语言查风险分析、案件与规则效果 · 会话持久化至 Redis",
                   llm_model=settings.LLM_MODEL_NAME)


# ---------------- 系统 ----------------
@router.get("/audit", response_class=HTMLResponse, summary="操作审计日志")
def page_audit(request: Request):
    return _render(request, "pages/audit.html", "audit",
                   page_title="操作审计日志",
                   page_subtitle="规则 / 名单 / 案件 / 配置 四类操作 · 仅追加不可篡改")


@router.get("/settings", response_class=HTMLResponse, summary="系统配置与健康度")
def page_settings(request: Request):
    return _render(request, "pages/settings.html", "settings",
                   page_title="系统配置与健康度",
                   page_subtitle="阈值在线调整 · 缓存与降级策略 · 性能健康度 · 运维任务")


@router.get("/dashboard/loss", response_class=HTMLResponse, summary="资损防护（嵌入仪表盘）")
def page_loss(request: Request):
    """资损防护区块 —— 由仪表盘页以 iframe / fetch 方式复用，也可独立访问"""
    return _render(request, "pages/loss.html", "dashboard",
                   page_title="资损防护",
                   page_subtitle="被拦截案件的金额合计")
