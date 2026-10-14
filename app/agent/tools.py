# -*- coding: utf-8 -*-
"""AI Agent 工具集（8 个）

分组（延续参考实现的两分法）：
    · **风控决策工具（4）**：risk_check / query_cases / query_user_profile / manage_blacklist
    · **数据分析工具（4）**：query_dashboard_stats / analyze_risk_trend /
      analyze_rule_effectiveness / query_business_data

铁律（SPEC 3.2 Agent 层边界）：
    工具内部**复用服务层**，不重复实现业务逻辑、不直接拼 SQL。
    唯一的例外是 `query_business_data` —— 它查的是 19 张业务表（风控不落库的那部分），
    为此采用**预定义查询模板白名单**（SPEC 8 安全要求：禁止任意 SQL）。

LLM 会读每个函数的 docstring 来决定怎么调用，所以参数说明必须写清楚。
"""
import json
import logging
from datetime import datetime, timedelta

from langchain_core.tools import tool

from app.database import SessionLocal
from app.service import (
    case_service,
    dashboard_service,
    event as event_service,
    list_service,
    profile_service,
    simulation_service,
)

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = ("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉")
VALID_CASE_STATUS = ("待审核", "审核中", "已通过", "已拒绝", "已关闭")
VALID_ENTITY_TYPES = ("用户", "手机号", "IP", "设备", "地址")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


# ============================================================
# 风控决策工具（4）
# ============================================================
@tool
def risk_check(user_id: str, event_type: str, source_id: str,
               order_id: str = "", device_id: str = "", login_ip: str = "") -> str:
    """对指定用户和事件执行实时风控检查，返回评分、风险等级、决策与命中规则。

    Args:
        user_id: 用户ID，如 "1003"
        event_type: 事件类型，必须是 登录 / 营销领券 / 下单 / 支付 / 售后申请 / 物流投诉 之一
        source_id: 关联业务ID，如订单号、登录记录ID（用户随便填一个能标识本次事件的字符串即可）
        order_id: 订单ID，可选；下单/支付类事件不传会自动从 source_id 推导
        device_id: 设备标识，可选；登录/领券事件建议传入
        login_ip: 登录IP，可选
    """
    db = SessionLocal()
    try:
        outcome = event_service.process_event(
            db, user_id=user_id, event_type=event_type, source_id=source_id,
            event_data={"device_id": device_id or None, "login_ip": login_ip or None},
            order_id=order_id or None,
        )
        return _dumps({
            "assessment_id": outcome.assessment_id,
            "user_id": outcome.user_id,
            "event_type": outcome.event_type,
            "final_score": outcome.final_score,
            "risk_level": outcome.risk_level,
            "decision": outcome.decision,
            "rule_count": outcome.rule_count,
            # ⚠️ EventProcessResult.triggered_rules 是**字段**；
            #    而 DecisionOutcome.triggered_rules 是**方法** —— 别加括号
            "triggered_rules": outcome.triggered_rules,
            "list_hit": outcome.list_hit_payload(),
            "case_id": outcome.case_id,
            "challenge_id": outcome.challenge_id,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("risk_check 执行失败")
        return f"风控检查失败：{exc}"
    finally:
        db.close()


@tool
def query_cases(status: str = "", case_category: str = "",
                conflict_only: bool = False, page: int = 1) -> str:
    """查询风控案件列表，可按状态 / 分类 / 名单冲突筛选。

    Args:
        status: 案件状态，可选 待审核 / 审核中 / 已通过 / 已拒绝 / 已关闭；空则查全部
        case_category: 案件分类，如 登录安全 / 营销薅羊毛 / 订单欺诈 等；空则查全部
        conflict_only: 是否只看「黑白名单同时命中」的冲突案件
        page: 页码，从 1 开始
    """
    db = SessionLocal()
    try:
        total, items = case_service.list_cases(
            db, case_status=status or None, case_category=case_category or None,
            conflict_only=conflict_only, page=page, page_size=10,
        )
        return _dumps({
            "total": total,
            "page": page,
            "items": [{
                "case_id": c.case_id,
                "case_status": c.case_status,
                "case_category": c.case_category,
                "conflict_flag": c.conflict_flag,
                "user_id": c.user_id,
                "source_id": c.source_id,
                "event_type": c.event_type,
                "reviewer": c.reviewer,
                "create_time": c.create_time.isoformat(timespec="seconds") if c.create_time else None,
            } for c in items],
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("query_cases 执行失败")
        return f"案件查询失败：{exc}"
    finally:
        db.close()


@tool
def query_user_profile(user_id: str) -> str:
    """查询用户风险画像：综合风险分、等级、订单/退款/投诉统计、评估次数，
    以及最近的评分趋势和名单履历。

    Args:
        user_id: 用户ID，如 "1003"
    """
    db = SessionLocal()
    try:
        detail = profile_service.get_profile(db, user_id, trend_limit=5)
        return _dumps(detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("query_user_profile 执行失败")
        return f"画像查询失败：{exc}"
    finally:
        db.close()


@tool
def manage_blacklist(action: str, entity_type: str = "用户", value: str = "",
                     list_type: str = "黑名单", reason: str = "") -> str:
    """查询 / 新增 / 移除三色名单条目。

    Args:
        action: 操作，必须是 查询 / 新增 / 移除 之一
        entity_type: 主体类型，用户 / 手机号 / IP / 设备 / 地址
        value: 主体值，如 "1003"、"D-SHARED-0001"、"10.99.99.99"
        list_type: 名单类型，黑名单 / 白名单 / 灰名单（默认黑名单）
        reason: 加入原因或移除原因

    注意：白名单有硬约束（必须设有效期、不超 90 天、不能与黑名单共存），
    不满足时工具会返回明确的错误说明。
    """
    db = SessionLocal()
    try:
        if action == "查询":
            total, items = list_service.list_items(
                db, list_type=list_type or None, entity_type=entity_type or None,
                keyword=value or None, page=1, page_size=20,
            )
            return _dumps({
                "action": "查询", "total": total,
                "items": [{
                    "list_id": i.list_id, "list_type": i.list_type,
                    "entity_type": i.entity_type, "entity_value": i.entity_value,
                    "reason": i.reason,
                    "expire_time": i.expire_time.isoformat(sep=" ", timespec="seconds") if i.expire_time else None,
                } for i in items],
            })

        if action == "新增":
            item = list_service.add_list_item(
                db, list_type=list_type, entity_type=entity_type, entity_value=value,
                reason=reason, created_by="agent",
            )
            return _dumps({
                "action": "新增", "list_id": item.list_id, "list_type": item.list_type,
                "entity_type": item.entity_type, "entity_value": item.entity_value,
                "expire_time": item.expire_time.isoformat(sep=" ", timespec="seconds") if item.expire_time else None,
            })

        if action == "移除":
            if not value:
                return "移除操作必须提供主体值"
            rows = db.query(list_service.RiskList).filter(
                list_service.RiskList.entity_value == value,
                list_service.RiskList.entity_type == entity_type,
            ).all()
            if not rows:
                return f"未找到 {entity_type}={value} 的名单条目"
            removed = [list_service.remove_list_item(db, r.list_id, operator="agent") for r in rows]
            return _dumps({"action": "移除", "removed": removed})

        return f"未知操作：{action}（必须是 查询 / 新增 / 移除）"
    except Exception as exc:  # noqa: BLE001
        logger.exception("manage_blacklist 执行失败")
        return f"名单操作失败：{exc}"
    finally:
        db.close()


# ============================================================
# 数据分析工具（4）
# ============================================================
@tool
def query_dashboard_stats() -> str:
    """查询风控大盘概览：今日评估数、高风险数、拦截率、通过率、待审案件数，
    近 7 天趋势、规则命中 TOP5、风险等级分布，以及资损防护统计。
    适合回答「今天风控整体情况怎么样」这类问题。
    """
    db = SessionLocal()
    try:
        overview = dashboard_service.overview(db, days=7)
        loss = dashboard_service.loss_prevention(db, days=7)
        return _dumps({"overview": overview, "loss_prevention": loss})
    except Exception as exc:  # noqa: BLE001
        logger.exception("query_dashboard_stats 执行失败")
        return f"大盘查询失败：{exc}"
    finally:
        db.close()


@tool
def analyze_risk_trend(days: int = 7) -> str:
    """分析近 N 天的风控趋势：评估量、拦截量、拦截率逐日变化，以及拦截金额。

    Args:
        days: 统计天数，默认 7，最大 90
    """
    db = SessionLocal()
    try:
        days = max(1, min(int(days), 90))
        overview = dashboard_service.overview(db, days=days)
        loss = dashboard_service.loss_prevention(db, days=days)
        return _dumps({
            "days": days,
            "trend": overview["trend"],
            "risk_distribution": overview["risk_distribution"],
            "today": overview["today"],
            "loss_prevention": loss,
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze_risk_trend 执行失败")
        return f"趋势分析失败：{exc}"
    finally:
        db.close()


@tool
def analyze_rule_effectiveness(days: int = 7, rule_id: str = "") -> str:
    """分析规则命中效果：对近期风险事件做仿真回放，统计每条规则的命中数、
    命中率与影响用户数。适合回答「某条规则最近命中了多少、误伤了谁」这类问题。

    Args:
        days: 回溯天数，默认 7（会取该范围内的历史案件作为样本）
        rule_id: 可选，只看某一条规则（如 "L002"）
    """
    db = SessionLocal()
    try:
        days = max(1, min(int(days), 90))
        limit = max(20, min(days * 20, 300))
        result = simulation_service.replay(
            db, limit=limit, rule_id=rule_id or None, include_detail=False,
        )
        return _dumps({
            "days": days,
            "sample_count": result["sample_count"],
            "avg_score": result["avg_score"],
            "decision_distribution": result["decision_distribution"],
            "rule_stats": result["rule_stats"],
            "zero_side_effect": result["zero_side_effect"],
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("analyze_rule_effectiveness 执行失败")
        return f"规则效果分析失败：{exc}"
    finally:
        db.close()


# ============================================================
# 预定义查询模板白名单（SPEC 8：禁止任意 SQL）
# ============================================================
# 每个模板只暴露必要参数，SQL 全参数化，且只走 SELECT
_QUERY_TEMPLATES = {
    "user_orders": (
        "SELECT oi.order_id, oi.order_status, oi.create_time, oi.payment_time "
        "FROM order_info oi WHERE oi.user_id = :uid ORDER BY oi.create_time DESC LIMIT :lim",
        "某用户的订单列表（含状态与时间）",
    ),
    "order_detail": (
        "SELECT od.order_detail_id, od.order_id, od.sku_name, od.sku_count, "
        "od.total_amount, od.final_amount FROM order_detail od "
        "WHERE od.order_id = :oid ORDER BY od.order_detail_id LIMIT :lim",
        "某订单的商品明细（含金额）",
    ),
    "user_logins": (
        "SELECT login_id, login_time, login_ip, login_city, login_result, device_id "
        "FROM user_login_record WHERE user_id = :uid ORDER BY login_time DESC LIMIT :lim",
        "某用户的最近登录记录（IP、城市、设备、结果）",
    ),
    "user_coupons": (
        "SELECT receive_id, coupon_id, coupon_type, coupon_amount, use_status, "
        "receive_time FROM coupon_receive_record WHERE user_id = :uid "
        "ORDER BY receive_time DESC LIMIT :lim",
        "某用户的领券记录（券种、面额、使用状态）",
    ),
    "user_addresses": (
        "SELECT receive_id, receiver_name, receiver_phone, receive_province, "
        "receive_city, receive_district, receive_street_address "
        "FROM receive_info WHERE user_id = :uid LIMIT :lim",
        "某用户的收货地址（含手机号）",
    ),
    "user_postsales": (
        "SELECT postsale_id, order_detail_id, postsale_status, receive_id "
        "FROM postsale WHERE receive_id IN "
        "(SELECT receive_id FROM receive_info WHERE user_id = :uid) LIMIT :lim",
        "某用户的售后单（含状态）",
    ),
}


@tool
def query_business_data(query_type: str, user_id: str = "", order_id: str = "",
                        limit: int = 10) -> str:
    """查询电商业务数据（订单 / 售后 / 地址 / 登录记录 / 领券记录）。

    Args:
        query_type: 查询类型，必须是以下之一：
            user_orders=用户订单列表 / order_detail=订单商品明细 /
            user_logins=用户登录记录 / user_coupons=用户领券记录 /
            user_addresses=用户收货地址 / user_postsales=用户售后单
        user_id: 用户ID（除 order_detail 外都必填）
        order_id: 订单ID（仅 order_detail 使用）
        limit: 返回条数，默认 10，最大 50

    本工具只走**预定义查询模板**，不执行任意 SQL。
    """
    db = SessionLocal()
    try:
        tpl = _QUERY_TEMPLATES.get(query_type)
        if tpl is None:
            return (f"未知查询类型：{query_type}"
                    f"（可选：{' / '.join(_QUERY_TEMPLATES)}）")

        sql, _desc = tpl
        lim = max(1, min(int(limit), 50))
        from sqlalchemy import text as sql_text
        rows = db.execute(sql_text(sql), {"uid": user_id, "oid": order_id, "lim": lim}).fetchall()
        return _dumps({
            "query_type": query_type,
            "row_count": len(rows),
            "rows": [dict(r._mapping) for r in rows],
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("query_business_data 执行失败")
        return f"业务查询失败：{exc}"
    finally:
        db.close()


RISK_TOOLS = [risk_check, query_cases, query_user_profile, manage_blacklist]
DATA_TOOLS = [query_dashboard_stats, analyze_risk_trend,
              analyze_rule_effectiveness, query_business_data]

ALL_TOOLS = RISK_TOOLS + DATA_TOOLS
