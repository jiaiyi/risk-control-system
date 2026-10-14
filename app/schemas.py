# -*- coding: utf-8 -*-
"""接口层请求 / 响应模型（Pydantic 2）

约定（SPEC 8 非功能设计）：
    · 全部请求经 Pydantic 校验（NFR-09）
    · 枚举用 `Literal` 约束，数值用 `Field(ge, le)` 约束
    · 敏感配置不落代码（NFR-07，由 `app/config.py` 从 .env 加载）

说明：响应体多数直接用服务层的 `to_dict()`（如 `EventProcessResult` 已对齐 SPEC 6.2 API-01），
这里只定义**需要校验的请求体**与少数结构化响应。
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ============================================================
# 枚举（与 DB 枚举保持一致，改这里要同步 app/models.py）
# ============================================================
EventType = Literal["登录", "营销领券", "下单", "支付", "售后申请", "物流投诉"]
ListType = Literal["黑名单", "白名单", "灰名单"]
EntityType = Literal["用户", "手机号", "IP", "设备", "地址"]
RiskLevel = Literal["低", "中", "高", "极高"]
RuleAction = Literal["通过", "标记", "人工审核", "二次验证", "拒绝"]
RuleCategory = Literal[
    "订单欺诈", "支付风险", "账户风险", "售后滥用", "地址风险",
    "物流风险", "登录安全", "营销薅羊毛", "黄牛囤货", "聚集风险",
]
RuleEventType = Literal["登录", "营销领券", "下单", "支付", "售后申请", "物流投诉", "通用"]
CaseStatus = Literal["待审核", "审核中", "已通过", "已拒绝", "已关闭"]
CaseDecision = Literal["已通过", "已拒绝"]


# ============================================================
# API-01 实时风险检查
# ============================================================
class RiskCheckRequest(BaseModel):
    event_type: EventType
    source_id: str = Field(..., max_length=50, description="关联业务 ID")
    user_id: str = Field(..., max_length=50, description="用户 ID，必须存在")
    order_id: Optional[str] = Field(None, max_length=50, description="未传时按事件类型自动推导")
    receive_id: Optional[str] = Field(None, max_length=50, description="未传时从订单反查")
    event_data: Optional[dict] = Field(None, description="事件上下文（设备、IP 等）")


# ============================================================
# API-02~08 规则管理
# ============================================================
class RuleCreateRequest(BaseModel):
    rule_id: Optional[str] = Field(None, max_length=50, description="不传则自动生成")
    rule_name: str = Field(..., max_length=100)
    rule_category: RuleCategory
    event_type: RuleEventType = "通用"
    rule_condition: dict = Field(..., description="条件表达式（结构化 JSON）")
    risk_level: RiskLevel
    risk_score: int = Field(..., ge=0, le=100, description="命中分值 0-100")
    action: RuleAction
    priority: int = Field(0, ge=0, le=999)
    description: Optional[str] = None
    is_enabled: int = Field(1, ge=0, le=1)


class RuleUpdateRequest(BaseModel):
    """部分更新（FR-10）：只传要改的字段"""

    rule_name: Optional[str] = Field(None, max_length=100)
    rule_category: Optional[RuleCategory] = None
    event_type: Optional[RuleEventType] = None
    rule_condition: Optional[dict] = None
    risk_level: Optional[RiskLevel] = None
    risk_score: Optional[int] = Field(None, ge=0, le=100)
    action: Optional[RuleAction] = None
    priority: Optional[int] = Field(None, ge=0, le=999)
    description: Optional[str] = None
    operator: str = "admin"


class RuleToggleRequest(BaseModel):
    is_enabled: int = Field(..., ge=0, le=1)
    operator: str = "admin"


class RuleConditionValidateRequest(BaseModel):
    """API-08：只校验条件表达式结构，不落库"""

    rule_condition: dict


# ============================================================
# API-09~12 案件管理
# ============================================================
class CaseReviewRequest(BaseModel):
    decision: CaseDecision
    reviewer: str = Field(..., max_length=50)
    review_comment: Optional[str] = None
    add_to_blacklist: bool = False
    blacklist_entity_types: Optional[list[EntityType]] = Field(
        None, description="不传时默认只加「用户」")
    blacklist_reason: Optional[str] = None


# ============================================================
# API-13~17 名单管理
# ============================================================
class ListItemCreateRequest(BaseModel):
    list_type: ListType
    entity_type: EntityType
    entity_value: str = Field(..., max_length=200)
    reason: Optional[str] = None
    expire_time: Optional[datetime] = Field(
        None, description="白名单必填且不超 90 天；灰名单缺省为 30 天；黑名单为空表示永久")
    created_by: Optional[str] = None
    force: bool = Field(False, description="白名单超 90 天时的二次确认标记")


# ============================================================
# API-23~25 规则仿真
# ============================================================
class SimulationRunRequest(BaseModel):
    event_type: EventType
    user_id: str = Field(..., max_length=50)
    source_id: Optional[str] = Field(None, max_length=50)
    order_id: Optional[str] = Field(None, max_length=50)
    receive_id: Optional[str] = Field(None, max_length=50)
    device_id: Optional[str] = Field(None, max_length=100)
    login_ip: Optional[str] = Field(None, max_length=50)
    event_data: Optional[dict] = None
    graylist_hit: bool = Field(False, description="模拟命中灰名单，验证加严效果")


class SimulationReplayRequest(BaseModel):
    event_type: Optional[EventType] = None
    limit: int = Field(50, ge=1, le=500)
    rule_id: Optional[str] = Field(None, description="只看某条规则的命中情况")
    include_detail: bool = False


# ============================================================
# API-27 二次验证
# ============================================================
class ChallengeVerifyRequest(BaseModel):
    verify_token: str = Field(..., max_length=100, description="验证凭据")
    verify_code: Optional[str] = Field(None, description="模拟验证码通道，占位")


# ============================================================
# API-46~52 系统配置与健康度
# ============================================================
class ThresholdUpdateRequest(BaseModel):
    """在线调整决策阈值与引擎参数（写审计、记录前后值 —— FR-56）"""

    RISK_PASS_THRESHOLD: Optional[int] = Field(None, ge=0, le=100)
    RISK_MARK_THRESHOLD: Optional[int] = Field(None, ge=0, le=100)
    RISK_REVIEW_THRESHOLD: Optional[int] = Field(None, ge=0, le=100)
    RISK_MULTI_RULE_BONUS: Optional[int] = Field(None, ge=0, le=20)
    RISK_VETO_MIN_SCORE: Optional[int] = Field(None, ge=0, le=100)
    RISK_GRAYLIST_BONUS: Optional[int] = Field(None, ge=0, le=50)
    PROFILE_ALPHA_HIGH: Optional[float] = Field(None, ge=0, le=1)
    PROFILE_ALPHA_MID: Optional[float] = Field(None, ge=0, le=1)
    PROFILE_ALPHA_LOW: Optional[float] = Field(None, ge=0, le=1)
    PROFILE_ACTIVE_THRESHOLD: Optional[int] = Field(None, ge=0, le=100)
    operator: str = "admin"


class CachePolicyUpdateRequest(BaseModel):
    REDIS_ENABLED: Optional[bool] = None
    CACHE_TTL_FEATURE: Optional[int] = Field(None, ge=0, le=86400)
    CACHE_TTL_PROFILE: Optional[int] = Field(None, ge=0, le=86400)
    CACHE_TTL_GRAPH: Optional[int] = Field(None, ge=0, le=86400)
    CACHE_TTL_RULE: Optional[int] = Field(None, ge=0, le=86400)
    operator: str = "admin"


# ============================================================
# API-28~29 AI 助手
# ============================================================
class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    session_id: Optional[str] = Field(None, max_length=100,
                                      description="会话 ID；不传则新建（首轮可省略）")


class AgentClearRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
