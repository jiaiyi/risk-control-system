# -*- coding: utf-8 -*-
"""仿真服务 —— 规则仿真回放（分步链路 + 批量回放 + 作弊样本库）

业务价值（PRD FR-14）：
    规则上线前要验证「会命中谁、误伤多少人」。直接在生产试错代价高，
    所以先在历史数据上跑一遍。

**零副作用**是硬约束（FR-14 验收 2 / SPEC 7.3）：
    · 不写 `risk_event` / `risk_feature` / `risk_assessment` / `risk_case` / `risk_user_profile`
    · 特征计算传 `write_cache=False`，连 Redis 回填都不做
    · 复用 `feature` / `rule` 引擎**直接算**，绝不调 `decision.run_risk_check`（它必然落库）
    · 响应里带 `side_effects`，**实测各表行数变化**，用数字证明而不是靠声明

作弊样本库（FR-52）：
    项目未单设样本表（SPEC 5.2 表清单无此表），样本直接**从历史案件反推** ——
    有案件即代表该事件曾被判为高风险，天然是「已知作弊样本」。
"""
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.engine.decision import (
    compute_score,
    resolve_context,
    score_to_decision,
    score_to_level,
)
from app.engine.feature import compute_all_features
from app.engine.rule import RuleHitResult, evaluate_condition, load_enabled_rules
from app.models import (
    RiskAssessment,
    RiskCase,
    RiskEvent,
    RiskFeature,
    RiskUserProfile,
)

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = ("登录", "营销领券", "下单", "支付", "售后申请", "物流投诉")

# 零副作用要盯住的表
_WATCHED_TABLES = {
    "risk_event": RiskEvent,
    "risk_feature": RiskFeature,
    "risk_assessment": RiskAssessment,
    "risk_case": RiskCase,
    "risk_user_profile": RiskUserProfile,
}


class SimulationServiceError(Exception):
    code = "BIZ_SIMULATION_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _snapshot_counts(db: Session) -> dict:
    """快照各表行数（用于证明零副作用）"""
    return {name: db.query(model).count() for name, model in _WATCHED_TABLES.items()}


# ============================================================
# 单条事件仿真
# ============================================================
def run_single(
    db: Session,
    *,
    user_id: str,
    event_type: str,
    source_id: str | None = None,
    order_id: str | None = None,
    receive_id: str | None = None,
    device_id: str | None = None,
    login_ip: str | None = None,
    event_data: dict | None = None,
    graylist_hit: bool = False,
    max_rule_steps: int = 200,
) -> dict:
    """对单条事件做分步仿真，**不产生任何写入**

    返回的 `steps` 逐条列出规则的求值过程（命中 / 未命中 + 实际特征值），
    供前端做「分步链路可视化」。
    """
    if event_type not in VALID_EVENT_TYPES:
        raise SimulationServiceError(
            f"事件类型非法：{event_type}（可选：{'/'.join(VALID_EVENT_TYPES)}）",
            {"event_type": event_type},
        )

    # ---- 1. 上下文补全（只读）----
    event_data = event_data or {}
    device_id = device_id or event_data.get("device_id")
    login_ip = login_ip or event_data.get("login_ip")
    receive_id = receive_id or event_data.get("receive_id")

    ctx = resolve_context(
        db, user_id=user_id, event_type=event_type, source_id=source_id,
        order_id=order_id, receive_id=receive_id,
        device_id=device_id, login_ip=login_ip,
    )

    # ---- 2. 特征计算（不回填缓存）----
    features = compute_all_features(
        db, user_id=user_id,
        order_id=ctx["order_id"], receive_id=ctx["receive_id"],
        device_id=ctx["device_id"], login_ip=ctx["login_ip"],
        write_cache=False,
    )

    # ---- 3. 规则逐条求值 ----
    rules = load_enabled_rules(db, event_type=event_type)
    steps: list[dict] = []
    hits: list[RuleHitResult] = []

    for rule in rules[:max_rule_steps]:
        try:
            condition = rule.condition_dict
        except Exception as exc:  # noqa: BLE001
            logger.warning("仿真：规则 %s 条件解析失败 %s", rule.rule_id, exc)
            continue

        matched = evaluate_condition(condition, features)
        # 把条件里引用的特征的实际取值一并带出来，便于前端解释「为什么没命中」
        referenced = _referenced_features(condition)
        steps.append({
            "rule_id": rule.rule_id,
            "rule_name": rule.rule_name,
            "rule_category": rule.rule_category,
            "risk_level": rule.risk_level,
            "risk_score": rule.risk_score,
            "action": rule.action,
            "priority": rule.priority,
            "condition": condition,
            "matched": matched,
            "actual_values": {f: features.get(f) for f in referenced},
        })
        if matched:
            hits.append(RuleHitResult(rule))

    # ---- 4. 评分与决策（复用引擎纯函数，不落库）----
    breakdown = compute_score(hits, graylist_hit=graylist_hit)
    final_score = breakdown.final_score

    return {
        "user_id": user_id,
        "event_type": event_type,
        "source_id": source_id,
        "context": ctx,
        "feature_count": len(features),
        "features": features,
        "rule_total": len(rules),
        "steps": steps,
        "triggered_rules": [h.to_dict() for h in hits],
        "rule_count": len(hits),
        "score_breakdown": breakdown.to_dict(),
        "final_score": final_score,
        "risk_level": score_to_level(final_score),
        "decision": score_to_decision(final_score, hits),
        "graylist_hit": graylist_hit,
        "side_effects": {"db_writes": 0, "note": "仿真全程只读，不写任何风控表"},
    }


def _referenced_features(condition) -> set[str]:
    """递归收集条件表达式里引用到的特征名"""
    names: set[str] = set()
    if not isinstance(condition, dict):
        return names
    if "field" in condition:
        names.add(condition["field"])
    for key in ("and", "or"):
        for sub in condition.get(key) or []:
            names |= _referenced_features(sub)
    return names


# ============================================================
# 作弊样本库（从历史案件反推）
# ============================================================
def load_samples(
    db: Session,
    *,
    limit: int = 50,
    event_type: str | None = None,
    min_score: int | None = None,
) -> list[dict]:
    """作弊样本库：取历史「已产生案件」的事件作为已知风险样本（FR-52）"""
    sql = (
        "SELECT c.user_id, c.event_type, c.source_id, c.case_id, c.case_category, "
        "c.conflict_flag, a.final_score, a.rule_count, a.create_time "
        "FROM risk_case c JOIN risk_assessment a ON c.assessment_id = a.assessment_id "
        "WHERE c.event_type IS NOT NULL"
    )
    params: dict = {"limit": limit}
    if event_type:
        sql += " AND c.event_type = :etype"
        params["etype"] = event_type
    if min_score is not None:
        sql += " AND a.final_score >= :min_score"
        params["min_score"] = min_score
    sql += " ORDER BY a.final_score DESC, c.create_time DESC LIMIT :limit"

    return [{
        "user_id": r.user_id,
        "event_type": r.event_type,
        "source_id": r.source_id,
        "case_id": r.case_id,
        "case_category": r.case_category,
        "conflict_flag": r.conflict_flag,
        "known_score": r.final_score,
        "known_rule_count": r.rule_count,
        "create_time": r.create_time.isoformat(timespec="seconds") if r.create_time else None,
    } for r in db.execute(text(sql), params)]


def list_samples(db: Session, *, limit: int = 50, event_type: str | None = None) -> dict:
    """样本库列表（API-25）"""
    samples = load_samples(db, limit=limit, event_type=event_type)
    by_type: dict[str, int] = {}
    for s in samples:
        by_type[s["event_type"]] = by_type.get(s["event_type"], 0) + 1
    return {"total": len(samples), "by_event_type": by_type, "samples": samples}


# ============================================================
# 批量回放
# ============================================================
def replay(
    db: Session,
    *,
    event_type: str | None = None,
    limit: int = 50,
    rule_id: str | None = None,
    include_detail: bool = False,
    samples: list[dict] | None = None,
) -> dict:
    """对一组历史事件批量回放，统计规则命中数与命中率（FR-14 / FR-52）

    改动前后对比（FR-14 能力 4）的做法：先在旧配置下跑一次存下结果，
    改规则后再跑一次，对比两次的 `rule_stats` 即可。
    """
    samples = samples if samples is not None else load_samples(
        db, limit=limit, event_type=event_type)
    if not samples:
        raise SimulationServiceError("没有可用样本（历史案件为空，请先跑几条风控检查）")

    before = _snapshot_counts(db)

    rule_hits: dict[str, dict] = {}
    affected_users: dict[str, set] = {}
    score_sum = 0
    decision_dist: dict[str, int] = {}
    results: list[dict] = []

    for s in samples:
        try:
            r = run_single(
                db, user_id=s["user_id"], event_type=s["event_type"],
                source_id=s.get("source_id"),
            )
        except SimulationServiceError:
            continue

        score_sum += r["final_score"]
        decision_dist[r["decision"]] = decision_dist.get(r["decision"], 0) + 1

        for hit in r["triggered_rules"]:
            rid = hit["rule_id"]
            info = rule_hits.setdefault(rid, {
                "rule_id": rid,
                "rule_name": hit["rule_name"],
                "rule_category": hit["rule_category"],
                "risk_level": hit["risk_level"],
                "hit_count": 0,
                "score_sum": 0,
            })
            info["hit_count"] += 1
            info["score_sum"] += hit["risk_score"]
            affected_users.setdefault(rid, set()).add(s["user_id"])

        if include_detail:
            results.append({
                "user_id": r["user_id"],
                "event_type": r["event_type"],
                "final_score": r["final_score"],
                "decision": r["decision"],
                "triggered": [h["rule_id"] for h in r["triggered_rules"]],
                "known_score": s.get("known_score"),
            })

    after = _snapshot_counts(db)
    side_effects = {k: after[k] - before[k] for k in before}
    total_writes = sum(abs(v) for v in side_effects.values())

    rule_stats = []
    for rid, info in sorted(rule_hits.items(), key=lambda kv: -kv[1]["hit_count"]):
        stat = dict(info)
        stat["hit_rate"] = round(stat["hit_count"] / len(samples), 4)
        stat["affected_users"] = len(affected_users.get(rid, set()))
        rule_stats.append(stat)

    if rule_id:
        rule_stats = [s for s in rule_stats if s["rule_id"] == rule_id]

    logger.info("批量回放: %d 个样本 → %d 条规则命中，写入 %d 行（应为 0）",
                len(samples), len(rule_stats), total_writes)

    payload = {
        "sample_count": len(samples),
        "event_type": event_type,
        "rule_stats": rule_stats,
        "avg_score": round(score_sum / len(samples), 2) if samples else 0,
        "decision_distribution": decision_dist,
        "side_effects": {**side_effects, "db_writes": total_writes},
        "zero_side_effect": total_writes == 0,
    }
    if include_detail:
        payload["results"] = results
    return payload
