# -*- coding: utf-8 -*-
"""
规则引擎 —— 基于 JSON 条件表达式对特征字典求值

支持的运算符（9 个比较符 + 2 个逻辑符）：
    比较：>  >=  <  <=  ==  !=
    集合：in  not_in
    区间：between
    逻辑：and  or（可嵌套）

安全取舍（SPEC ADR-05）：
    使用**白名单 if/elif 硬编码**，不使用 eval / exec，也不引入 simpleeval 等第三方沙箱。
    理由：风控合规要求求值路径 100% 可审计；第三方沙箱有 CVE 历史。
    代价：新增运算符需改代码走 PR，视为可接受成本。
"""
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import RiskRule

logger = logging.getLogger(__name__)


# ============================================================
# 条件求值器
# ============================================================
def evaluate_condition(condition: dict, features: dict[str, float]) -> bool:
    """递归求值 JSON 条件表达式

    条件格式：
        {"field": "user_refund_rate", "op": ">", "value": 0.5}
        {"and": [条件1, 条件2]}
        {"or":  [条件1, 条件2]}
    """
    if not isinstance(condition, dict):
        return False

    # 逻辑组合
    if "and" in condition:
        subs = condition["and"]
        return bool(subs) and all(evaluate_condition(s, features) for s in subs)
    if "or" in condition:
        subs = condition["or"]
        return bool(subs) and any(evaluate_condition(s, features) for s in subs)

    # 单条件比较
    field = condition.get("field", "")
    op = condition.get("op", "")
    value = condition.get("value")

    actual = features.get(field)
    if actual is None:
        # 特征不存在 → 判定为不命中（不报错）
        logger.debug("特征 '%s' 不存在，条件跳过", field)
        return False

    try:
        return _compare(actual, op, value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("条件求值异常: field=%s op=%s value=%s actual=%s err=%s",
                       field, op, value, actual, exc)
        return False


def _compare(actual: float, op: str, value: Any) -> bool:
    """执行单次比较运算（白名单运算符）"""
    if op == ">":
        return actual > float(value)
    elif op == ">=":
        return actual >= float(value)
    elif op == "<":
        return actual < float(value)
    elif op == "<=":
        return actual <= float(value)
    elif op == "==":
        return actual == float(value)
    elif op == "!=":
        return actual != float(value)
    elif op == "in":
        return actual in [float(v) for v in value]
    elif op == "not_in":
        return actual not in [float(v) for v in value]
    elif op == "between":
        low, high = float(value[0]), float(value[1])
        return low <= actual <= high
    else:
        logger.warning("不支持的运算符: %s（判定为不命中）", op)
        return False


# ============================================================
# 规则匹配
# ============================================================
class RuleHitResult:
    """单条规则的命中结果"""

    def __init__(self, rule: RiskRule):
        self.rule_id = rule.rule_id
        self.rule_name = rule.rule_name
        self.rule_category = rule.rule_category
        self.risk_level = rule.risk_level
        self.risk_score = rule.risk_score
        self.action = rule.action
        self.description = rule.description
        self.priority = rule.priority

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "rule_category": self.rule_category,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "action": self.action,
            "description": self.description,
            "priority": self.priority,
        }


def load_enabled_rules(db: Session, event_type: str | None = None) -> list[RiskRule]:
    """加载启用的规则

    · 过滤 is_deleted = 0（逻辑删除，SPEC FR-12）
    · 若指定 event_type，只加载该事件类型 + 「通用」的规则
    · **按 priority 降序**（案件分类取 hits[0] 时依赖此顺序，见 SPEC Q2 决策）
    """
    query = db.query(RiskRule).filter(
        RiskRule.is_enabled == 1,
        RiskRule.is_deleted == 0,
    )
    if event_type:
        query = query.filter(RiskRule.event_type.in_([event_type, "通用"]))
    return query.order_by(RiskRule.priority.desc()).all()


def match_rules(rules: list[RiskRule], features: dict[str, float]) -> list[RuleHitResult]:
    """逐条匹配规则，返回命中列表（保持 rules 的顺序）"""
    hits: list[RuleHitResult] = []
    for rule in rules:
        try:
            condition = rule.condition_dict
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("规则 %s 条件解析失败: %s", rule.rule_id, exc)
            continue

        if evaluate_condition(condition, features):
            logger.info("规则命中: %s (%s), 分值=%d", rule.rule_id, rule.rule_name, rule.risk_score)
            hits.append(RuleHitResult(rule))
    return hits


def validate_condition(condition: Any) -> tuple[bool, str]:
    """校验条件表达式的结构合法性（供规则新建/编辑接口调用，FR-13）

    返回 (是否合法, 错误说明)
    """
    if not isinstance(condition, dict) or not condition:
        return False, "条件必须是合法的 JSON 对象"

    if "and" in condition or "or" in condition:
        key = "and" if "and" in condition else "or"
        subs = condition[key]
        if not isinstance(subs, list) or not subs:
            return False, f"{key} 的值必须是至少含一个条件的数组"
        for sub in subs:
            ok, msg = validate_condition(sub)
            if not ok:
                return False, msg
        return True, ""

    if not {"field", "op"} <= set(condition.keys()):
        return False, "单条件必须包含 field 与 op"
    if "value" not in condition:
        return False, "单条件必须包含 value"

    allowed_ops = {">", ">=", "<", "<=", "==", "!=", "in", "not_in", "between"}
    if condition["op"] not in allowed_ops:
        return False, f"不支持的运算符：{condition['op']}（可选：{'/'.join(sorted(allowed_ops))}）"

    if condition["op"] in ("in", "not_in") and not isinstance(condition["value"], list):
        return False, f"{condition['op']} 的 value 必须是数组"
    if condition["op"] == "between":
        v = condition["value"]
        if not isinstance(v, list) or len(v) != 2:
            return False, "between 的 value 必须是 [min, max] 两元素数组"

    return True, ""
