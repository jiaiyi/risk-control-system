# -*- coding: utf-8 -*-
"""
缓存层 —— Redis 旁路缓存（Cache-Aside）

设计约束（见 specs/SPEC.md 5.x / 8.x）：
  1. **缓存只加速「读」，不改变判定结果** —— 命中与否必须得到相同评分
  2. **降级优先** —— Redis 不可用时自动直查数据库，**风控检查不得因缓存故障而失败**
  3. **写后失效**（Write-Invalidate）—— 名单/规则/案件变更后主动删缓存
  4. **订单与商品维度特征禁止缓存**（含本次订单实时金额）

因此本模块对外只暴露 `safe_*` 系列方法：
任何 Redis 异常都会被吞掉并降级为「未命中」，绝不上抛中断风控主链路。
"""
import json
import logging
from typing import Any

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# ============================================================
# 键命名规范   risk:<域>:<维度>:<标识>
# ============================================================
K_FEATURE = "risk:feature"          # risk:feature:user:1002
K_PROFILE = "risk:profile"          # risk:profile:1002
K_GRAPH = "risk:graph"              # risk:graph:用户:U5001:2
K_RULES = "risk:rules"              # risk:rules:登录
K_LIST = "risk:list"                # risk:list:黑名单:用户:1002
K_LIST_ALL = "risk:list:all"        # 名单全量（用于冲突裁决）
K_AGENT_SESSION = "risk:agent:sess" # AI 助手会话

_client: "redis.Redis | None" = None
_client_ready = False


def get_client() -> "redis.Redis | None":
    """获取 Redis 客户端。

    返回 None 表示「缓存不可用」，调用方应直查数据库。
    只在这里做一次连接尝试，失败后不再反复重试（避免拖慢每次请求）。
    """
    global _client, _client_ready
    if not settings.REDIS_ENABLED:
        return None
    if _client_ready:
        return _client
    _client_ready = True
    try:
        c = redis.from_url(
            settings.get_redis_url(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        c.ping()
        _client = c
        logger.info("Redis 缓存已连接: %s", settings.get_redis_url())
    except Exception as exc:  # noqa: BLE001
        _client = None
        logger.warning("Redis 不可用，本次运行将直查数据库（降级）: %s", exc)
    return _client


def is_available() -> bool:
    """缓存当前是否可用（供健康检查与前端展示）"""
    return get_client() is not None


# ============================================================
# 安全读写：任何异常都降级，绝不上抛
# ============================================================
def safe_get(key: str) -> Any | None:
    """读缓存。未命中或任何异常 → 返回 None"""
    c = get_client()
    if c is None:
        return None
    try:
        raw = c.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存读取失败 [%s]，回源数据库: %s", key, exc)
        return None


def safe_set(key: str, value: Any, ttl: int) -> bool:
    """写缓存。返回是否成功。任何异常都吞掉"""
    c = get_client()
    if c is None:
        return False
    try:
        c.setex(key, ttl, json.dumps(value, ensure_ascii=False, default=str))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存写入失败 [%s]: %s", key, exc)
        return False


def safe_delete(*keys: str) -> int:
    """删除缓存（写后失效）。返回删除数量"""
    c = get_client()
    if c is None or not keys:
        return 0
    try:
        return c.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存删除失败 %s: %s", keys, exc)
        return 0


def safe_delete_prefix(prefix: str) -> int:
    """按前缀批量失效（用 SCAN，避免 KEYS 阻塞）。

    用于「规则变更后清空所有 event_type 的规则缓存」这类场景。
    """
    c = get_client()
    if c is None:
        return 0
    try:
        n = 0
        for key in c.scan_iter(match=f"{prefix}*", count=200):
            c.delete(key)
            n += 1
        return n
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存前缀失效失败 [%s]: %s", prefix, exc)
        return 0


# ============================================================
# 业务语义封装
# ============================================================
def feature_key(entity_type: str, entity_id: str) -> str:
    return f"{K_FEATURE}:{entity_type}:{entity_id}"


def profile_key(user_id: str) -> str:
    return f"{K_PROFILE}:{user_id}"


def graph_key(entity_type: str, entity_value: str, hops: int) -> str:
    return f"{K_GRAPH}:{entity_type}:{entity_value}:{hops}"


def rules_key(event_type: str) -> str:
    return f"{K_RULES}:{event_type}"


def list_key(entity_type: str, entity_value: str) -> str:
    """单个主体（五类之一）的三色名单命中结果

    按「主体」而非「整张名单表」缓存：判定时主体是已知的，命中率高且 value 小；
    名单表任何变更统一走 invalidate_list() 全量失效。
    """
    return f"{K_LIST}:{entity_type}:{entity_value}"


def agent_session_key(session_id: str) -> str:
    return f"{K_AGENT_SESSION}:{session_id}"


# 写后失效：业务变更时调用这些，避免脏读
def invalidate_rules() -> None:
    """规则变更（增删改启停）后调用"""
    safe_delete_prefix(K_RULES)


def invalidate_list() -> None:
    """名单变更后调用"""
    safe_delete_prefix(K_LIST)


def invalidate_profile(user_id: str) -> None:
    safe_delete(profile_key(user_id))


def invalidate_features(user_id: str) -> None:
    """用户相关特征全部失效（案件审核拒绝后调用）"""
    safe_delete(feature_key("user", user_id))


def stats() -> dict:
    """缓存状态摘要（供 /api/health/metrics 使用）"""
    c = get_client()
    if c is None:
        return {"available": False, "enabled": settings.REDIS_ENABLED}
    try:
        info = c.info("stats")
        keyspace = c.info("keyspace")
        db_key = f"db{settings.REDIS_DB}"
        return {
            "available": True,
            "enabled": True,
            "url": f"{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
            "keys": keyspace.get(db_key, {}).get("keys", 0),
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("缓存状态查询失败: %s", exc)
        return {"available": False, "enabled": True, "error": str(exc)}
