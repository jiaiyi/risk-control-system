# -*- coding: utf-8 -*-
"""规则引擎单测 —— `app/engine/rule.py`

覆盖验收项：**AC-12**（规则条件容错）、**AC-13**（未知运算符容错）、
**AC-11**（规则热更新）、**AC-18**（逻辑删除）、**AC-28**（事件类型隔离）。

rule.py 除 `load_enabled_rules` 外全是纯函数，因此绝大多数用例不碰数据库 ——
跑得快，失败点也定位得准。
"""
import pytest

from app.engine.rule import (
    evaluate_condition,
    load_enabled_rules,
    match_rules,
    validate_condition,
)


def _rule(**kw):
    """构造一个**不入库**的 RiskRule —— 规则求值不依赖会话，内存对象足够"""
    from app.models import RiskRule

    base = dict(
        rule_id="T-001",
        rule_name="测试规则",
        rule_category="订单欺诈",
        event_type="通用",
        rule_condition='{"field": "x", "op": ">", "value": 1}',
        risk_level="高",
        risk_score=50,
        action="人工审核",
        is_enabled=1,
        priority=0,
        is_deleted=0,
    )
    base.update(kw)
    return RiskRule(**base)


# ============================================================
# 1. 九个比较符（SPEC ADR-05 白名单运算符）
# ============================================================
@pytest.mark.parametrize(
    ("op", "actual", "value", "expected"),
    [
        (">", 10.0, 5, True),
        (">", 5.0, 5, False),
        (">=", 5.0, 5, True),
        (">=", 4.0, 5, False),
        ("<", 3.0, 5, True),
        ("<", 5.0, 5, False),
        ("<=", 5.0, 5, True),
        ("==", 5.0, 5, True),
        ("==", 5.0, 6, False),
        ("!=", 5.0, 6, True),
        ("!=", 5.0, 5, False),
        ("in", 3.0, [1, 3, 5], True),
        ("in", 4.0, [1, 3, 5], False),
        ("not_in", 4.0, [1, 3, 5], True),
        ("not_in", 3.0, [1, 3, 5], False),
        ("between", 5.0, [1, 10], True),
        ("between", 1.0, [1, 10], True),    # 闭区间：下界可取到
        ("between", 10.0, [1, 10], True),   # 闭区间：上界可取到
        ("between", 0.9, [1, 10], False),
        ("between", 10.1, [1, 10], False),
        # 字符串形式的数值也应被正确转换（规则 JSON 里 "5" 与 5 等价）
        (">", 10.0, "5", True),
    ],
)
def test_nine_operators(op, actual, value, expected):
    cond = {"field": "f", "op": op, "value": value}
    assert evaluate_condition(cond, {"f": actual}) is expected


# ============================================================
# 2. 两个逻辑符（可嵌套）
# ============================================================
def test_and_requires_all_true():
    cond = {"and": [
        {"field": "a", "op": ">", "value": 1},
        {"field": "b", "op": "<", "value": 10},
    ]}
    assert evaluate_condition(cond, {"a": 5, "b": 5}) is True
    assert evaluate_condition(cond, {"a": 0, "b": 5}) is False
    assert evaluate_condition(cond, {"a": 5, "b": 99}) is False


def test_or_requires_any_true():
    cond = {"or": [
        {"field": "a", "op": "==", "value": 1},
        {"field": "b", "op": "==", "value": 2},
    ]}
    assert evaluate_condition(cond, {"a": 9, "b": 2}) is True
    assert evaluate_condition(cond, {"a": 1, "b": 9}) is True
    assert evaluate_condition(cond, {"a": 9, "b": 9}) is False


def test_nested_logic():
    cond = {"and": [
        {"or": [
            {"field": "a", "op": "==", "value": 1},
            {"field": "b", "op": "==", "value": 2},
        ]},
        {"field": "c", "op": ">", "value": 0},
    ]}
    assert evaluate_condition(cond, {"a": 9, "b": 2, "c": 1}) is True
    assert evaluate_condition(cond, {"a": 9, "b": 9, "c": 1}) is False
    assert evaluate_condition(cond, {"a": 1, "b": 2, "c": 0}) is False


def test_empty_logic_array_is_not_hit():
    """空数组**不**视为恒真 —— 否则 `{"and": []}` 会变成命中一切的万能规则"""
    assert evaluate_condition({"and": []}, {"a": 1}) is False
    assert evaluate_condition({"or": []}, {"a": 1}) is False


# ============================================================
# 3. 容错（AC-12 / AC-13）—— 全部要求「不命中且不抛异常」
# ============================================================
def test_missing_feature_is_not_hit():
    """AC-12：规则引用不存在的特征名 → 不命中，接口正常返回，不报 500"""
    cond = {"field": "no_such_feature", "op": ">", "value": 1}
    assert evaluate_condition(cond, {"other_feature": 99}) is False


def test_unknown_operator_is_not_hit():
    """AC-13：未支持的运算符 → 不命中（并落警告日志，见 _compare 的 warning）"""
    assert evaluate_condition({"field": "f", "op": "=~", "value": "x"}, {"f": 1}) is False
    assert evaluate_condition({"field": "f", "op": "LIKE", "value": "x"}, {"f": 1}) is False
    assert evaluate_condition({"field": "f", "op": "", "value": 1}, {"f": 1}) is False


def test_non_numeric_value_is_not_hit():
    """value 转 float 失败 → 内部异常被吞成「不命中」，绝不冒泡成 500"""
    assert evaluate_condition({"field": "f", "op": ">", "value": "abc"}, {"f": 1}) is False
    assert evaluate_condition({"field": "f", "op": "between", "value": [1]}, {"f": 1}) is False
    assert evaluate_condition({"field": "f", "op": "in", "value": "not-a-list"}, {"f": 1}) is False


def test_non_dict_condition_is_not_hit():
    assert evaluate_condition("not a dict", {"f": 1}) is False
    assert evaluate_condition([], {"f": 1}) is False
    assert evaluate_condition(None, {"f": 1}) is False


# ============================================================
# 4. 条件结构校验（FR-13，规则新建/编辑接口调用）
# ============================================================
@pytest.mark.parametrize(
    ("cond", "ok"),
    [
        ({"field": "f", "op": ">", "value": 1}, True),
        ({"field": "f", "op": "in", "value": [1, 2]}, True),
        ({"field": "f", "op": "between", "value": [1, 2]}, True),
        ({"and": [{"field": "f", "op": ">", "value": 1}]}, True),
        ({"or": [{"field": "f", "op": ">", "value": 1},
                 {"field": "g", "op": "<", "value": 2}]}, True),
        ({}, False),                                              # 空对象
        ({"op": ">", "value": 1}, False),                          # 缺 field
        ({"field": "f", "value": 1}, False),                       # 缺 op
        ({"field": "f", "op": ">"}, False),                        # 缺 value
        ({"field": "f", "op": "=~", "value": 1}, False),           # 未支持运算符
        ({"field": "f", "op": "in", "value": 1}, False),           # in 的 value 非数组
        ({"field": "f", "op": "not_in", "value": "x"}, False),
        ({"field": "f", "op": "between", "value": [1]}, False),    # between 长度不为 2
        ({"field": "f", "op": "between", "value": [1, 2, 3]}, False),
        ({"and": []}, False),                                      # 空逻辑数组
        ({"or": "x"}, False),                                      # 逻辑值非数组
        ("not a dict", False),
        (None, False),
    ],
)
def test_validate_condition(cond, ok):
    valid, msg = validate_condition(cond)
    assert valid is ok, f"cond={cond!r} → {valid!r} / {msg}"
    if not ok:
        assert msg, "不合法时必须给出错误说明"


# ============================================================
# 5. 规则匹配
# ============================================================
def test_match_rules_keeps_order_and_skips_broken_json():
    """条件 JSON 解析失败的规则应被跳过，不影响其余规则的匹配"""
    rules = [
        _rule(rule_id="A", rule_condition='{"field": "s", "op": ">", "value": 50}'),
        _rule(rule_id="B", rule_condition="{ 这不是合法 JSON }"),
        _rule(rule_id="C", rule_condition='{"field": "s", "op": ">", "value": 10}'),
    ]
    hits = match_rules(rules, {"s": 60})
    assert [h.rule_id for h in hits] == ["A", "C"]


def test_match_rules_result_payload_shape():
    """命中结果要能直接序列化进 risk_assessment.rule_results（O-03 规则命中明细）"""
    rule = _rule(rule_id="R001", rule_name="高额订单", risk_score=80,
                 rule_condition='{"field": "order_amount", "op": ">=", "value": 10000}')
    hits = match_rules([rule], {"order_amount": 12000})
    assert len(hits) == 1

    payload = hits[0].to_dict()
    for key in ("rule_id", "rule_name", "rule_category", "risk_level",
                "risk_score", "action", "description", "priority"):
        assert key in payload, f"命中明细缺少字段 {key}"
    assert payload["rule_id"] == "R001"
    assert payload["risk_score"] == 80


# ============================================================
# 6. 规则加载（需要数据库）
# ============================================================
def test_preset_rules_count(db):
    """T-05 的既定事实：41 条预置规则全部启用、无逻辑删除"""
    rules = load_enabled_rules(db)
    assert len(rules) == 41, f"预置规则应为 41 条，实际 {len(rules)}"

    ids = {r.rule_id for r in rules}
    for prefix, count in (("R", 24), ("L", 5), ("C", 5), ("B", 3), ("A", 4)):
        got = sum(1 for i in ids if i.startswith(prefix))
        assert got == count, f"{prefix} 系列规则应为 {count} 条，实际 {got}"


def test_load_enabled_rules_filters_by_event_type(db):
    """AC-28：登录事件只加载「登录安全」+「通用」，下单类规则不参与求值"""
    rules = load_enabled_rules(db, event_type="登录")
    assert rules, "登录场景应有可用规则"
    types = {r.event_type for r in rules}
    assert types <= {"登录", "通用"}, f"出现了不该加载的事件类型：{types}"
    assert "下单" not in types


def test_load_enabled_rules_excludes_disabled(db):
    """AC-11：启用中的规则被停用后，立即不再参与匹配（热更新，无需重启）"""
    total = len(load_enabled_rules(db))
    assert total == 41

    from app.models import RiskRule

    target = db.query(RiskRule).order_by(RiskRule.rule_id).first()
    target.is_enabled = 0
    db.commit()

    left = load_enabled_rules(db)
    assert len(left) == total - 1
    assert target.rule_id not in {r.rule_id for r in left}


def test_load_enabled_rules_excludes_soft_deleted(db):
    """AC-18：逻辑删除后不再参与匹配（但记录仍在表里，历史评估仍可回溯）"""
    from app.models import RiskRule

    target = db.query(RiskRule).order_by(RiskRule.rule_id).first()
    target.is_deleted = 1
    db.commit()

    left = load_enabled_rules(db)
    assert target.rule_id not in {r.rule_id for r in left}
    assert db.query(RiskRule).filter(
        RiskRule.rule_id == target.rule_id).count() == 1, "逻辑删除不应物理删行"


def test_load_enabled_rules_sorted_by_priority_desc(db):
    """案件分类取 hits[0]，依赖 priority 降序（SPEC Q2 决策）"""
    rules = load_enabled_rules(db)
    priorities = [r.priority for r in rules]
    assert priorities == sorted(priorities, reverse=True)
