# -*- coding: utf-8 -*-
"""名单服务单测 —— `app/service/list_service.py`

覆盖：**AC-04/06/07**（前置拦截 / 冲突裁决 / 过期失效）、**AC-25**（归档）、
**AC-31**（白名单三条准入校验）、**AC-32**（五类主体）、**AC-33**（灰名单加严）、
**AC-34**（批量导入）、**FR-38**（黑白冲突不自动裁决）。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.config import settings
from app.models import RiskList, RiskListArchive
from app.service import list_service as ls


def _future(days: int = 10) -> datetime:
    return datetime.now() + timedelta(days=days)


# ============================================================
# 一、基础 CRUD
# ============================================================
def test_add_and_query(db):
    item = ls.add_list_item(db, list_type="黑名单", entity_type="用户",
                            entity_value="T-U-1", reason="测试拉黑", created_by="pytest")
    assert item.list_id

    total, items = ls.list_items(db, list_type="黑名单", entity_type="用户", keyword="T-U-1")
    assert total == 1
    assert items[0].entity_value == "T-U-1"


def test_duplicate_same_color_subject_rejected(db):
    """同一颜色 + 同一主体唯一（表上有 uk_list_type_entity 唯一约束）"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-DUP", reason="首次")
    with pytest.raises(ls.ListDuplicateError):
        ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-DUP", reason="重复")


def test_remove_by_subject_only_affects_given_color(db):
    """按主体删除时必须带上名单类型 —— 否则「移出黑名单」会连白名单一起删掉"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-RM", reason="黑")

    removed = ls.remove_list_items_by_subject(
        db, list_type="黑名单", entity_type="用户", entity_value="T-RM")
    assert len(removed) == 1
    assert db.query(RiskList).filter(RiskList.entity_value == "T-RM").count() == 0

    # 再删一次：没找到，返回空列表而不是报错
    assert ls.remove_list_items_by_subject(
        db, list_type="黑名单", entity_type="用户", entity_value="T-RM") == []


# ============================================================
# 二、三色判定（PRD FR-05）
# ============================================================
def test_blacklist_blocks(db):
    """AC-04 的服务层形态：黑名单命中 → 直接拒绝"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-BLK", reason="刷单")
    v = ls.check_lists(db, [ls.Subject("用户", "T-BLK")])
    assert v.decision == "拒绝"
    assert v.black and not v.white
    assert v.conflict is False
    # 说明文案要能直接展示给审核员（含原因）
    assert "刷单" in v.describe()


def test_whitelist_passes(db):
    """AC-05 的服务层形态：白名单命中 → 直接放行"""
    ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="T-WLK",
                     reason="VIP 客户", expire_time=_future())
    v = ls.check_lists(db, [ls.Subject("用户", "T-WLK")])
    assert v.decision == "通过"
    assert v.white and not v.black


def test_graylist_does_not_block(db):
    """AC-33：灰名单不拦截、不放行，只把 graylist_hit 交给决策引擎加严"""
    ls.add_list_item(db, list_type="灰名单", entity_type="用户", entity_value="T-GRY", reason="观察期")
    v = ls.check_lists(db, [ls.Subject("用户", "T-GRY")])
    assert v.decision is None, "灰名单不应产生名单级裁决"
    assert v.graylist_hit is True
    assert v.gray and not v.black and not v.white


def test_no_hit_returns_none(db):
    v = ls.check_lists(db, [ls.Subject("用户", "T-NOBODY")])
    assert v.decision is None
    assert v.graylist_hit is False
    assert v.hits == []


@pytest.mark.parametrize(
    ("entity_type", "value"),
    [
        ("用户", "T-FIVE-U"),
        ("手机号", "13900000001"),
        ("IP", "203.0.113.99"),
        ("设备", "T-FIVE-DEV"),
        ("地址", "北京市朝阳区测试路 1 号"),
    ],
)
def test_five_entity_types_all_block(db, entity_type, value):
    """AC-32：五类主体（用户 / 手机号 / IP / 设备 / 地址）都能触发前置拦截"""
    ls.add_list_item(db, list_type="黑名单", entity_type=entity_type,
                     entity_value=value, reason="五类主体测试")
    v = ls.check_lists(db, [ls.Subject(entity_type, value)])
    assert v.decision == "拒绝", f"{entity_type} 未触发拦截"


def test_expired_record_is_ignored(db):
    """AC-07：有效期已过的记录不参与判定，也不报错"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-EXP",
                     reason="已过期", expire_time=datetime.now() - timedelta(days=1))
    v = ls.check_lists(db, [ls.Subject("用户", "T-EXP")])
    assert v.decision is None
    assert v.black == []


def test_permanent_record_has_no_expire_time(db):
    """expire_time IS NULL = 永久有效（黑名单允许永久）"""
    item = ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-PERM", reason="永久")
    assert item.expire_time is None
    assert ls.check_lists(db, [ls.Subject("用户", "T-PERM")]).decision == "拒绝"


# ============================================================
# 三、冲突裁决（AC-06 / FR-38）
# ============================================================
def test_black_white_conflict_goes_to_manual_review(db):
    """AC-06：黑白同时命中 → 人工审核，不做自动裁决

    注：常规 API 路径**构造不出**这种状态 —— `_check_whitelist_rules` 会拦下
    「已在黑名单时加白」。但冲突仍必须处理（外部系统写库、名单规则变更、
    历史数据都可能造成），所以这里用 SQL 直接造数据。
    """
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-CF", reason="黑")
    db.execute(text(
        "INSERT INTO risk_list (list_type, entity_type, entity_value, reason, created_by) "
        "VALUES ('白名单', '用户', 'T-CF', '白', 'pytest')"))
    db.commit()

    v = ls.check_lists(db, [ls.Subject("用户", "T-CF")])
    assert v.conflict is True
    assert v.decision == "人工审核"
    assert "冲突" in v.describe()


# ============================================================
# 四、白名单准入校验（AC-31 的三条）
# ============================================================
def test_whitelist_requires_expire_time(db):
    """AC-31 ①：不填有效期 → 拒绝保存（禁止永久加白）"""
    with pytest.raises(ls.ListExpireRequiredError):
        ls.add_list_item(db, list_type="白名单", entity_type="用户",
                         entity_value="T-W-1", reason="测试")


def test_whitelist_expire_beyond_limit_needs_confirmation(db):
    """AC-31 ③：有效期填 120 天 → 超过 90 天上限，需二次确认（force=True）"""
    too_long = datetime.now() + timedelta(days=settings.WHITELIST_MAX_DAYS + 30)

    with pytest.raises(ls.ListExpireTooLongError):
        ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="T-W-2",
                         reason="长期白名单", expire_time=too_long)

    item = ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="T-W-2",
                            reason="长期白名单", expire_time=too_long, force=True)
    assert item.list_id, "二次确认后应可保存"


def test_whitelist_conflicts_with_existing_blacklist(db):
    """AC-31 ②：对黑名单中的主体加白 → 拒绝并提示先移除黑名单"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-W-3", reason="先黑")
    with pytest.raises(ls.ListConflictBlacklistError) as exc:
        ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="T-W-3",
                         reason="后白", expire_time=_future())
    assert "黑名单" in str(exc.value)


def test_whitelist_within_limit_is_ok(db):
    """AC-31 边界：有效期正好在上限内 → 直接通过，无需 force"""
    item = ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="T-W-4",
                            reason="合规白名单",
                            expire_time=datetime.now() + timedelta(days=settings.WHITELIST_MAX_DAYS - 1))
    assert item.list_id


def test_invalid_entity_type_rejected(db):
    with pytest.raises(ls.ListServiceError):
        ls.add_list_item(db, list_type="黑名单", entity_type="身份证",
                         entity_value="T-X", reason="非法主体类型")


def test_empty_entity_value_rejected(db):
    with pytest.raises(ls.ListServiceError):
        ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="   ", reason="空值")


# ============================================================
# 五、批量导入（AC-34 / SPEC Q9）
# ============================================================
def test_import_all_valid_persists_all(db):
    rows = [{"list_type": "黑名单", "entity_type": "用户", "entity_value": f"T-OK-{i}"}
            for i in range(5)]
    report = ls.import_items(db, rows)

    assert report.total == 5
    assert report.ok is True
    assert report.imported == 5
    assert db.query(RiskList).filter(RiskList.entity_value.like("T-OK-%")).count() == 5


def test_import_rejects_whole_batch_on_any_error(db):
    """批量导入：**任一行有错则整批不入库**（SPEC Q9 决策）

    ⚠ 与 PRD **AC-34** 的口径不一致：AC-34 写的是「8 条成功入库，2 条标红，
    不阻断其余记录」。实现取 Q9 的「不做部分入库」（理由：导了一半的模糊状态
    排查成本高）。此冲突已记入 `specs/验收报告.md`，本用例按**实现**断言。
    """
    rows = [{"list_type": "黑名单", "entity_type": "用户", "entity_value": f"T-IMP-{i}"}
            for i in range(8)]
    rows.append({"list_type": "黑名单", "entity_type": "不存在的主体", "entity_value": "T-BAD-1"})
    rows.append({"list_type": "黑名单", "entity_type": "用户", "entity_value": ""})

    report = ls.import_items(db, rows)

    assert report.total == 10
    assert len(report.errors) == 2, "两行错误都应被逐条报出"
    assert report.ok is False
    assert report.imported == 0, "Q9：任一行有错则整体不入库"
    assert db.query(RiskList).filter(RiskList.entity_value.like("T-IMP-%")).count() == 0

    # 错误明细要能定位到行号 + 原因，供前端标红
    for err in report.errors:
        assert "row" in err and "message" in err


def test_import_over_limit_rejected_entirely(db):
    """SPEC Q9：超过 5000 行上限 → 拒绝整批，不做部分入库"""
    rows = [{"list_type": "黑名单", "entity_type": "用户", "entity_value": f"T-{i}"}
            for i in range(settings.LIST_IMPORT_MAX_ROWS + 1)]
    with pytest.raises(ls.ListImportTooLargeError) as exc:
        ls.import_items(db, rows)
    assert "上限" in str(exc.value)


# ============================================================
# 六、归档（AC-25 / FR-42）
# ============================================================
def test_archive_expired_moves_records(db):
    """AC-25：过期记录搬移到历史表，主表不再包含，且不影响判定逻辑"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-ARC-1",
                     reason="待归档", expire_time=datetime.now() - timedelta(days=1))

    archived = ls.archive_expired(db, operator="pytest")
    assert archived >= 1

    assert db.query(RiskList).filter(RiskList.entity_value == "T-ARC-1").count() == 0, \
        "主表应已不含该记录"
    row = db.query(RiskListArchive).filter(RiskListArchive.entity_value == "T-ARC-1").first()
    assert row is not None
    assert row.list_type == "黑名单"
    assert row.reason == "待归档"

    # 归档后判定逻辑不受影响（该主体本就不参与判定）
    assert ls.check_lists(db, [ls.Subject("用户", "T-ARC-1")]).decision is None


def test_archive_keeps_effective_records(db):
    """归档只搬「已过期」的，未过期的与永久有效的必须留在主表"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-KEEP-1", reason="永久")
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-KEEP-2",
                     reason="未过期", expire_time=_future())

    ls.archive_expired(db)

    assert db.query(RiskList).filter(RiskList.entity_value == "T-KEEP-1").count() == 1
    assert db.query(RiskList).filter(RiskList.entity_value == "T-KEEP-2").count() == 1


def test_archive_is_idempotent(db):
    """没有过期记录时，归档任务应返回 0 而不是报错（定时任务会反复调用）"""
    assert ls.archive_expired(db) == 0
    assert ls.archive_expired(db) == 0


def test_subject_history_reads_archive(db):
    """FR-42：归档表要能按主体独立检索历史（「某设备历史上被拉黑过几次」）"""
    ls.add_list_item(db, list_type="黑名单", entity_type="设备", entity_value="T-DEV-HIST",
                     reason="历史拉黑", expire_time=datetime.now() - timedelta(days=1))
    ls.archive_expired(db)

    history = ls.subject_history(db, entity_type="设备", entity_value="T-DEV-HIST")
    assert history["archived_count"] >= 1
    assert any(r["list_type"] == "黑名单" for r in history["archived"])


# ============================================================
# 七、主体收集（FR-06 五类主体的取值来源）
# ============================================================
def test_collect_subjects_from_event_context(db):
    """设备与 IP 来自本次事件上下文；用户来自请求；五类中去重后返回"""
    subjects = ls.collect_subjects(
        db, user_id="1002", device_id="D-9002", login_ip="203.0.113.7")
    by_type = {s.entity_type: s.entity_value for s in subjects}

    assert by_type.get("用户") == "1002"
    assert by_type.get("设备") == "D-9002"
    assert by_type.get("IP") == "203.0.113.7"
    assert len(subjects) == len({(s.entity_type, s.entity_value) for s in subjects}), "主体不应重复"


def test_collect_subjects_skips_missing_dimensions(db):
    """取不到值的维度直接跳过（不作为判定主体），不能塞空串进去"""
    subjects = ls.collect_subjects(db, user_id="1002")
    assert all(s.entity_value for s in subjects), "不应出现空值主体"
    assert {s.entity_type for s in subjects} <= {"用户", "手机号", "地址", "设备", "IP"}


# ============================================================
# 八、统计
# ============================================================
def test_list_statistics_counts_by_color_and_type(db):
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="T-S-1", reason="x")
    ls.add_list_item(db, list_type="黑名单", entity_type="设备", entity_value="T-S-2", reason="x")
    ls.add_list_item(db, list_type="灰名单", entity_type="用户", entity_value="T-S-3", reason="x")

    stats = ls.list_statistics(db)
    assert stats["by_list_type"]["黑名单"] >= 2
    assert stats["by_list_type"]["灰名单"] >= 1
    assert stats["total"] >= 3
