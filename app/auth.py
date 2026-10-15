# -*- coding: utf-8 -*-
"""
鉴权与角色权限（RBAC）

> 背景：`specs/PRD.md:110` 原文声明「**本期不实现登录鉴权（RBAC）**，角色用于界定
> 功能归属与操作规范，不做系统级权限拦截」，但用户明确要求补上登录页与角色拦截。
> 所以本模块是**超出 PRD 本期范围的新增项**，权限划分严格照抄 `specs/PRD.md:91-108`
> 的权限矩阵，不自行发明。

设计取舍：

1. **会话存 Redis**（键 `risk:auth:sess:{token}`，TTL `settings.AUTH_SESSION_TTL_SECONDS`）
   —— 沿用 `app/cache.py` 的键命名规范 `risk:<域>:<维度>:<标识>`。

2. **刻意不用 `app/cache.py` 的 `safe_*` 系列**。那套方法把 Redis 异常一律吞成
   「未命中」，对旁路缓存是对的（缓存挂了就直查库），但用在鉴权上是**危险**的：
   Redis 故障会被伪装成「会话不存在」，而更糟的实现会顺手放行。所以这里自己拿
   Redis 连接，**Redis 不可用时抛 `AuthBackendError`（500）而不是放行** ——
   鉴权系统故障必须显式失败（fail-closed），不能默认通过。

3. **账号写在配置里而非建表**。不新增数据库表、不动 `sql/` 下 4 个初始化脚本，
   避免污染既有 29 张表结构。演示账号密码以 PBKDF2-HMAC-SHA256 加盐存哈希，
   不存明文（虽然是演示账号，但把明文口令写进代码是坏习惯）。

4. **权限码用 `view` / `manage` 两档粒度**，不引入 `resource:action:scope` 三元组。
   矩阵里只有「只读」和「可改」两种区分，过度设计反而难维护。
"""
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import redis
from fastapi import Request, Response

from app.config import settings

logger = logging.getLogger(__name__)

# ============================================================
# 会话
# ============================================================
K_AUTH_SESSION = "risk:auth:sess"       # risk:auth:sess:<token>
SESSION_COOKIE = "rc_session"

_auth_client: "redis.Redis | None" = None
_auth_client_ready = False


class AuthBackendError(Exception):
    """鉴权后端（Redis）不可用。

    **这是 fail-closed 的**：抛出去让请求失败（500），而不是当作「未登录」或
    「已登录」放行。把 Redis 故障当成放行，等于鉴权在最需要它的时候失效。
    """


def _client() -> "redis.Redis | None":
    """鉴权专用的 Redis 连接。返回 None 表示配置层面就禁用了 Redis。"""
    global _auth_client, _auth_client_ready
    if not settings.REDIS_ENABLED:
        return None
    if _auth_client_ready:
        return _auth_client
    _auth_client_ready = True
    try:
        c = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        c.ping()
        _auth_client = c
        logger.info("鉴权会话后端已连接: %s", settings.get_redis_url())
    except Exception as exc:                      # noqa: BLE001
        logger.error("鉴权会话后端不可用：%s", exc)
        _auth_client = None
    return _auth_client


def reset_client() -> None:
    """丢弃缓存的连接，下次调用重新连接（供测试与运行期自愈使用）"""
    global _auth_client, _auth_client_ready
    _auth_client = None
    _auth_client_ready = False


# ============================================================
# 角色与权限矩阵（严格对齐 specs/PRD.md:91-108）
# ============================================================
ROLE_STRATEGIST = "风控策略师"
ROLE_REVIEWER = "风控审核员"
ROLE_ANALYST = "数据分析师"
ROLE_ADMIN = "系统管理员"
ROLE_AUDITOR = "审计人员"

ROLES = (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST, ROLE_ADMIN, ROLE_AUDITOR)

# 权限码 → 允许的角色
PERMISSIONS: dict[str, tuple[str, ...]] = {
    # 矩阵首行：5 个角色都能看仪表盘
    "dashboard:view":  ROLES,

    # 规则管理：策略师可改；审计人员只读（其余角色无权）
    "rules:view":      (ROLE_STRATEGIST, ROLE_AUDITOR),
    "rules:manage":    (ROLE_STRATEGIST,),

    # 规则仿真回放：策略师 / 审核员 / 分析师
    "simulation:run":  (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST),

    # 案件：策略师与审核员可看，分析师与审计人员只读
    "cases:view":      (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST, ROLE_AUDITOR),
    "cases:review":    (ROLE_REVIEWER,),

    # 名单：策略师可管；审计人员只读
    "lists:view":      (ROLE_STRATEGIST, ROLE_AUDITOR),
    "lists:manage":    (ROLE_STRATEGIST,),
    "lists:import":    (ROLE_STRATEGIST,),

    # 用户画像 / 关联图谱
    "profiles:view":   (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST, ROLE_AUDITOR),
    "graph:view":      (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST, ROLE_AUDITOR),

    # 即时风险检查 / AI 助手：策略师 / 审核员 / 分析师
    "risk:check":      (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST),
    "agent:use":       (ROLE_STRATEGIST, ROLE_REVIEWER, ROLE_ANALYST),

    # 审计日志：策略师 / 系统管理员 / 审计人员
    "audit:view":      (ROLE_STRATEGIST, ROLE_ADMIN, ROLE_AUDITOR),

    # 系统配置：仅系统管理员可改；**审计人员只读**（PRD.md:107 明写「只读」，
    # 所以读权限必须与写权限分开，否则审计人员连阈值是多少都看不到）
    "settings:view":   (ROLE_ADMIN, ROLE_AUDITOR),
    "settings:manage": (ROLE_ADMIN,),

    # 性能与健康度监控：仅系统管理员（PRD.md:108 审计人员是「—」，无只读）
    "health:view":     (ROLE_ADMIN,),
}


def role_permissions(role: str) -> frozenset[str]:
    """某角色拥有的全部权限码"""
    return frozenset(p for p, roles in PERMISSIONS.items() if role in roles)


# ============================================================
# 账号
# ============================================================
@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: str
    initial: str                 # 头像字
    title: str = ""

    @property
    def permissions(self) -> frozenset[str]:
        return role_permissions(self.role)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role,
            "initial": self.initial,
            "permissions": sorted(self.permissions),
        }


# 演示口令：仅在 README 与登录页提示中公开，用于本地演示。
# 生产环境应当改为从环境变量或密钥管理系统读取。
_DEMO_PASSWORD = "risk@2026"

# 账号定义：(user_id, 姓名, 角色, 头像字, 岗位说明)
_ACCOUNT_SPECS = (
    ("zhangcx", "张晨曦", ROLE_STRATEGIST, "张", "风控策略与名单库负责人"),
    ("shenhey", "沈核言", ROLE_REVIEWER, "沈", "中高风险案件复核"),
    ("fengxs", "冯析数", ROLE_ANALYST, "冯", "风控效果与趋势分析"),
    ("yunwq", "云维清", ROLE_ADMIN, "云", "运行配置与健康度"),
    ("shenjr", "慎计仁", ROLE_AUDITOR, "慎", "操作合规独立核查"),
)

_PBKDF2_ROUNDS = 120_000


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return dk.hex()


@lru_cache(maxsize=1)
def _accounts() -> dict[str, tuple[User, str, bytes]]:
    """构建账号表：user_id → (User, 口令哈希, 盐)

    口令用固定盐的 PBKDF2 存哈希。同口令同盐意味着哈希相同，但这里只是为了
    「代码里不出现明文口令」，并不追求抵抗彩虹表 —— 演示系统的定位如此。
    """
    salt = b"rc-demo-static-salt"
    pw_hash = _hash_password(_DEMO_PASSWORD, salt)
    out: dict[str, tuple[User, str, bytes]] = {}
    for uid, name, role, initial, title in _ACCOUNT_SPECS:
        out[uid] = (User(uid, name, role, initial, title), pw_hash, salt)
    return out


def all_users() -> list[User]:
    return [u for u, _, _ in _accounts().values()]


def verify_credentials(user_id: str, password: str) -> Optional[User]:
    """校验账号口令。账号不存在与口令错误返回同一个 None，不泄露账号是否存在。"""
    rec = _accounts().get((user_id or "").strip())
    if not rec:
        # 即使账号不存在也做一次哈希，避免用响应时间区分「账号不存在」与「口令错」
        _hash_password(password or "", b"rc-demo-static-salt")
        return None
    user, pw_hash, salt = rec
    if hmac.compare_digest(_hash_password(password or "", salt), pw_hash):
        return user
    return None


# ============================================================
# 会话读写
# ============================================================
def create_session(user: User) -> str:
    """建立会话，返回 token（写入 Cookie）"""
    token = secrets.token_urlsafe(32)
    client = _client()
    if client is None:
        raise AuthBackendError("鉴权会话后端不可用，无法登录")
    try:
        client.setex(
            f"{K_AUTH_SESSION}:{token}",
            settings.AUTH_SESSION_TTL_SECONDS,
            user.user_id,
        )
    except Exception as exc:                      # noqa: BLE001
        raise AuthBackendError(f"会话写入失败：{exc}") from exc
    return token


def get_session(token: Optional[str]) -> Optional[User]:
    """按 token 取用户；token 无效返回 None。Redis 故障抛 AuthBackendError。"""
    if not token:
        return None
    client = _client()
    if client is None:
        raise AuthBackendError("鉴权会话后端不可用，无法校验登录状态")
    try:
        user_id = client.get(f"{K_AUTH_SESSION}:{token}")
    except Exception as exc:                      # noqa: BLE001
        raise AuthBackendError(f"会话读取失败：{exc}") from exc
    if not user_id:
        return None
    rec = _accounts().get(user_id)
    if not rec:
        return None
    user = rec[0]
    # 滑动续期：活跃用户不会中途被踢，闲置超过 TTL 才失效
    try:
        client.expire(f"{K_AUTH_SESSION}:{token}", settings.AUTH_SESSION_TTL_SECONDS)
    except Exception:                             # noqa: BLE001
        pass                                      # 续期失败不影响本次请求
    return user


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    client = _client()
    if client is None:
        return
    try:
        client.delete(f"{K_AUTH_SESSION}:{token}")
    except Exception as exc:                      # noqa: BLE001
        logger.warning("会话删除失败：%s", exc)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.AUTH_SESSION_TTL_SECONDS,
        httponly=True,        # JS 读不到，降低 XSS 窃取会话的风险
        samesite="lax",       # 缓解 CSRF：跨站 POST 不携带
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


# ============================================================
# 请求级取用户 / FastAPI 依赖
# ============================================================
def current_user(request: Request) -> Optional[User]:
    """从请求 Cookie 解析当前用户；未登录返回 None"""
    return get_session(request.cookies.get(SESSION_COOKIE))


def operator_of(request: Request, fallback: str = "system") -> str:
    """审计日志的「操作人」口径。

    PRD.md:110 说审计表要预留「操作人」字段，「待鉴权落地后自动生效」——
    鉴权落地了，这里就是那个「自动生效」的接缝：统一从登录会话取
    `姓名(user_id)`，取不到才回落 fallback。

    格式统一成 `张晨曦(zhangcx)` 而不是只写 user_id，因为审计日志是给人看的，
    只写 `zhangcx` 还得再查一次才知道是谁。
    """
    try:
        user = getattr(request.state, "user", None) or current_user(request)
    except Exception:                             # noqa: BLE001  鉴权后端故障不该让写操作 500
        user = None
    if user is None:
        return fallback
    return f"{user.name}({user.user_id})"


def require_permission(permission: str):
    """FastAPI 依赖工厂：要求当前用户拥有指定权限。

    401 = 没登录（该去登录页）；403 = 登录了但角色不够（不该重试登录）。
    这两种必须分开，否则前端无法判断该跳登录还是该提示无权限。
    """
    from app.errors import AuthForbiddenError, AuthRequiredError

    def dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise AuthRequiredError(f"未登录或登录已过期，需要权限：{permission}")
        if not user.has(permission):
            raise AuthForbiddenError(
                f"当前角色「{user.role}」无权执行该操作（需要权限：{permission}）",
                detail={"role": user.role, "required": permission},
            )
        return user

    return dep
