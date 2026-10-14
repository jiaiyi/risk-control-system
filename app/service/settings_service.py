# -*- coding: utf-8 -*-
"""系统配置服务 —— 阈值 / 缓存策略的在线调整 + 健康度指标 + 运维任务

职责（SPEC 3.2「系统配置模块」）：
    决策阈值与引擎参数的读写与**持久化**、变更审计、健康度指标聚合、
    运维任务的调度与手动触发。**不直接改写业务数据**。

持久化方案：
    项目未设配置表（29 张表的契约不为此破坏），改为写入
    `data/runtime_settings.json`，应用启动时 `apply_overrides()` 回灌。
    每次变更**强制写审计**（记录变更前后值），满足 FR-56 的可追溯要求。

合规：口径变更（阈值）属「配置」类操作，审计不可省。
"""
import json
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path

from sqlalchemy.orm import Session

from app import cache
from app.config import settings
from app.errors import ServiceError
from app.service import audit_service

logger = logging.getLogger(__name__)

# 允许在线调整的配置项（白名单 —— 只暴露与风控判定相关、且调整无害的参数）
THRESHOLD_FIELDS = (
    "RISK_PASS_THRESHOLD", "RISK_MARK_THRESHOLD", "RISK_REVIEW_THRESHOLD",
    "RISK_MULTI_RULE_BONUS", "RISK_VETO_MIN_SCORE", "RISK_GRAYLIST_BONUS",
    "PROFILE_ALPHA_HIGH", "PROFILE_ALPHA_MID", "PROFILE_ALPHA_LOW",
    "PROFILE_ACTIVE_THRESHOLD",
)
CACHE_FIELDS = (
    "REDIS_ENABLED", "CACHE_TTL_FEATURE", "CACHE_TTL_PROFILE",
    "CACHE_TTL_GRAPH", "CACHE_TTL_RULE",
)

class SettingsServiceError(ServiceError):
    code = "BIZ_SETTINGS_ERROR"


def _override_path() -> Path:
    # app/service/settings_service.py → <项目根>/data/runtime_settings.json
    return Path(__file__).resolve().parent.parent.parent / "data" / "runtime_settings.json"


# ============================================================
# 持久化
# ============================================================
def _load_overrides() -> dict:
    path = _override_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("运行时配置读取失败（忽略）：%s", exc)
        return {}


def _save_overrides(values: dict) -> None:
    path = _override_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise SettingsServiceError(f"运行时配置写入失败：{exc}") from exc


def apply_overrides() -> dict:
    """启动时把持久化的覆盖值回灌到内存 settings（由 lifespan 调用）"""
    overrides = _load_overrides()
    applied = {}
    for key, value in overrides.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
            applied[key] = value
    if applied:
        logger.info("已应用 %d 项运行时配置覆盖：%s", len(applied), applied)
    return applied


# ============================================================
# 阈值 / 引擎参数
# ============================================================
def get_thresholds() -> dict:
    """读取当前阈值与引擎参数（API-46）"""
    return {k: getattr(settings, k) for k in THRESHOLD_FIELDS}


def update_thresholds(db: Session, payload: dict, operator: str | None = None) -> dict:
    """在线调整阈值与引擎参数（API-47 / FR-56）

    校验 → 改内存 → 持久化 → 写审计（含前后值）。**同一事务内完成审计**。
    """
    changes = {k: v for k, v in payload.items()
               if k in THRESHOLD_FIELDS and v is not None}
    if not changes:
        raise SettingsServiceError("没有可调整的配置项",
                                   {"allowed": list(THRESHOLD_FIELDS)})

    before = {k: getattr(settings, k) for k in changes}

    # 阈值顺序必须自洽，否则决策映射会出现空档（如 通过阈值 > 标记阈值）
    merged = {**get_thresholds(), **changes}
    if not (merged["RISK_PASS_THRESHOLD"] <= merged["RISK_MARK_THRESHOLD"]
            <= merged["RISK_REVIEW_THRESHOLD"]):
        raise SettingsServiceError(
            "阈值顺序非法：必须满足 通过 ≤ 标记 ≤ 人工审核",
            {"pass": merged["RISK_PASS_THRESHOLD"],
             "mark": merged["RISK_MARK_THRESHOLD"],
             "review": merged["RISK_REVIEW_THRESHOLD"]},
        )

    for key, value in changes.items():
        setattr(settings, key, value)

    overrides = _load_overrides()
    overrides.update(changes)
    _save_overrides(overrides)

    audit_service.log(
        db, operator=operator, action_type="配置",
        action_desc=f"调整决策阈值（{len(changes)} 项）", target_id="thresholds",
        before=before, after=changes,
    )
    db.commit()

    logger.info("阈值调整: %s → %s（by %s）", before, changes, operator)
    return {"before": before, "after": changes, "current": get_thresholds()}


# ============================================================
# 缓存与降级策略
# ============================================================
def get_cache_policy() -> dict:
    """读取缓存与降级策略（API-48）"""
    policy = {k: getattr(settings, k) for k in CACHE_FIELDS}
    policy["available"] = cache.is_available()
    policy["note"] = "缓存不可用时自动降级直查数据库，风控主链路不受影响（NFR-14）"
    return policy


def update_cache_policy(db: Session, payload: dict, operator: str | None = None) -> dict:
    """调整缓存开关与 TTL（API-49 / FR-56）"""
    changes = {k: v for k, v in payload.items() if k in CACHE_FIELDS and v is not None}
    if not changes:
        raise SettingsServiceError("没有可调整的配置项", {"allowed": list(CACHE_FIELDS)})

    before = {k: getattr(settings, k) for k in changes}
    for key, value in changes.items():
        setattr(settings, key, value)

    overrides = _load_overrides()
    overrides.update(changes)
    _save_overrides(overrides)

    audit_service.log(
        db, operator=operator, action_type="配置",
        action_desc=f"调整缓存策略（{len(changes)} 项）", target_id="cache-policy",
        before=before, after=changes,
    )
    db.commit()

    logger.info("缓存策略调整: %s → %s（by %s）", before, changes, operator)
    return {"before": before, "after": changes, "current": get_cache_policy()}


# ============================================================
# 性能采样（S-11：性能可验证）
# ============================================================
class PerformanceTracker:
    """接口耗时采样（进程内环形缓冲）

    由 main.py 的中间件写入；`/api/health/metrics` 读取。
    只保留最近 N 条，避免长时间运行吃内存。
    """

    def __init__(self, maxlen: int = 2000):
        self._samples: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    # 把路径里的动态段归一：/api/cases/cas01m4wk9x2x... → /api/cases/{id}
    # 否则每个 ID 都会单独成一组，by_path 统计会被打散得没法看
    _DYNAMIC_SEG = re.compile(r"/(?:cas|evt|ast|chl|vt)[0-9a-z]{16,}|/\d+")

    def record(self, path: str, elapsed_ms: float, ok: bool = True) -> None:
        normalized = self._DYNAMIC_SEG.sub("/{id}", path)
        with self._lock:
            self._samples.append((normalized, elapsed_ms, ok, time.time()))

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()

    def metrics(self) -> dict:
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return {"requests": 0, "error_rate": 0, "p95_ms": 0, "avg_ms": 0,
                    "by_path": {}, "note": "尚未采集到请求样本"}

        latencies = sorted(s[1] for s in samples)
        errors = sum(1 for s in samples if not s[2])

        def _p95(values: list[float]) -> float:
            idx = max(int(len(values) * 0.95) - 1, 0)
            return round(values[idx], 2)

        by_path: dict[str, dict] = {}
        for path, ms, ok, _ts in samples:
            # 把动态段归一，避免每个 ID 一个分组（/api/cases/cas01x → /api/cases/{id}）
            bucket = by_path.setdefault(path, {"count": 0, "errors": 0, "latencies": []})
            bucket["count"] += 1
            bucket["errors"] += 0 if ok else 1
            bucket["latencies"].append(ms)
        for path, b in by_path.items():
            lat = sorted(b.pop("latencies"))
            b["p95_ms"] = _p95(lat)
            b["avg_ms"] = round(sum(lat) / len(lat), 2) if lat else 0

        return {
            "requests": len(samples),
            "error_rate": round(errors / len(samples), 4),
            "p95_ms": _p95(latencies),
            "avg_ms": round(sum(latencies) / len(latencies), 2),
            "max_ms": round(latencies[-1], 2),
            "by_path": dict(sorted(by_path.items(), key=lambda kv: -kv[1]["count"])[:10]),
        }


perf = PerformanceTracker()


def health_metrics(db: Session) -> dict:
    """健康度指标（API-50 / FR-56）：性能 + 缓存 + 依赖状态"""
    from app.service import dashboard_service

    cache_stats = cache.stats()
    total_gets = cache_stats.get("get_hit", 0) + cache_stats.get("get_miss", 0)
    hit_rate = round(cache_stats.get("get_hit", 0) / total_gets, 4) if total_gets else 0

    return {
        "performance": perf.metrics(),
        "cache": {**cache_stats, "hit_rate": hit_rate},
        "dependencies": {
            "mysql": _ping_mysql(db),
            "redis": cache_stats.get("available", False),
        },
        "engine": dashboard_service.health_snapshot(db),
    }


def _ping_mysql(db: Session) -> bool:
    from sqlalchemy import text
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


# ============================================================
# 运维任务（API-51 / API-52 / FR-42）
# ============================================================
def _task_archive_lists(db: Session) -> dict:
    from app.service import list_service
    n = list_service.archive_expired(db, operator="scheduler")
    return {"archived": n, "detail": f"清理 {n} 条过期名单（转入 risk_list_archive）"}


def _task_warmup_cache(db: Session) -> dict:
    """缓存预热：把启用规则与名单读一遍，让下一次检查直接命中"""
    from app.engine.rule import load_enabled_rules
    rules = load_enabled_rules(db)
    cache.safe_set(cache.rules_key("ALL"), {"count": len(rules)}, settings.CACHE_TTL_RULE)
    return {"rules_loaded": len(rules), "detail": f"已预热 {len(rules)} 条启用规则"}


def _task_health_check(db: Session) -> dict:
    from app.service import dashboard_service
    return {"healthy": _ping_mysql(db) and cache.is_available(),
            "detail": "依赖健康检查完成",
            "engine": dashboard_service.health_snapshot(db)["thresholds"]}


def _task_invalidate_cache(db: Session) -> dict:
    """手动清缓存（榜单/规则/画像改动后排查脏数据用）"""
    n = cache.safe_delete_prefix("risk:")
    return {"deleted_keys": n, "detail": f"已清理 {n} 个缓存键"}


TASKS = {
    "归档过期名单": _task_archive_lists,
    "预热缓存": _task_warmup_cache,
    "健康检查": _task_health_check,
    "清理缓存": _task_invalidate_cache,
}


def list_tasks() -> list[dict]:
    """运维任务列表（API-51）"""
    return [
        {"name": name, "description": (fn.__doc__ or "").strip().split("\n")[0]}
        for name, fn in TASKS.items()
    ]


def run_task(db: Session, name: str, operator: str | None = None) -> dict:
    """手动触发运维任务（API-52 / FR-42）

    任务本身有副作用（归档、清缓存），因此**记录审计**；但不属四类关键操作用的
    「配置」类别归口。
    """
    fn = TASKS.get(name)
    if fn is None:
        raise SettingsServiceError(
            f"未知任务：{name}（可选：{'/'.join(TASKS)}）", {"task": name})

    started = time.time()
    result = fn(db)
    elapsed = round((time.time() - started) * 1000, 2)

    audit_service.log(
        db, operator=operator, action_type="配置",
        action_desc=f"执行运维任务：{name}", target_id=f"task:{name}",
        after={**result, "elapsed_ms": elapsed},
        auto_commit=True,
    )
    logger.info("运维任务执行: %s → %s（%.1fms）", name, result, elapsed)
    return {"task": name, "elapsed_ms": elapsed, **result}
