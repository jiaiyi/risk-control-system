"""
电商风控系统 - ORM 模型定义

共 29 张表：
  · 业务侧 19 张（只读映射，不在本项目中写入）
  · 风控侧 10 张（读写）

业务表结构沿用尚硅谷参考实现，保证与既有数据生成脚本兼容；
风控表按 specs/SPEC.md 5.3 / 5.4 定义，相对参考实现的变更见文件末尾注释。
"""
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# ============================================================
# 第一部分：业务表 ORM（19 张，只读）
# ============================================================


class UserInfo(Base):
    """用户信息表"""
    __tablename__ = "user_info"

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="用户ID")


class Region(Base):
    """地区表（复合主键）"""
    __tablename__ = "region"

    province: Mapped[str] = mapped_column(String(20), primary_key=True, comment="省")
    city: Mapped[str] = mapped_column(String(20), primary_key=True, comment="市")
    district: Mapped[str] = mapped_column(String(20), primary_key=True, comment="区")


class ReceiveInfo(Base):
    """收货信息表"""
    __tablename__ = "receive_info"

    receive_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="收货信息ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    receiver_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货人姓名")
    receiver_phone: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货人手机")
    receive_province: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货省")
    receive_city: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货市")
    receive_district: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货区")
    receive_street_address: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货详细地址")


class ProductCategory(Base):
    """商品分类表"""
    __tablename__ = "product_category"

    product_category: Mapped[str] = mapped_column(String(20), primary_key=True, comment="商品类别")


class SkuInfo(Base):
    """商品信息表"""
    __tablename__ = "sku_info"

    sku_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="商品ID")
    sku_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="商品名称")
    sku_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, comment="商品价格")
    sku_category: Mapped[str] = mapped_column(String(20), nullable=False, comment="商品类别")
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="商品库存")


class OrderStatus(Base):
    """订单状态表"""
    __tablename__ = "order_status"

    order_status: Mapped[str] = mapped_column(String(20), primary_key=True, comment="订单状态")
    status_code: Mapped[Optional[int]] = mapped_column(Integer, comment="状态码")


class OrderInfo(Base):
    """订单表"""
    __tablename__ = "order_info"

    order_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="订单ID")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建时间")
    payment_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="支付时间")
    delivered_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="签收时间")
    complete_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="完成时间")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    receive_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货信息ID")
    order_status: Mapped[str] = mapped_column(String(20), nullable=False, comment="订单状态")


class OrderDetail(Base):
    """订单详细表"""
    __tablename__ = "order_detail"

    order_detail_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="订单详细ID")
    order_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="订单ID")
    sku_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="商品ID")
    sku_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="商品名称")
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="商品数量")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, comment="总单价格")
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, comment="折扣金额")
    final_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, comment="实付金额")


class Logistics(Base):
    """物流表"""
    __tablename__ = "logistics"

    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="物流ID")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建时间")
    delivered_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="签收时间")
    logistics_tracking: Mapped[Optional[str]] = mapped_column(String(500), comment="物流详细")
    logistics_category: Mapped[Optional[str]] = mapped_column(
        Enum("退货", "换货退货", "换货发货", name="logistics_category_enum"), comment="物流类别",
    )


class OrderLogistics(Base):
    """订单物流关联关系表（复合主键）"""
    __tablename__ = "order_logistics"

    order_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="订单ID")
    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="物流ID")


class LogisticsCompany(Base):
    """物流公司表"""
    __tablename__ = "logistics_company"

    company_name: Mapped[str] = mapped_column(String(20), primary_key=True, comment="物流公司名称")


class LogisticsComplaint(Base):
    """物流投诉内容对照表（复合主键）"""
    __tablename__ = "logistics_complaint"

    logistics_status: Mapped[str] = mapped_column(String(20), primary_key=True, comment="物流状态")
    logistics_complaint: Mapped[str] = mapped_column(String(100), primary_key=True, comment="物流投诉问题")


class LogisticsComplaintsRecord(Base):
    """物流投诉记录表"""
    __tablename__ = "logistics_complaints_record"

    record_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="投诉记录ID")
    logistics_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="物流ID")
    logistics_complaint: Mapped[str] = mapped_column(String(500), nullable=False, comment="物流投诉问题")
    complaint_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="投诉时间")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")


class PostsaleStatus(Base):
    """售后状态表"""
    __tablename__ = "postsale_status"

    postsale_status: Mapped[str] = mapped_column(String(20), primary_key=True, comment="售后状态")
    is_refund: Mapped[int] = mapped_column(Integer, nullable=False, comment="是否退款")
    is_return: Mapped[int] = mapped_column(Integer, nullable=False, comment="是否退货")
    is_exchange: Mapped[int] = mapped_column(Integer, nullable=False, comment="是否换货")
    status_code: Mapped[Optional[int]] = mapped_column(Integer, comment="状态码")


class PostsaleReason(Base):
    """售后原因表"""
    __tablename__ = "postsale_reason"

    postsale_reason: Mapped[str] = mapped_column(String(100), primary_key=True, comment="售后原因")
    product_category: Mapped[Optional[str]] = mapped_column(String(20), comment="商品类别")


class Postsale(Base):
    """售后表"""
    __tablename__ = "postsale"

    postsale_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="售后ID")
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建时间")
    complete_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="完成时间")
    order_detail_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="订单详细ID")
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, comment="退款金额")
    postsale_type: Mapped[Optional[str]] = mapped_column(
        Enum("退款", "退货", "换货", name="postsale_type_enum"), comment="售后类型",
    )
    postsale_reason: Mapped[str] = mapped_column(String(500), nullable=False, comment="售后原因")
    postsale_status: Mapped[str] = mapped_column(String(20), nullable=False, comment="售后状态")
    receive_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="收货信息ID")


class PostsaleLogistics(Base):
    """售后物流关联关系表（复合主键）"""
    __tablename__ = "postsale_logistics"

    postsale_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="售后ID")
    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="物流ID")


# ---- 本期新增的 2 张业务表（登录 / 营销领券事件的数据底座）----


class UserLoginRecord(Base):
    """用户登录记录表"""
    __tablename__ = "user_login_record"

    login_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="登录记录ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    login_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="登录时间")
    device_id: Mapped[Optional[str]] = mapped_column(String(100), comment="设备标识")
    login_ip: Mapped[Optional[str]] = mapped_column(String(50), comment="登录IP")
    login_city: Mapped[Optional[str]] = mapped_column(String(50), comment="登录城市")
    login_result: Mapped[str] = mapped_column(
        Enum("成功", "失败", name="login_result_enum"), nullable=False, server_default="成功", comment="登录结果",
    )

    __table_args__ = (
        Index("idx_login_user_time", "user_id", "login_time"),
        Index("idx_login_device_time", "device_id", "login_time"),
        Index("idx_login_ip_time", "login_ip", "login_time"),
    )


class CouponReceiveRecord(Base):
    """优惠券领取记录表"""
    __tablename__ = "coupon_receive_record"

    receive_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="领取记录ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    coupon_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="券标识")
    coupon_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="券种")
    coupon_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0, comment="券面金额")
    receive_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="领取时间")
    device_id: Mapped[Optional[str]] = mapped_column(String(100), comment="设备标识")
    use_status: Mapped[str] = mapped_column(
        Enum("未使用", "已使用", "已过期", "已作废", name="coupon_use_status_enum"),
        nullable=False, server_default="未使用", comment="使用状态",
    )
    use_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="使用时间")

    __table_args__ = (
        Index("idx_coupon_user_time", "user_id", "receive_time"),
        Index("idx_coupon_device_time", "device_id", "receive_time"),
        Index("idx_coupon_type_time", "coupon_type", "receive_time"),
    )


# ============================================================
# 第二部分：风控表 ORM（9 张，读写）
# ============================================================


class RiskRule(Base):
    """风控规则配置表"""
    __tablename__ = "risk_rule"

    rule_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="规则ID")
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="规则名称")
    rule_category: Mapped[str] = mapped_column(
        Enum("订单欺诈", "支付风险", "账户风险", "售后滥用", "地址风险",
             "物流风险", "登录安全", "营销薅羊毛", "黄牛囤货", "聚集风险",
             name="rule_category_enum"),
        nullable=False, comment="风险场景分类",
    )
    event_type: Mapped[str] = mapped_column(
        Enum("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉", "通用",
             name="rule_event_type_enum"),
        nullable=False, server_default="通用", comment="适用事件类型",
    )
    rule_condition: Mapped[str] = mapped_column(Text, nullable=False, comment="条件表达式(JSON)")
    risk_level: Mapped[str] = mapped_column(
        Enum("低", "中", "高", "极高", name="risk_level_enum"), nullable=False, comment="风险等级",
    )
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, comment="命中分值(0-100)")
    action: Mapped[str] = mapped_column(
        Enum("通过", "标记", "人工审核", "二次验证", "拒绝", name="rule_action_enum"),
        nullable=False, comment="触发动作",
    )
    is_enabled: Mapped[int] = mapped_column(Integer, default=1, comment="是否启用")
    priority: Mapped[int] = mapped_column(Integer, default=0, comment="优先级(越高越先执行)")
    is_deleted: Mapped[int] = mapped_column(Integer, default=0, comment="逻辑删除标记(0正常 1已删除)")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="规则描述")
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")
    update_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        Index("idx_rule_enabled_priority", "is_enabled", "is_deleted", "priority"),
    )

    @property
    def condition_dict(self) -> dict:
        """将 JSON 字符串形式的 rule_condition 解析为字典"""
        if isinstance(self.rule_condition, str):
            return json.loads(self.rule_condition)
        return self.rule_condition


class RiskEvent(Base):
    """风控事件审计表"""
    __tablename__ = "risk_event"

    event_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="事件ID")
    event_type: Mapped[str] = mapped_column(
        Enum("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉", name="event_type_enum"),
        nullable=False, comment="事件类型",
    )
    event_source_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="关联业务ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    event_data: Mapped[Optional[str]] = mapped_column(Text, comment="事件快照(JSON)")
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_risk_event_user_id", "user_id"),
        Index("idx_risk_event_create_time", "create_time"),
    )


class RiskFeature(Base):
    """风控特征快照表

    entity_type 使用字符串而非数据库枚举 —— 特征维度已达 9 个
    （用户/订单/地址/登录/领券/设备/IP/商品/手机号），
    新增维度只需在应用层映射字典中加一行，无需改表结构（SPEC Q6 决策）。
    """
    __tablename__ = "risk_feature"

    feature_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="特征ID")
    event_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="关联事件ID")
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="实体类型")
    entity_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="实体ID")
    feature_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="特征名称")
    feature_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 4), comment="特征值")
    compute_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="计算时间")

    __table_args__ = (
        Index("idx_risk_feature_event_id", "event_id"),
        Index("idx_risk_feature_entity", "entity_type", "entity_id"),
    )


class RiskAssessment(Base):
    """风控评估结果表"""
    __tablename__ = "risk_assessment"

    assessment_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="评估ID")
    event_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="关联事件ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    rule_results: Mapped[Optional[str]] = mapped_column(Text, comment="规则命中明细(JSON)")
    rule_count: Mapped[int] = mapped_column(Integer, default=0, comment="命中规则数")
    final_score: Mapped[int] = mapped_column(Integer, nullable=False, comment="最终评分(0-100)")
    risk_level: Mapped[str] = mapped_column(
        Enum("低", "中", "高", "极高", name="assessment_risk_level_enum"), nullable=False, comment="风险等级",
    )
    decision: Mapped[str] = mapped_column(
        Enum("通过", "标记", "人工审核", "二次验证", "拒绝", name="assessment_decision_enum"),
        nullable=False, comment="决策",
    )
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_risk_assessment_user_id", "user_id"),
        Index("idx_risk_assessment_decision", "decision"),
        Index("idx_risk_assessment_create_time", "create_time"),
    )


class RiskCase(Base):
    """风控案件表"""
    __tablename__ = "risk_case"

    case_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="案件ID")
    assessment_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="关联评估ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    case_status: Mapped[str] = mapped_column(
        Enum("待审核", "审核中", "已通过", "已拒绝", "已关闭", name="case_status_enum"),
        default="待审核", comment="案件状态",
    )
    case_category: Mapped[Optional[str]] = mapped_column(String(50), comment="案件分类")
    conflict_flag: Mapped[int] = mapped_column(Integer, default=0, comment="是否名单冲突案件(0否 1是)")
    risk_detail: Mapped[Optional[str]] = mapped_column(Text, comment="风险详情(JSON)")
    reviewer: Mapped[Optional[str]] = mapped_column(String(50), comment="审核人")
    review_comment: Mapped[Optional[str]] = mapped_column(Text, comment="审核意见")
    review_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="审核时间")
    # --- 业务回溯字段（「重做检查」用一个按钮从案件跳回风控检查页）---
    source_id: Mapped[Optional[str]] = mapped_column(String(50), comment="原始业务ID(订单/售后/投诉ID)")
    event_type: Mapped[Optional[str]] = mapped_column(
        Enum("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉", name="case_event_type_enum"),
        comment="触发案件的事件类型",
    )
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")
    update_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        Index("idx_risk_case_status", "case_status"),
        Index("idx_risk_case_user_id", "user_id"),
        Index("idx_risk_case_conflict", "conflict_flag"),
    )


class RiskList(Base):
    """风控名单表（三色 × 五类主体）

    用法：
      · 黑名单 — 命中直接拒绝（评分 100）
      · 白名单 — 命中直接放行（评分 0）
      · 灰名单 — 命中不拦截，但对最终评分加严（RISK_GRAYLIST_BONUS）
      · 黑白同时命中 → 移交人工审核（不自动裁决）
    """
    __tablename__ = "risk_list"

    list_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="名单ID")
    list_type: Mapped[str] = mapped_column(
        Enum("黑名单", "白名单", "灰名单", name="list_type_enum"), nullable=False, comment="名单类型",
    )
    entity_type: Mapped[str] = mapped_column(
        Enum("用户", "手机号", "IP", "设备", "地址", name="list_entity_type_enum"),
        nullable=False, comment="主体类型",
    )
    entity_value: Mapped[str] = mapped_column(String(200), nullable=False, comment="主体值")
    reason: Mapped[Optional[str]] = mapped_column(Text, comment="加入原因/准入凭据")
    expire_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="过期时间(NULL=永久)")
    created_by: Mapped[Optional[str]] = mapped_column(String(50), comment="操作人")
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        UniqueConstraint("list_type", "entity_type", "entity_value", name="uk_list_type_entity"),
        Index("idx_list_lookup", "entity_type", "entity_value", "expire_time"),
    )


class RiskUserProfile(Base):
    """用户风险画像表"""
    __tablename__ = "risk_user_profile"

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="用户ID")
    risk_score: Mapped[int] = mapped_column(Integer, default=0, comment="综合风险评分(0-100)")
    risk_level: Mapped[str] = mapped_column(
        Enum("低", "中", "高", "极高", name="profile_risk_level_enum"), default="低", comment="风险等级",
    )
    total_orders: Mapped[int] = mapped_column(Integer, default=0, comment="总订单数")
    total_refunds: Mapped[int] = mapped_column(Integer, default=0, comment="退款次数")
    refund_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=0, comment="退款率")
    avg_order_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, comment="平均订单金额")
    address_count: Mapped[int] = mapped_column(Integer, default=0, comment="地址数量")
    complaint_count: Mapped[int] = mapped_column(Integer, default=0, comment="投诉次数")
    assessment_count: Mapped[int] = mapped_column(Integer, default=0, comment="评估次数")
    last_assessment_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最近评估时间")
    profile_data: Mapped[Optional[str]] = mapped_column(Text, comment="扩展画像数据(JSON)")
    update_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")


class RiskAuditLog(Base):
    """操作审计流水表（仅追加，不提供更新/删除接口）

    覆盖规则 / 名单 / 案件 / 配置四类关键操作，记录变更前后值。
    """
    __tablename__ = "risk_audit_log"

    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="日志ID")
    operator: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作人")
    action_type: Mapped[str] = mapped_column(
        Enum("规则", "名单", "案件", "配置", name="audit_action_type_enum"), nullable=False, comment="操作类别",
    )
    action_desc: Mapped[str] = mapped_column(String(100), nullable=False, comment="操作描述")
    target_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作对象ID")
    before_value: Mapped[Optional[str]] = mapped_column(Text, comment="变更前值(JSON)")
    after_value: Mapped[Optional[str]] = mapped_column(Text, comment="变更后值(JSON)")
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="操作时间")

    __table_args__ = (
        Index("idx_audit_type_time", "action_type", "create_time"),
        Index("idx_audit_target", "target_id"),
        Index("idx_audit_operator", "operator", "create_time"),
    )


class RiskChallenge(Base):
    """二次验证记录表

    对应决策档位中的「二次验证」（Challenge）：命中动作配置为「二次验证」的规则时，
    不直接放行也不直接拦截，而是生成待验证记录阻断当前操作；
    超时未验证则升级为人工审核。
    """
    __tablename__ = "risk_challenge"

    challenge_id: Mapped[str] = mapped_column(String(50), primary_key=True, comment="验证ID")
    assessment_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="关联评估ID")
    user_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="用户ID")
    verify_token: Mapped[str] = mapped_column(String(100), nullable=False, comment="验证凭据(模拟通道占位)")
    status: Mapped[str] = mapped_column(
        Enum("待验证", "已验证", "已超时", name="challenge_status_enum"),
        nullable=False, server_default="待验证", comment="验证状态",
    )
    expire_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="验证过期时间")
    create_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间")

    __table_args__ = (
        Index("idx_challenge_status", "status", "expire_time"),
        Index("idx_challenge_assessment", "assessment_id"),
    )


class RiskListArchive(Base):
    """名单归档表（FR-42）

    过期名单清理前的落脚点。与 `risk_audit_log` 的区别：
    审计流水是「操作留痕」（谁在何时做了什么），本表是「数据归档」——
    按主体维度可**独立检索历史名单**（例如「某设备历史上被拉黑过几次」）。

    不设唯一约束：同一主体可被反复加入 / 移除，产生多条归档记录。
    `original_list_id` 保留原表主键，便于与原表对照追溯。
    """
    __tablename__ = "risk_list_archive"

    archive_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="归档ID")
    original_list_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="原名单ID")
    list_type: Mapped[str] = mapped_column(
        Enum("黑名单", "白名单", "灰名单", name="archive_list_type_enum"),
        nullable=False, comment="名单类型",
    )
    entity_type: Mapped[str] = mapped_column(
        Enum("用户", "手机号", "IP", "设备", "地址", name="archive_entity_type_enum"),
        nullable=False, comment="主体类型",
    )
    entity_value: Mapped[str] = mapped_column(String(200), nullable=False, comment="主体值")
    reason: Mapped[Optional[str]] = mapped_column(Text, comment="原加入原因/准入凭据")
    expire_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="原过期时间")
    created_by: Mapped[Optional[str]] = mapped_column(String(50), comment="原操作人")
    create_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="原创建时间")
    archive_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="归档时间")
    archived_by: Mapped[Optional[str]] = mapped_column(String(50), comment="归档触发者")
    archive_reason: Mapped[Optional[str]] = mapped_column(String(100), comment="归档原因")

    __table_args__ = (
        Index("idx_archive_entity", "entity_type", "entity_value"),
        Index("idx_archive_type_time", "list_type", "archive_time"),
        Index("idx_archive_time", "archive_time"),
    )


# ============================================================
# 相对参考实现的变更清单（详见 specs/SPEC.md 5.4）
# ============================================================
# 1. RiskRule      + is_deleted；rule_category 6→10 类；action 新增「二次验证」
# 2. RiskEvent     event_type 4→6 类（新增「登录」「营销领券」）
# 3. RiskFeature   entity_type 由 Enum 改为 String(20)（9 个维度，避免改表）
# 4. RiskCase      + conflict_flag（名单冲突标记）
# 5. RiskBlacklist → RiskList：单表建模三色 × 五类主体
# 6. RiskAuditLog  新增：操作审计流水
# 7. RiskChallenge 新增：二次验证记录
# 8. 业务侧新增 UserLoginRecord / CouponReceiveRecord 两张表
# 9. RiskListArchive 新增：名单归档表（FR-42，支撑历史名单独立检索）
#
# 表统计：业务 19 张（17 原有 + 2 新增）+ 风控 10 张（7 原有 + 3 新增）= 29 张
