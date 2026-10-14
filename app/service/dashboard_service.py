# -*- coding: utf-8 -*-
"""仪表盘服务 —— 资损防护统计 / 实时事件流 / 大盘概览

性能约定（SPEC 8 NFR-03）：
    仪表盘加载 < 2s。手段：聚合走数据库侧（规则命中 TOP5 用 MySQL `JSON_TABLE`
    在库内展开，**不把 JSON 全量拉回内存**再统计），聚合结果可缓存 60s。
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cache
from app.config import settings

logger = logging.getLogger(__name__)

_OVERVIEW_CACHE_KEY = "risk:dashboard:overview"
_OVERVIEW_TTL = 60      # 仪表盘聚合缓存 60s（NFR-03）


# ============================================================
# API-20 资损防护统计（FR-50）
# ============================================================
def loss_prevention(db: Session, *, days: int = 7) -> dict:
    """被拦截的案件数、账号数、拦截金额 —— 回答「风控到底挡下了多少钱」

    金额只统计**下单 / 支付**类事件且决策为「拒绝」的案件，
    并按 `risk_case.source_id → order_info → order_detail` 求实际支付金额。
    """
    since = datetime.now() - timedelta(days=days)

    blocked = db.execute(text(
        "SELECT COUNT(DISTINCT c.case_id) AS cases, "
        "       COUNT(DISTINCT c.user_id) AS users "
        "FROM risk_case c JOIN risk_assessment a ON c.assessment_id = a.assessment_id "
        "WHERE c.create_time >= :since AND a.decision = '拒绝'"
    ), {"since": since}).first()

    amount = db.execute(text(
        "SELECT COALESCE(SUM(od.final_amount), 0) AS total "
        "FROM risk_case c "
        "JOIN risk_assessment a ON c.assessment_id = a.assessment_id "
        "JOIN order_info oi ON oi.order_id = c.source_id "
        "JOIN order_detail od ON od.order_id = oi.order_id "
        "WHERE a.decision = '拒绝' AND c.event_type IN ('下单', '支付') "
        "  AND c.create_time >= :since"
    ), {"since": since}).first()

    by_day = db.execute(text(
        "SELECT DATE(c.create_time) AS d, COUNT(DISTINCT c.case_id) AS cases "
        "FROM risk_case c JOIN risk_assessment a ON c.assessment_id = a.assessment_id "
        "WHERE a.decision = '拒绝' AND c.create_time >= :since "
        "GROUP BY DATE(c.create_time) ORDER BY d"
    ), {"since": since}).fetchall()

    return {
        "days": days,
        "blocked_cases": blocked.cases if blocked else 0,
        "blocked_users": blocked.users if blocked else 0,
        "blocked_amount": float(amount.total) if amount else 0.0,
        "trend": [{"date": str(r.d), "cases": r.cases} for r in by_day],
    }


# ============================================================
# API-21 实时事件流（FR-51）
# ============================================================
def realtime_events(db: Session, *, limit: int = 20) -> list[dict]:
    """最近 N 条风控检查（事件 + 评估结果），供前端滚屏展示"""
    rows = db.execute(text(
        "SELECT e.event_id, e.event_type, e.user_id, e.event_source_id, "
        "       e.create_time, a.final_score, a.risk_level, a.decision, a.rule_count, "
        "       a.assessment_id "
        "FROM risk_event e LEFT JOIN risk_assessment a ON a.event_id = e.event_id "
        "ORDER BY e.create_time DESC, e.event_id DESC LIMIT :limit"
    ), {"limit": limit}).fetchall()

    return [{
        "event_id": r.event_id,
        "assessment_id": r.assessment_id,
        "event_type": r.event_type,
        "user_id": r.user_id,
        "source_id": r.event_source_id,
        "final_score": r.final_score,
        "risk_level": r.risk_level,
        "decision": r.decision,
        "rule_count": r.rule_count,
        "create_time": r.create_time.isoformat(timespec="seconds") if r.create_time else None,
    } for r in rows]


# ============================================================
# API-22 大盘概览（FR-26~28）
# ============================================================
def overview(db: Session, *, days: int = 7, use_cache: bool = True) -> dict:
    """今日概览 + 近 N 天趋势 + 规则命中 TOP5 + 风险等级分布 + 拦截率"""
    if use_cache:
        hit = cache.safe_get(_OVERVIEW_CACHE_KEY)
        if isinstance(hit, dict):
            hit = dict(hit)
            hit["cached"] = True
            return hit

    since = datetime.now() - timedelta(days=days)

    # ---- 今日概览 ----
    today = db.execute(text(
        "SELECT COUNT(*) AS assessments, "
        "       SUM(CASE WHEN risk_level IN ('高','极高') THEN 1 ELSE 0 END) AS high_risk, "
        "       SUM(CASE WHEN decision = '拒绝' THEN 1 ELSE 0 END) AS rejected, "
        "       SUM(CASE WHEN decision = '人工审核' THEN 1 ELSE 0 END) AS review, "
        "       SUM(CASE WHEN decision = '通过' THEN 1 ELSE 0 END) AS passed, "
        "       SUM(CASE WHEN decision = '二次验证' THEN 1 ELSE 0 END) AS challenge "
        "FROM risk_assessment WHERE create_time >= CURDATE()"
    )).first()

    total = today.assessments or 0
    rejected = today.rejected or 0
    passed = today.passed or 0

    # ---- 近 N 天趋势（含拦截率，FR-27 扩展）----
    trend_rows = db.execute(text(
        "SELECT DATE(create_time) AS d, COUNT(*) AS total, "
        "       SUM(CASE WHEN decision = '拒绝' THEN 1 ELSE 0 END) AS rejected, "
        "       SUM(CASE WHEN risk_level IN ('高','极高') THEN 1 ELSE 0 END) AS high_risk "
        "FROM risk_assessment WHERE create_time >= :since "
        "GROUP BY DATE(create_time) ORDER BY d"
    ), {"since": since}).fetchall()

    trend = []
    for r in trend_rows:
        t = r.total or 0
        trend.append({
            "date": str(r.d),
            "total": t,
            "rejected": r.rejected or 0,
            "high_risk": r.high_risk or 0,
            "block_rate": round((r.rejected or 0) / t, 4) if t else 0,
        })

    # ---- 规则命中 TOP5（MySQL JSON_TABLE 库内展开，不拉回内存）----
    top_rules = []
    try:
        rows = db.execute(text(
            "SELECT jt.rule_id, jt.rule_name, jt.rule_category, COUNT(*) AS hits "
            "FROM risk_assessment a, "
            "     JSON_TABLE(a.rule_results, '$[*]' COLUMNS ("
            "       rule_id       VARCHAR(50)  PATH '$.rule_id', "
            "       rule_name     VARCHAR(100) PATH '$.rule_name', "
            "       rule_category VARCHAR(50)  PATH '$.rule_category'"
            "     )) AS jt "
            "WHERE a.create_time >= :since "
            "GROUP BY jt.rule_id, jt.rule_name, jt.rule_category "
            "ORDER BY hits DESC LIMIT 5"
        ), {"since": since}).fetchall()
        top_rules = [{"rule_id": r.rule_id, "rule_name": r.rule_name,
                      "rule_category": r.rule_category, "hits": r.hits} for r in rows]
    except Exception as exc:  # noqa: BLE001
        # JSON_TABLE 在部分 MySQL 版本/模式下不可用 —— 降级为空列表，不阻断整个仪表盘
        logger.warning("规则命中 TOP5 聚合失败（已降级为空）：%s", exc)

    # ---- 风险等级分布 ----
    dist = {}
    for r in db.execute(text(
        "SELECT risk_level, COUNT(*) AS c FROM risk_assessment "
        "WHERE create_time >= :since GROUP BY risk_level"
    ), {"since": since}).fetchall():
        dist[r.risk_level] = r.c

    # ---- 待审案件数 ----
    pending = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_case WHERE case_status IN ('待审核','审核中')"
    )).first()

    result = {
        "today": {
            "assessments": total,
            "high_risk": today.high_risk or 0,
            "rejected": rejected,
            "review": today.review or 0,
            "passed": passed,
            "challenge": today.challenge or 0,
            "pass_rate": round(passed / total, 4) if total else 0,
            "block_rate": round(rejected / total, 4) if total else 0,
        },
        "pending_cases": pending.c if pending else 0,
        "trend": trend,
        "top_rules": top_rules,
        "risk_distribution": dist,
        "cached": False,
    }

    cache.safe_set(_OVERVIEW_CACHE_KEY, result, _OVERVIEW_TTL)
    return result


def invalidate_overview_cache() -> None:
    """案件 / 评估有变更时清掉大盘缓存（写后失效）"""
    cache.safe_delete(_OVERVIEW_CACHE_KEY)


def health_snapshot(db: Session) -> dict:
    """运营健康度快照（供 /api/health/metrics 组合）

    函数内导入 sub-service：避免模块级循环依赖（它们可能间接引用本模块的缓存失效函数）。
    """
    from app.service import profile_service, rule_service

    return {
        "rules": rule_service.statistics(db),
        "profiles": profile_service.statistics(db),
        "cache": cache.stats(),
        "thresholds": {
            "pass": settings.RISK_PASS_THRESHOLD,
            "mark": settings.RISK_MARK_THRESHOLD,
            "review": settings.RISK_REVIEW_THRESHOLD,
            "multi_rule_bonus": settings.RISK_MULTI_RULE_BONUS,
            "veto_min_score": settings.RISK_VETO_MIN_SCORE,
            "graylist_bonus": settings.RISK_GRAYLIST_BONUS,
        },
        "graph_limits": {
            "default_hops": settings.GRAPH_DEFAULT_HOPS,
            "max_hops": settings.GRAPH_MAX_HOPS,
            "max_nodes": settings.GRAPH_MAX_NODES,
        },
    }
