# -*- coding: utf-8 -*-
"""T-10 名单服务冒烟验证

前置：先跑 `python scripts/init_db.py` 重建业务数据（本脚本只清风控表）。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_list.py

验证：
    1. 五类主体收集口径
    2. 三色判定 + 黑白冲突裁决（PRD FR-05/06/38）
    3. 过期记录不参与判定
    4. 白名单硬约束（必须有有效期 / 不超 90 天 / 与黑名单互斥）
    5. 灰名单默认观察期
    6. CRUD + 审计留痕
    7. 批量导入：全量校验、一行错则整体不入库、超限拒绝整批
    8. 缓存命中与写后失效
    9. 过期归档
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 供 import _smoke
import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from app import cache  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import RiskAuditLog, RiskList, RiskListArchive  # noqa: E402
from app.service import list_service as ls  # noqa: E402






def expect_error(fn, exc_type, msg: str) -> Exception | None:
    """断言某个调用抛出指定业务异常"""
    try:
        fn()
    except exc_type as exc:  # noqa: BLE001
        check(True, f"{msg}（{exc.code}）")
        return exc
    except Exception as exc:  # noqa: BLE001
        check(False, f"{msg} —— 却抛出了 {type(exc).__name__}: {exc}")
        return None
    check(False, f"{msg} —— 但未抛出异常")
    return None


# 选一个有收货信息的用户（1002 是「撞库试登」账号，数据里没有收货地址，
# 会导致手机号 / 地址两类主体收集不到）
USER = "1003"
DEVICE = "D-SHARED-0001"
IP = "172.16.0.10"

db = SessionLocal()

# 清空风控表，保证脚本可重复执行
for model in (RiskList, RiskListArchive, RiskAuditLog):
    db.query(model).delete()
db.commit()
cache.invalidate_list()

# ============================================================
section("一、五类主体收集（PRD FR-06「五类主体的查询来源」）")

subjects = ls.collect_subjects(db, user_id=USER, device_id=DEVICE, login_ip=IP)
types = [s.entity_type for s in subjects]
print(f"  主体：{[f'{s.entity_type}={s.entity_value}' for s in subjects]}")
check(types == ["用户", "手机号", "地址", "设备", "IP"], f"五类主体齐全且顺序正确（{types}）")
check(any(s.entity_type == "手机号" and s.entity_value for s in subjects), "手机号取自收货信息")
check(any(s.entity_type == "地址" and s.entity_value for s in subjects), "地址由省市区+详址拼接而成")

no_receive = ls.collect_subjects(db, user_id=USER, device_id=None, login_ip=None)
check(len(no_receive) == 3,
      f"无设备/IP 时仍收集到「用户+手机号+地址」3 类主体（{len(no_receive)}）")

# ============================================================
section("二、三色判定与黑白冲突裁决（PRD FR-05 / FR-06 / FR-38）")

verdict = ls.check_lists(db, subjects)
check(verdict.decision is None and not verdict.hits,
      "空名单 → 不裁决且无命中（走正常流程）")

# ① 仅黑名单 → 拒绝
black_item = ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value=USER,
                              reason="撞库试登", created_by="tester")
verdict = ls.check_lists(db, subjects)
check(verdict.black and not verdict.white, "仅黑名单命中")
check(verdict.decision == "拒绝", f"仅黑名单 → 决策「拒绝」（实际 {verdict.decision}）")

# ② 仅白名单 → 通过（先移除黑名单：白名单不允许与黑名单共存，只能走合法路径）
ls.remove_list_item(db, black_item.list_id, operator="admin")
white_item = ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value=USER,
                              reason="战略客户误判澄清", created_by="admin",
                              expire_time=datetime.now() + timedelta(days=30))
verdict = ls.check_lists(db, subjects)
check(verdict.white and not verdict.black, "仅白名单命中")
check(verdict.decision == "通过", f"仅白名单 → 决策「通过」（实际 {verdict.decision}）")

# ③ 再叠加黑名单 → 黑白冲突（黑名单的添加不受白名单限制）
ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value=USER,
                 reason="后续确认的恶意行为", created_by="tester")
verdict = ls.check_lists(db, subjects)
check(verdict.conflict, "黑白同时命中 → conflict=True")
check(verdict.decision == "人工审核", f"冲突 → 决策「人工审核」（实际 {verdict.decision}）")
check("冲突" in verdict.describe() and "黑名单" in verdict.describe() and "白名单" in verdict.describe(),
      f"判定说明标注了冲突双方：{verdict.describe()[:52]}…")

# ④ 清空黑白，改为仅灰名单 → 不裁决，但置 graylist_hit
for item in db.query(RiskList).filter(RiskList.entity_value == USER).all():
    ls.remove_list_item(db, item.list_id, operator="admin")
ls.add_list_item(db, list_type="灰名单", entity_type="设备", entity_value=DEVICE,
                 reason="观察期主体", created_by="tester")
verdict = ls.check_lists(db, subjects)
check(verdict.decision is None, f"仅灰名单 → 不做拦截决策（实际 {verdict.decision}）")
check(verdict.graylist_hit, "graylist_hit=True（交决策引擎 +15 加严）")
check("加严" in verdict.describe(), "判定说明提示加严分值")

# ============================================================
section("三、过期记录不参与判定（PRD FR-06 第 3 步 / AC-4）")

db.add(RiskList(list_type="黑名单", entity_type="IP", entity_value=IP,
                reason="历史过期记录", expire_time=datetime.now() - timedelta(days=1)))
db.commit()
cache.invalidate_list()

verdict = ls.check_lists(db, subjects)
check(not verdict.black, "已过期黑名单记录不触发拦截")
check(verdict.decision is None, f"过期记录未影响裁决（实际 {verdict.decision}）")

db.query(RiskList).filter(RiskList.entity_type == "IP").delete()
db.commit()
cache.invalidate_list()

# ============================================================
section("四、白名单硬约束（PRD 4.6）")

ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="U-BLACK-1",
                 reason="构造互斥场景", created_by="tester")

expect_error(
    lambda: ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="U-W-1",
                             reason="未填有效期", created_by="admin"),
    ls.ListExpireRequiredError, "白名单不设有效期 → 拒绝保存",
)
expect_error(
    lambda: ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="U-W-2",
                             reason="超 90 天", created_by="admin",
                             expire_time=datetime.now() + timedelta(days=91)),
    ls.ListExpireTooLongError, "白名单有效期超 90 天 → 提示需二次确认",
)
ok_item = ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="U-W-2",
                           reason="超 90 天但已确认", created_by="admin",
                           expire_time=datetime.now() + timedelta(days=91), force=True)
check(ok_item.list_id is not None, "force=True 时超长有效期可保存（二次确认路径）")

expect_error(
    lambda: ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="U-BLACK-1",
                             reason="已在黑名单", created_by="admin",
                             expire_time=datetime.now() + timedelta(days=30)),
    ls.ListConflictBlacklistError, "已在黑名单中的主体加白 → 拒绝并提示先移除",
)

# ============================================================
section("五、灰名单默认观察期 + 重复校验")

gray = ls.add_list_item(db, list_type="灰名单", entity_type="用户", entity_value="U-GRAY-1",
                        reason="观察期", created_by="tester")
days = round((gray.expire_time - datetime.now()).total_seconds() / 86400)
check(days == settings.GRAYLIST_DEFAULT_DAYS,
      f"灰名单未填有效期 → 默认 {settings.GRAYLIST_DEFAULT_DAYS} 天（实际 {days} 天）")

expect_error(
    lambda: ls.add_list_item(db, list_type="灰名单", entity_type="用户", entity_value="U-GRAY-1",
                             reason="重复", created_by="tester"),
    ls.ListDuplicateError, "同色同主体重复添加 → 拒绝",
)

# ============================================================
section("六、CRUD + 审计留痕（PRD 4.6「滥用防护」）")

total, items = ls.list_items(db, list_type="灰名单", page=1, page_size=10)
check(total >= 2 and all(i.list_type == "灰名单" for i in items),
      f"按名单类型筛选生效（{total} 条灰名单）")

total_kw, items_kw = ls.list_items(db, keyword="U-GRAY", page=1, page_size=10)
check(total_kw == 1 and items_kw[0].entity_value == "U-GRAY-1", "按主体值模糊搜索生效")

audit_rows = db.query(RiskAuditLog).filter(RiskAuditLog.action_type == "名单").all()
actions = [a.action_desc for a in audit_rows]
check(any("新增" in a for a in actions), f"新增操作已写审计（共 {len(audit_rows)} 条流水）")
check(any("移除" in a for a in actions), "移除操作已写审计")
sample = [a for a in audit_rows if "移除" in a.action_desc]
check(sample and sample[0].before_value and "entity_value" in sample[0].before_value,
      "移除操作审计记录了变更前值（可追溯）")

# ============================================================
section("七、批量导入（FR-49 / SPEC Q9：全量校验、一行错则整批不入库）")

before_count = db.query(RiskList).count()
report = ls.import_items(db, [
    {"list_type": "黑名单", "entity_type": "设备", "entity_value": "D-IMP-001",
     "reason": "批量导入", "created_by": "importer"},
    {"list_type": "黑名单", "entity_type": "设备", "entity_value": "D-IMP-002",
     "reason": "批量导入"},
], operator="importer")
check(report.ok and report.imported == 2, f"合法数据整体入库（imported={report.imported}）")
check(db.query(RiskList).count() == before_count + 2, "数据库中确实多了 2 条")

before_count = db.query(RiskList).count()
bad_report = ls.import_items(db, [
    {"list_type": "黑名单", "entity_type": "设备", "entity_value": "D-IMP-003"},
    {"list_type": "黑名单", "entity_type": "不存在的类型", "entity_value": "X"},
    {"list_type": "白名单", "entity_type": "IP", "entity_value": "1.2.3.4"},   # 白名单缺有效期
], operator="importer")
check(not bad_report.ok and len(bad_report.errors) == 2,
      f"整批校验发现 2 行错误（{len(bad_report.errors)}）")
check(bad_report.imported == 0, "有错误行时不导入任何数据")
check(db.query(RiskList).count() == before_count,
      "第三行合法 + 前两行有误 → 整批不入库（连合法行也不写）")
check(all("row" in e and "message" in e for e in bad_report.errors),
      f"错误明细含行号与原因：{bad_report.errors[0]}")

too_many = [{"list_type": "黑名单", "entity_type": "设备", "entity_value": f"D-{i}"}
            for i in range(settings.LIST_IMPORT_MAX_ROWS + 1)]
expect_error(lambda: ls.import_items(db, too_many, operator="importer"),
             ls.ListImportTooLargeError,
             f"超过 {settings.LIST_IMPORT_MAX_ROWS} 行 → 拒绝整批（不做部分入库）")

# 模板可生成
template = ls.build_import_template()
check(template[:2] == b"PK", f"导入模板可生成（{len(template)} 字节 xlsx）")

# 文件名分发
try:
    ls.parse_import_file("x.txt", b"abc")
    check(False, "非 xlsx/csv 文件应被拒绝")
except ls.ListServiceError:
    check(True, "非 xlsx/csv 文件被拒绝")

# ============================================================
section("八、缓存命中与写后失效（NFR-05 / ADR-03）")

cache.invalidate_list()
v1 = ls.check_lists(db, subjects)
v2 = ls.check_lists(db, subjects)
check(v1.cache_hit is False, "首次判定未命中缓存（穿透到数据库）")
check(v2.cache_hit is True, "二次判定命中缓存")
check(len(v1.hits) == len(v2.hits), f"缓存与直查结果一致（{len(v1.hits)} 条命中）")

ls.add_list_item(db, list_type="黑名单", entity_type="IP", entity_value=IP,
                 reason="缓存失效验证", created_by="tester")
v3 = ls.check_lists(db, subjects)
check(v3.cache_hit is False, "名单变更后缓存已被写后失效")
check(any(h.entity_type == "IP" for h in v3.black), "新增的黑名单立即生效（无脏读）")

# ============================================================
section("九、过期归档 + 历史名单独立检索（FR-42）")

db.add(RiskList(list_type="黑名单", entity_type="用户", entity_value="U-ARCH-1",
                reason="第一次拉黑", expire_time=datetime.now() - timedelta(days=2)))
db.add(RiskList(list_type="灰名单", entity_type="用户", entity_value="U-ARCH-2",
                reason="待归档", expire_time=datetime.now() - timedelta(days=1)))
db.commit()

before_count = db.query(RiskList).count()
archived = ls.archive_expired(db, operator="scheduler")
check(archived == 2, f"归档清理 2 条过期记录（实际 {archived}）")
check(db.query(RiskList).count() == before_count - 2, "过期记录已从名单表移除")
check(db.query(RiskListArchive).count() == 2, "2 条已转入归档表（不是直接丢弃）")

# 归档内容完整性
row = db.query(RiskListArchive).filter(RiskListArchive.entity_value == "U-ARCH-1").first()
check(row is not None, "可按主体值检索到归档记录")
check(row.list_type == "黑名单" and row.reason == "第一次拉黑",
      f"归档保留了原始名单类型与原因（{row.list_type} / {row.reason}）")
check(row.original_list_id >= 1,
      f"保留原名单 ID 便于与原表对照（original_list_id={row.original_list_id}）")
check(row.archived_by == "scheduler" and row.archive_reason == "过期自动归档",
      f"记录了归档触发者与原因（{row.archived_by} / {row.archive_reason}）")
check(row.expire_time is not None and row.create_time is not None,
      "保留了原过期时间与创建时间")

# 同一主体第二次进出 → 归档可累积，不覆盖
ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="U-ARCH-1",
                 reason="第二次拉黑", created_by="tester")
db.query(RiskList).filter(RiskList.entity_value == "U-ARCH-1").update(
    {"expire_time": datetime.now() - timedelta(hours=1)})
db.commit()
ls.archive_expired(db, operator="scheduler")

total_hist, hist = ls.list_archived(db, entity_type="用户", entity_value="U-ARCH-1")
check(total_hist == 2, f"同一主体两次进出 → 2 条归档记录（实际 {total_hist}）")
check({h.reason for h in hist} == {"第一次拉黑", "第二次拉黑"},
      "两次归档的原因都可检索到（归档累积、不覆盖）")

total_black, _ = ls.list_archived(db, list_type="黑名单")
check(total_black == 2, f"按名单类型筛选归档（{total_black} 条黑名单归档）")

total_recent, _ = ls.list_archived(db, start_time=datetime.now() - timedelta(hours=1))
check(total_recent >= 2, f"按归档时间范围筛选（近 1 小时 {total_recent} 条）")

hist_payload = ls.subject_history(db, "用户", "U-ARCH-1")
check(hist_payload["archived_count"] == 2 and hist_payload["current"] == [],
      f"主体履历=归档 2 条 + 当前生效 0 条（{hist_payload['archived_count']}）")

arch_audit = (db.query(RiskAuditLog)
              .filter(RiskAuditLog.action_desc == "归档过期名单")
              .order_by(RiskAuditLog.log_id.desc()).first())
check(arch_audit is not None and arch_audit.before_value and "list_ids" in arch_audit.before_value,
      "归档动作同时留有审计流水（数据 + 操作 两条留痕）")

stats = ls.list_statistics(db)
check("by_list_type" in stats and stats["total"] == db.query(RiskList).count(),
      f"统计口径正确：{stats}")
check("archived" in stats, f"统计含归档数量（archived={stats.get('archived')}）")

db.close()

# ============================================================
sys.exit(smoke.summary())
