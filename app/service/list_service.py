# -*- coding: utf-8 -*-
"""名单服务 —— 三色（黑 / 白 / 灰）× 五类主体（用户 / 手机号 / IP / 设备 / 地址）

职责边界（SPEC 3.2）：
    三色名单 CRUD、五类主体判定、批量导入、过期判定、定时归档。
    **不做拦截决策** —— 只返回判定结果，由事件管道（T-11）决定后续动作。

裁决口径（PRD FR-05 / FR-06 / FR-38，顺序不可调换）：
    ① 黑 + 白同时命中 → 人工审核（不做自动裁决，交人工处理；案件 conflict_flag=1）
    ② 仅黑名单命中   → 拒绝，评分 100，等级 极高
    ③ 仅白名单命中   → 通过，评分 0，等级 低
    ④ 仅灰名单命中   → 正常流程，把 graylist_hit=True 传给决策引擎（+15 分加严）
    ⑤ 均未命中       → 正常流程
    过期记录不参与判定（expire_time IS NULL 视为永久有效）。

设计取舍（SPEC ADR-04）：
    名单用**单表 + 类型字段**（risk_list），判定时一条 SQL 拿全三色，
    不写「三张表 → 三个分支 → 合并」的逻辑。

白名单硬约束（PRD 4.6）：
    · 必须设置有效期，且不超过 WHITELIST_MAX_DAYS（默认 90 天）—— 禁止永久加白
    · 超出上限需二次确认（force=True）
    · 主体当前在黑名单中时不得直接加白（先移除黑名单）
    · 加白 / 移除 / 白名单命中均留痕
"""
import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cache
from app.config import settings
from app.models import RiskAuditLog, RiskList, RiskListArchive

logger = logging.getLogger(__name__)

VALID_LIST_TYPES = ("黑名单", "白名单", "灰名单")
VALID_ENTITY_TYPES = ("用户", "手机号", "IP", "设备", "地址")

IMPORT_MAX_ROWS = settings.LIST_IMPORT_MAX_ROWS      # 单次导入上限（Q9：超限拒绝整批）

EXPECTED_IMPORT_COLUMNS = (
    ("list_type", ("list_type", "名单类型", "类型")),
    ("entity_type", ("entity_type", "主体类型", "主体")),
    ("entity_value", ("entity_value", "主体值", "值")),
    ("reason", ("reason", "原因", "备注")),
    ("expire_time", ("expire_time", "过期时间", "有效期至")),
    ("created_by", ("created_by", "操作人", "创建人")),
)

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d")


# ============================================================
# 业务异常（接口层据此转 HTTP 状态码，SPEC 6.3 错误码体系）
# ============================================================
class ListServiceError(Exception):
    code = "BIZ_LIST_ERROR"
    http_status = 400

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ListExpireRequiredError(ListServiceError):
    """白名单未设置有效期（PRD 4.6 验收 1）"""
    code = "BIZ_LIST_EXPIRE_REQUIRED"


class ListExpireTooLongError(ListServiceError):
    """白名单有效期超出上限，需二次确认（PRD 4.6 验收 2）"""
    code = "BIZ_LIST_EXPIRE_TOO_LONG"
    http_status = 409


class ListConflictBlacklistError(ListServiceError):
    """主体已在黑名单中，不得直接加白（PRD 4.6 验收 3）"""
    code = "BIZ_LIST_CONFLICT_BLACKLIST"
    http_status = 409


class ListDuplicateError(ListServiceError):
    """同一颜色 + 同一主体已存在"""
    code = "BIZ_LIST_DUPLICATE"
    http_status = 409


class ListNotFoundError(ListServiceError):
    code = "BIZ_LIST_NOT_FOUND"
    http_status = 404


class ListImportTooLargeError(ListServiceError):
    """导入文件超出上限，整批拒绝（SPEC Q9，不做部分入库）"""
    code = "BIZ_LIST_IMPORT_TOO_LARGE"


# ============================================================
# 判定结果数据结构
# ============================================================
@dataclass(frozen=True)
class Subject:
    """一个待判定的主体（五类之一）"""

    entity_type: str
    entity_value: str


@dataclass
class ListHit:
    """一条名单命中记录"""

    list_id: int
    list_type: str
    entity_type: str
    entity_value: str
    reason: str | None
    expire_time: datetime | None

    def to_dict(self) -> dict:
        return {
            "list_id": self.list_id,
            "list_type": self.list_type,
            "entity_type": self.entity_type,
            "entity_value": self.entity_value,
            "reason": self.reason,
            "expire_time": self.expire_time.isoformat(sep=" ", timespec="seconds") if self.expire_time else None,
        }


@dataclass
class ListVerdict:
    """一次名单判定的完整结果"""

    hits: list[ListHit] = field(default_factory=list)
    subjects: list[Subject] = field(default_factory=list)
    cache_hit: bool = False

    @property
    def black(self) -> list[ListHit]:
        return [h for h in self.hits if h.list_type == "黑名单"]

    @property
    def white(self) -> list[ListHit]:
        return [h for h in self.hits if h.list_type == "白名单"]

    @property
    def gray(self) -> list[ListHit]:
        return [h for h in self.hits if h.list_type == "灰名单"]

    @property
    def conflict(self) -> bool:
        """黑白名单同时命中 → 冲突（PRD FR-38）"""
        return bool(self.black) and bool(self.white)

    @property
    def decision(self) -> str | None:
        """名单前置裁决结果；**返回 None 表示「不拦截也不放行」，走正常风控流程**"""
        if self.conflict:
            return "人工审核"
        if self.black:
            return "拒绝"
        if self.white:
            return "通过"
        return None

    @property
    def graylist_hit(self) -> bool:
        return bool(self.gray)

    def describe(self) -> str:
        """供响应体展示的判定说明"""
        if self.conflict:
            b = self.black[0]
            w = self.white[0]
            return (f"名单冲突：主体 {b.entity_type}={b.entity_value} 同时命中黑名单"
                    f"（原因：{b.reason or '未填写'}）与白名单（原因：{w.reason or '未填写'}），移交人工审核")
        if self.black:
            h = self.black[0]
            return f"命中黑名单：{h.entity_type}={h.entity_value}（{h.reason or '未填写原因'}）"
        if self.white:
            h = self.white[0]
            return f"命中白名单：{h.entity_type}={h.entity_value}（{h.reason or '未填写原因'}）"
        if self.gray:
            h = self.gray[0]
            return f"命中灰名单：{h.entity_type}={h.entity_value}，评分加严 {settings.RISK_GRAYLIST_BONUS} 分"
        return "五类主体均未命中黑 / 白 / 灰名单"

    def to_dict(self) -> dict:
        return {
            "hit": bool(self.hits),
            "decision": self.decision,
            "conflict": self.conflict,
            "graylist_hit": self.graylist_hit,
            "black": [h.to_dict() for h in self.black],
            "white": [h.to_dict() for h in self.white],
            "gray": [h.to_dict() for h in self.gray],
            "subjects": [{"entity_type": s.entity_type, "entity_value": s.entity_value}
                         for s in self.subjects],
            "detail": self.describe(),
            "cache_hit": self.cache_hit,
        }


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _audit(
    db: Session, *, operator: str | None, action_desc: str, target_id,
    before: dict | None = None, after: dict | None = None,
) -> None:
    """写操作审计流水

    T-15 会把它抽成独立的 audit_service；此处内联，保证名单变更当下即可留痕
    （PRD 4.6：加白 / 移除 / 白名单命中均可查）。
    """
    db.add(RiskAuditLog(
        operator=operator or "system",
        action_type="名单",
        action_desc=action_desc,
        target_id=str(target_id),
        before_value=_json(before) if before is not None else None,
        after_value=_json(after) if after is not None else None,
    ))


def _row_to_hit(row) -> ListHit:
    return ListHit(
        list_id=row.list_id,
        list_type=row.list_type,
        entity_type=row.entity_type,
        entity_value=row.entity_value,
        reason=row.reason,
        expire_time=row.expire_time,
    )


# ============================================================
# 五类主体收集
# ============================================================
def format_address(row) -> str:
    """地址主体值的统一口径（名单登记与判定必须使用同一拼接方式，否则对不上）

    省 + 市 + 区 + 详细地址
    """
    return f"{row.receive_province}{row.receive_city}{row.receive_district}{row.receive_street_address}"


def _fetch_receive(db: Session, user_id: str, receive_id: str | None):
    """取收货信息：指定 receive_id 优先，否则取该用户任一条（PRD：手机号 / 地址来源）"""
    if receive_id:
        row = db.execute(
            text("SELECT * FROM receive_info WHERE receive_id = :rid"),
            {"rid": receive_id},
        ).first()
        if row:
            return row
    return db.execute(
        text("SELECT * FROM receive_info WHERE user_id = :uid ORDER BY receive_id LIMIT 1"),
        {"uid": user_id},
    ).first()


def collect_subjects(
    db: Session,
    *,
    user_id: str,
    receive_id: str | None = None,
    device_id: str | None = None,
    login_ip: str | None = None,
) -> list[Subject]:
    """收集本次检查涉及的五类主体取值（PRD FR-06「五类主体的查询来源」）

    · 用户        ← 请求的 user_id
    · 手机号/地址 ← 该用户关联的收货信息
    · 设备/IP     ← 本次事件上下文（下单等无设备信息时由决策引擎补该用户最近一次登录）
    取不到值的维度直接跳过（不作为判定主体）。
    """
    subjects: list[Subject] = []

    def push(entity_type: str, value: str | None) -> None:
        if value is not None and str(value).strip():
            subjects.append(Subject(entity_type, str(value).strip()))

    push("用户", user_id)

    receive = _fetch_receive(db, user_id, receive_id)
    if receive is not None:
        push("手机号", receive.receiver_phone)
        push("地址", format_address(receive))

    push("设备", device_id)
    push("IP", login_ip)

    return subjects


# ============================================================
# 名单判定
# ============================================================
_LOOKUP_SQL = """
SELECT list_id, list_type, entity_type, entity_value, reason, expire_time
FROM risk_list
WHERE ({clauses})
  AND (expire_time IS NULL OR expire_time > :now)
"""


def _query_hits(db: Session, subjects: list[Subject], now: datetime) -> list[ListHit]:
    """一条 SQL 拿全五类主体的三色命中（ADR-04：单表 + OR 子句，避免三分支）"""
    if not subjects:
        return []

    clauses = []
    params: dict = {"now": now}
    for i, s in enumerate(subjects):
        clauses.append(f"(entity_type = :t{i} AND entity_value = :v{i})")
        params[f"t{i}"] = s.entity_type
        params[f"v{i}"] = s.entity_value

    sql = _LOOKUP_SQL.format(clauses=" OR ".join(clauses))
    return [_row_to_hit(r) for r in db.execute(text(sql), params).fetchall()]


def check_lists(db: Session, subjects: list[Subject]) -> ListVerdict:
    """判定五类主体的三色名单命中情况

    缓存策略：以「主体」为粒度缓存其命中结果；未命中的主体合并成一次查询后回填。
    Redis 不可用时自动降级为直查数据库（NFR-14）。
    """
    verdict = ListVerdict(subjects=list(subjects))
    if not subjects:
        return verdict

    now = datetime.now()
    pending: list[Subject] = []
    all_cache_hit = True

    for s in subjects:
        cached = cache.safe_get(cache.list_key(s.entity_type, s.entity_value))
        if isinstance(cached, list):
            for item in cached:
                verdict.hits.append(ListHit(
                    list_id=item["list_id"],
                    list_type=item["list_type"],
                    entity_type=item["entity_type"],
                    entity_value=item["entity_value"],
                    reason=item.get("reason"),
                    expire_time=(
                        datetime.fromisoformat(item["expire_time"])
                        if item.get("expire_time") else None
                    ),
                ))
        else:
            all_cache_hit = False
            pending.append(s)

    if pending:
        fetched = _query_hits(db, pending, now)
        # 回填缓存：未命中的主体也缓存空列表，避免每次穿透到数据库
        grouped: dict[tuple[str, str], list[dict]] = {(s.entity_type, s.entity_value): [] for s in pending}
        for hit in fetched:
            grouped.setdefault((hit.entity_type, hit.entity_value), []).append(hit.to_dict())
        for s in pending:
            cache.safe_set(
                cache.list_key(s.entity_type, s.entity_value),
                grouped[(s.entity_type, s.entity_value)],
                settings.CACHE_TTL_FEATURE,
            )
        verdict.hits.extend(fetched)

    verdict.cache_hit = all_cache_hit and bool(subjects)

    if verdict.white:
        # PRD 4.6「滥用防护」：白名单命中必须落日志，便于核查是否被滥用
        logger.info("白名单命中: %s", verdict.describe())
    if verdict.black:
        logger.info("黑名单命中: %s", verdict.describe())
    if verdict.gray:
        logger.info("灰名单命中: %s", verdict.describe())

    return verdict


# ============================================================
# CRUD
# ============================================================
def _validate_enum(list_type: str, entity_type: str, entity_value: str) -> str:
    if list_type not in VALID_LIST_TYPES:
        raise ListServiceError(f"名单类型非法：{list_type}（可选：{'/'.join(VALID_LIST_TYPES)}）",
                               {"list_type": list_type})
    if entity_type not in VALID_ENTITY_TYPES:
        raise ListServiceError(f"主体类型非法：{entity_type}（可选：{'/'.join(VALID_ENTITY_TYPES)}）",
                               {"entity_type": entity_type})
    value = (entity_value or "").strip()
    if not value:
        raise ListServiceError("主体值不能为空", {"entity_type": entity_type})
    if len(value) > 200:
        raise ListServiceError("主体值长度不得超过 200 字符", {"length": len(value)})
    return value


def _check_whitelist_rules(
    db: Session, *, entity_type: str, entity_value: str,
    expire_time: datetime | None, force: bool,
) -> datetime:
    """白名单硬约束校验，返回最终有效期"""
    if expire_time is None:
        raise ListExpireRequiredError(
            f"白名单必须设置有效期（不超过 {settings.WHITELIST_MAX_DAYS} 天），禁止永久加白",
            {"entity_type": entity_type, "entity_value": entity_value},
        )

    limit = datetime.now() + timedelta(days=settings.WHITELIST_MAX_DAYS)
    if expire_time > limit and not force:
        raise ListExpireTooLongError(
            f"白名单有效期超过 {settings.WHITELIST_MAX_DAYS} 天上限，请确认后重试",
            {"expire_time": expire_time.isoformat(sep=" ", timespec="seconds"),
             "max_days": settings.WHITELIST_MAX_DAYS},
        )

    # 互斥约束：已在黑名单中的主体不得直接加白
    existing_black = db.execute(
        text("SELECT reason FROM risk_list WHERE list_type = '黑名单' "
             "AND entity_type = :t AND entity_value = :v LIMIT 1"),
        {"t": entity_type, "v": entity_value},
    ).first()
    if existing_black is not None:
        raise ListConflictBlacklistError(
            "该主体已在黑名单中，请先移除黑名单再加入白名单",
            {"entity_type": entity_type, "entity_value": entity_value,
             "blacklist_reason": existing_black.reason},
        )

    return expire_time


def add_list_item(
    db: Session,
    *,
    list_type: str,
    entity_type: str,
    entity_value: str,
    reason: str | None = None,
    expire_time: datetime | None = None,
    created_by: str | None = None,
    force: bool = False,
    auto_commit: bool = True,
) -> RiskList:
    """新增名单项（API-14 / FR-22）

    校验 → 写库 → 失效缓存 → 记审计，同一事务内完成。

    `auto_commit=False`：只 flush 不提交，事务交给调用方统一控制 ——
    案件审核（T-12）要把「加黑名单 + 回写业务表 + 更新案件」放在同一事务里，
    不能被中途 commit 打断。
    """
    value = _validate_enum(list_type, entity_type, entity_value)

    if list_type == "白名单":
        expire_time = _check_whitelist_rules(
            db, entity_type=entity_type, entity_value=value,
            expire_time=expire_time, force=force,
        )
    elif list_type == "灰名单" and expire_time is None:
        # 灰名单必须有观察期，默认 30 天（PRD 4.6 灰名单管理规范）
        expire_time = datetime.now() + timedelta(days=settings.GRAYLIST_DEFAULT_DAYS)

    duplicate = db.execute(
        text("SELECT list_id FROM risk_list WHERE list_type = :lt "
             "AND entity_type = :t AND entity_value = :v LIMIT 1"),
        {"lt": list_type, "t": entity_type, "v": value},
    ).first()
    if duplicate is not None:
        raise ListDuplicateError(
            f"该主体已在{list_type}中（list_id={duplicate.list_id}）",
            {"list_id": duplicate.list_id, "entity_type": entity_type, "entity_value": value},
        )

    item = RiskList(
        list_type=list_type,
        entity_type=entity_type,
        entity_value=value,
        reason=reason,
        expire_time=expire_time,
        created_by=created_by,
    )
    db.add(item)
    db.flush()      # 拿到自增 list_id，供审计 target_id 使用

    _audit(db, operator=created_by, action_desc=f"新增{list_type}", target_id=item.list_id,
           after={"list_type": list_type, "entity_type": entity_type, "entity_value": value,
                  "reason": reason,
                  "expire_time": expire_time.isoformat(sep=" ", timespec="seconds") if expire_time else None})

    if auto_commit:
        db.commit()
        cache.invalidate_list()     # 写后失效（ADR-03）

    logger.info("名单新增: %s %s=%s id=%s 有效期=%s", list_type, entity_type, value,
                item.list_id, expire_time)
    return item


def remove_list_item(db: Session, list_id: int, operator: str | None = None) -> RiskList:
    """移除名单项（API-15 / FR-23）"""
    item = db.query(RiskList).filter(RiskList.list_id == list_id).first()
    if item is None:
        raise ListNotFoundError(f"名单项不存在：{list_id}", {"list_id": list_id})

    before = {
        "list_type": item.list_type, "entity_type": item.entity_type,
        "entity_value": item.entity_value, "reason": item.reason,
        "expire_time": item.expire_time.isoformat(sep=" ", timespec="seconds") if item.expire_time else None,
    }
    db.delete(item)
    _audit(db, operator=operator, action_desc=f"移除{item.list_type}", target_id=list_id, before=before)
    db.commit()
    cache.invalidate_list()

    logger.info("名单移除: %s %s=%s id=%s", before["list_type"], before["entity_type"],
                before["entity_value"], list_id)
    return item


def list_items(
    db: Session,
    *,
    list_type: str | None = None,
    entity_type: str | None = None,
    keyword: str | None = None,
    include_expired: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskList]]:
    """名单列表查询（API-13 / FR-21），返回 (总数, 当前页)"""
    if list_type and list_type not in VALID_LIST_TYPES:
        raise ListServiceError(f"名单类型非法：{list_type}")
    if entity_type and entity_type not in VALID_ENTITY_TYPES:
        raise ListServiceError(f"主体类型非法：{entity_type}")

    query = db.query(RiskList)
    if list_type:
        query = query.filter(RiskList.list_type == list_type)
    if entity_type:
        query = query.filter(RiskList.entity_type == entity_type)
    if keyword:
        query = query.filter(RiskList.entity_value.like(f"%{keyword}%"))
    if not include_expired:
        now = datetime.now()
        query = query.filter((RiskList.expire_time.is_(None)) | (RiskList.expire_time > now))

    total = query.count()
    items = (query.order_by(RiskList.create_time.desc(), RiskList.list_id.desc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


# ============================================================
# 批量导入（FR-49 / API-16）
# ============================================================
def parse_import_file(filename: str, content: bytes) -> list[dict]:
    """解析导入文件（.xlsx / .csv），返回原始行字典

    仅做「读成行」，不校验业务规则 —— 校验统一在 import_items 里做，
    以保证「全量校验 → 一次性返回全部错误行 → 整批入库」的语义（SPEC Q9）。
    """
    lower = (filename or "").lower()
    if lower.endswith(".csv"):
        text_content = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text_content))
        raw_rows = [dict(r) for r in reader]
    elif lower.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            return []
        keys = [str(h).strip() if h is not None else "" for h in header]
        raw_rows = []
        for values in rows_iter:
            if values is None or all(v is None for v in values):
                continue
            raw_rows.append(dict(zip(keys, values)))
        wb.close()
    else:
        raise ListServiceError("仅支持 .xlsx 或 .csv 文件", {"filename": filename})

    return _normalize_columns(raw_rows)


def _normalize_columns(raw_rows: list[dict]) -> list[dict]:
    """把中英文表头统一映射为标准字段名"""
    normalized: list[dict] = []
    for raw in raw_rows:
        item: dict = {}
        for field_name, aliases in EXPECTED_IMPORT_COLUMNS:
            for alias in aliases:
                if alias in raw and raw[alias] not in (None, ""):
                    item[field_name] = raw[alias]
                    break
        normalized.append(item)
    return normalized


def _parse_expire_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    raise ListServiceError(f"过期时间格式无法解析：{value}（支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）",
                           {"expire_time": text_value})


@dataclass
class ImportReport:
    """导入结果：任一行有错则整体不入库（SPEC Q9）"""

    total: int = 0
    imported: int = 0
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "imported": self.imported,
            "failed": len(self.errors),
            "errors": self.errors,
            "success": self.ok,
        }


def import_items(
    db: Session, rows: list[dict], *, operator: str | None = None, force: bool = False,
) -> ImportReport:
    """批量导入名单项

    流程：**全量校验 → 返回全部错误行 → 全部通过才整体入库**。
    不做部分入库 —— 「导了一半」的模糊状态排查成本高（SPEC Q9）。
    """
    report = ImportReport(total=len(rows))

    if len(rows) > IMPORT_MAX_ROWS:
        raise ListImportTooLargeError(
            f"文件超出上限（{len(rows)} > {IMPORT_MAX_ROWS} 行），请拆分后重试",
            {"rows": len(rows), "max_rows": IMPORT_MAX_ROWS},
        )

    pending: list[dict] = []
    for idx, raw in enumerate(rows, start=2):     # 第 1 行是表头，数据从第 2 行起
        try:
            list_type = str(raw.get("list_type") or "").strip()
            entity_type = str(raw.get("entity_type") or "").strip()
            entity_value = _validate_enum(list_type, entity_type, raw.get("entity_value") or "")
            expire_time = _parse_expire_time(raw.get("expire_time"))

            if list_type == "白名单":
                expire_time = _check_whitelist_rules(
                    db, entity_type=entity_type, entity_value=entity_value,
                    expire_time=expire_time, force=force,
                )
            elif list_type == "灰名单" and expire_time is None:
                expire_time = datetime.now() + timedelta(days=settings.GRAYLIST_DEFAULT_DAYS)

            duplicate = db.execute(
                text("SELECT list_id FROM risk_list WHERE list_type = :lt "
                     "AND entity_type = :t AND entity_value = :v LIMIT 1"),
                {"lt": list_type, "t": entity_type, "v": entity_value},
            ).first()
            if duplicate is not None:
                raise ListDuplicateError(f"该主体已在{list_type}中")
        except ListServiceError as exc:
            report.errors.append({"row": idx, "code": exc.code, "message": exc.message})
            continue

        pending.append({
            "list_type": list_type, "entity_type": entity_type, "entity_value": entity_value,
            "reason": raw.get("reason"), "expire_time": expire_time,
            "created_by": raw.get("created_by") or operator,
        })

    if report.errors:
        logger.warning("名单导入校验未通过：%d/%d 行有误，整体不入库", len(report.errors), report.total)
        return report      # 全量校验失败 → 一行都不写

    for item in pending:
        db.add(RiskList(**item))

    _audit(db, operator=operator, action_desc="批量导入名单",
           target_id=f"import:{len(pending)}",
           after={"count": len(pending), "rows": len(rows)})

    db.commit()
    cache.invalidate_list()

    report.imported = len(pending)
    logger.info("名单批量导入成功：%d 行", report.imported)
    return report


def build_import_template() -> bytes:
    """生成导入模板（API-17），含中文表头与一行示例"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "名单导入"
    ws.append([name for _, aliases in EXPECTED_IMPORT_COLUMNS for name in aliases[:1]])
    ws.append(["黑名单", "设备", "D-MALICIOUS-0001", "批量注册小号", "", "admin"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ============================================================
# 归档（FR-42，P2）
# ============================================================
def archive_expired(
    db: Session, operator: str = "system", reason: str = "过期自动归档",
) -> int:
    """把已过期的名单记录**归档到 `risk_list_archive`** 并清理原表（FR-42）

    两条留痕各司其职：
      · `risk_list_archive`（本函数主路径）—— 存**数据**，按主体可独立检索历史名单
      · `risk_audit_log`（附带）—— 存**操作**，记录「谁在何时触发了这次归档」

    归档表不设唯一约束，同一主体被反复加入 / 移除会产生多条归档记录，
    由 `archive_time` 区分批次。
    """
    now = datetime.now()
    expired = db.query(RiskList).filter(
        RiskList.expire_time.isnot(None), RiskList.expire_time <= now
    ).all()

    if not expired:
        return 0

    for item in expired:
        db.add(RiskListArchive(
            original_list_id=item.list_id,
            list_type=item.list_type,
            entity_type=item.entity_type,
            entity_value=item.entity_value,
            reason=item.reason,
            expire_time=item.expire_time,
            created_by=item.created_by,
            create_time=item.create_time,
            archive_time=now,
            archived_by=operator,
            archive_reason=reason,
        ))
        db.delete(item)

    _audit(db, operator=operator, action_desc="归档过期名单",
           target_id=f"archive:{len(expired)}",
           before={"count": len(expired),
                   "list_ids": [i.list_id for i in expired]})
    db.commit()
    cache.invalidate_list()

    logger.info("名单归档：%d 条过期记录已转入 risk_list_archive（%s）", len(expired), reason)
    return len(expired)


def list_archived(
    db: Session,
    *,
    list_type: str | None = None,
    entity_type: str | None = None,
    entity_value: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[RiskListArchive]]:
    """检索历史名单（归档记录），支持按主体 / 类型 / 归档时间范围筛选

    典型用法：`list_archived(db, entity_type="设备", entity_value="D-XXX")`
    —— 查某个设备历史上被拉黑 / 加白过几次。
    """
    if list_type and list_type not in VALID_LIST_TYPES:
        raise ListServiceError(f"名单类型非法：{list_type}")
    if entity_type and entity_type not in VALID_ENTITY_TYPES:
        raise ListServiceError(f"主体类型非法：{entity_type}")

    query = db.query(RiskListArchive)
    if list_type:
        query = query.filter(RiskListArchive.list_type == list_type)
    if entity_type:
        query = query.filter(RiskListArchive.entity_type == entity_type)
    if entity_value:
        query = query.filter(RiskListArchive.entity_value == entity_value)
    if start_time:
        query = query.filter(RiskListArchive.archive_time >= start_time)
    if end_time:
        query = query.filter(RiskListArchive.archive_time <= end_time)

    total = query.count()
    items = (query.order_by(RiskListArchive.archive_time.desc(),
                            RiskListArchive.archive_id.desc())
             .offset(max(page - 1, 0) * page_size).limit(page_size).all())
    return total, items


def subject_history(db: Session, entity_type: str, entity_value: str) -> dict:
    """某个主体的完整名单履历：当前生效 + 历史归档

    供案件详情 / 名单管理页展示「这个主体被处理过几次」。
    """
    now = datetime.now()
    current = db.query(RiskList).filter(
        RiskList.entity_type == entity_type,
        RiskList.entity_value == entity_value,
    ).all()
    archived = db.query(RiskListArchive).filter(
        RiskListArchive.entity_type == entity_type,
        RiskListArchive.entity_value == entity_value,
    ).order_by(RiskListArchive.archive_time.desc()).all()

    return {
        "entity_type": entity_type,
        "entity_value": entity_value,
        "current": [
            {"list_id": i.list_id, "list_type": i.list_type, "reason": i.reason,
             "expire_time": i.expire_time.isoformat(sep=" ", timespec="seconds") if i.expire_time else None,
             "active": i.expire_time is None or i.expire_time > now}
            for i in current
        ],
        "archived_count": len(archived),
        "archived": [
            {"archive_id": a.archive_id, "original_list_id": a.original_list_id,
             "list_type": a.list_type, "reason": a.reason,
             "archive_time": a.archive_time.isoformat(sep=" ", timespec="seconds") if a.archive_time else None,
             "archive_reason": a.archive_reason}
            for a in archived
        ],
    }


# ============================================================
# 统计（供仪表盘 / 健康度使用）
# ============================================================
def list_statistics(db: Session) -> dict:
    """名单统计：三色 × 五类的分布，以及过期数量"""
    now = datetime.now()
    rows = db.execute(text(
        "SELECT list_type, entity_type, COUNT(*) AS c FROM risk_list GROUP BY list_type, entity_type"
    )).fetchall()
    by_type: dict[str, int] = {t: 0 for t in VALID_LIST_TYPES}
    by_entity: dict[str, int] = {t: 0 for t in VALID_ENTITY_TYPES}
    for r in rows:
        by_type[r.list_type] = by_type.get(r.list_type, 0) + r.c
        by_entity[r.entity_type] = by_entity.get(r.entity_type, 0) + r.c

    expired = db.execute(text(
        "SELECT COUNT(*) AS c FROM risk_list WHERE expire_time IS NOT NULL AND expire_time <= :now"
    ), {"now": now}).first()

    total = db.execute(text("SELECT COUNT(*) AS c FROM risk_list")).first()
    archived = db.execute(text("SELECT COUNT(*) AS c FROM risk_list_archive")).first()
    return {
        "total": total.c if total else 0,
        "by_list_type": by_type,
        "by_entity_type": by_entity,
        "expired": expired.c if expired else 0,
        "archived": archived.c if archived else 0,
    }
