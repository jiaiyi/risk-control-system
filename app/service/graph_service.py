# -*- coding: utf-8 -*-
"""图谱服务 —— 四维度实体关联 + 多跳扩散

业务价值（PRD FR-07）：
    单账号行为可能看着正常，但**同一设备 / 同一 IP 下聚集多个账号**往往是团伙化作案的铁证
    —— 把「单点判定」升级为「群体判定」。

设计取舍（SPEC ADR-06）：
    关联维度只有 4 类（设备 / IP / 手机号 / 地址）、跳数 ≤3，
    用 **MySQL 邻接查询**即可满足，不引入图数据库（运维成本远超收益）。

跳数与节点上限（NFR-06 / SPEC Q3）：
    跳数 ≤ `GRAPH_MAX_HOPS`(3)，默认 2；每跳新增节点 ≤ `GRAPH_MAX_NODES`(200)。
    超限立即截断并返回 `truncated=true`，**不继续扩散**。

数据来源：
    · 用户 ↔ 设备：`user_login_record` ∪ `coupon_receive_record` 的 device_id
    · 用户 ↔ IP  ：`user_login_record` 的 login_ip
    · 用户 ↔ 手机号：`receive_info` 的 receiver_phone
    · 用户 ↔ 地址：`receive_info` 的「省市区+详址」拼接值（与名单登记的地址口径一致）
"""
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cache
from app.errors import ServiceError
from app.config import settings

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = ("用户", "手机号", "IP", "设备", "地址")

# 关联关系的中文标签（前端边标签用）
REL_LABEL = {
    ("用户", "设备"): "使用设备",
    ("设备", "用户"): "设备上的账号",
    ("用户", "IP"): "登录IP",
    ("IP", "用户"): "该IP上的账号",
    ("用户", "手机号"): "收货手机",
    ("手机号", "用户"): "同手机号账号",
    ("用户", "地址"): "收货地址",
    ("地址", "用户"): "同地址账号",
}


class GraphServiceError(ServiceError):
    code = "BIZ_GRAPH_ERROR"
    http_status = 400


# ============================================================
# 邻接查询：给定一个节点，返回它的直接邻居
# ============================================================
def _neighbors_of_user(db: Session, user_id: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    for r in db.execute(text(
        "SELECT DISTINCT device_id AS v FROM user_login_record "
        "WHERE user_id = :u AND device_id IS NOT NULL "
        "UNION SELECT DISTINCT device_id FROM coupon_receive_record "
        "WHERE user_id = :u AND device_id IS NOT NULL"
    ), {"u": user_id}):
        out.append(("设备", r.v))

    for r in db.execute(text(
        "SELECT DISTINCT login_ip AS v FROM user_login_record "
        "WHERE user_id = :u AND login_ip IS NOT NULL"
    ), {"u": user_id}):
        out.append(("IP", r.v))

    for r in db.execute(text(
        "SELECT DISTINCT receiver_phone AS v FROM receive_info "
        "WHERE user_id = :u AND receiver_phone IS NOT NULL"
    ), {"u": user_id}):
        out.append(("手机号", r.v))

    for r in db.execute(text(
        "SELECT DISTINCT CONCAT(receive_province, receive_city, receive_district, "
        "receive_street_address) AS v FROM receive_info WHERE user_id = :u"
    ), {"u": user_id}):
        if r.v:
            out.append(("地址", r.v))

    return out


def _neighbors_of_device(db: Session, device_id: str) -> list[tuple[str, str]]:
    rows = db.execute(text(
        "SELECT DISTINCT user_id AS v FROM user_login_record WHERE device_id = :d "
        "UNION SELECT DISTINCT user_id FROM coupon_receive_record WHERE device_id = :d"
    ), {"d": device_id})
    return [("用户", r.v) for r in rows]


def _neighbors_of_ip(db: Session, ip: str) -> list[tuple[str, str]]:
    rows = db.execute(text(
        "SELECT DISTINCT user_id AS v FROM user_login_record WHERE login_ip = :ip"
    ), {"ip": ip})
    return [("用户", r.v) for r in rows]


def _neighbors_of_phone(db: Session, phone: str) -> list[tuple[str, str]]:
    rows = db.execute(text(
        "SELECT DISTINCT user_id AS v FROM receive_info WHERE receiver_phone = :p"
    ), {"p": phone})
    return [("用户", r.v) for r in rows]


def _neighbors_of_address(db: Session, address: str) -> list[tuple[str, str]]:
    rows = db.execute(text(
        "SELECT DISTINCT user_id AS v FROM receive_info "
        "WHERE CONCAT(receive_province, receive_city, receive_district, "
        "receive_street_address) = :a"
    ), {"a": address})
    return [("用户", r.v) for r in rows]


_FINDERS = {
    "用户": _neighbors_of_user,
    "设备": _neighbors_of_device,
    "IP": _neighbors_of_ip,
    "手机号": _neighbors_of_phone,
    "地址": _neighbors_of_address,
}


def find_neighbors(db: Session, entity_type: str, entity_value: str) -> list[tuple[str, str]]:
    """查一个节点的直接邻居（1 跳）"""
    finder = _FINDERS.get(entity_type)
    if finder is None:
        return []
    try:
        return finder(db, entity_value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("邻接查询失败 %s=%s: %s", entity_type, entity_value, exc)
        return []


def _node_id(entity_type: str, entity_value: str) -> str:
    return f"{entity_type}:{entity_value}"


# ============================================================
# 多跳图谱查询
# ============================================================
def query_graph(
    db: Session,
    *,
    entity_type: str,
    entity_value: str,
    hops: int | None = None,
    max_nodes: int | None = None,
    use_cache: bool = True,
) -> dict:
    """从中心实体出发做多跳扩散，返回 {nodes, edges, truncated, ...}（API-18 / FR-07）

    BFS 逐跳扩散；每跳新增节点数触达上限即截断，不再继续往外扩。
    """
    if entity_type not in VALID_ENTITY_TYPES:
        raise GraphServiceError(
            f"主体类型非法：{entity_type}（可选：{'/'.join(VALID_ENTITY_TYPES)}）",
            {"entity_type": entity_type},
        )
    if not entity_value:
        raise GraphServiceError("主体值不能为空", {"entity_type": entity_type})

    hops = min(hops or settings.GRAPH_DEFAULT_HOPS, settings.GRAPH_MAX_HOPS)
    max_nodes = max_nodes or settings.GRAPH_MAX_NODES

    if use_cache:
        key = cache.graph_key(entity_type, entity_value, hops)
        hit = cache.safe_get(key)
        if isinstance(hit, dict):
            hit = dict(hit)
            hit["cached"] = True
            return hit

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    edge_keys: set[tuple[str, str, str]] = set()

    center_key = _node_id(entity_type, entity_value)
    nodes[center_key] = {
        "id": center_key, "type": entity_type, "value": entity_value,
        "label": entity_value, "center": True, "hop": 0,
    }

    frontier = [(entity_type, entity_value)]
    truncated = False

    for hop in range(1, hops + 1):
        next_frontier: list[tuple[str, str]] = []
        for etype, evalue in frontier:
            for ntype, nvalue in find_neighbors(db, etype, evalue):
                nkey = _node_id(ntype, nvalue)
                new_node = nkey not in nodes
                if new_node:
                    if len(nodes) >= max_nodes:
                        truncated = True
                        break
                    nodes[nkey] = {
                        "id": nkey, "type": ntype, "value": nvalue,
                        "label": nvalue, "center": False, "hop": hop,
                    }
                    next_frontier.append((ntype, nvalue))

                # 边去重：无向，两个方向只留一条
                ekey = tuple(sorted([_node_id(etype, evalue), nkey])) + (
                    REL_LABEL.get((etype, ntype), "关联"),)
                if ekey not in edge_keys:
                    edge_keys.add(ekey)
                    edges.append({
                        "source": _node_id(etype, evalue),
                        "target": nkey,
                        "relation": REL_LABEL.get((etype, ntype), "关联"),
                    })
            if truncated:
                break
        frontier = next_frontier
        if truncated or not frontier:
            break

    result = {
        "center": {"type": entity_type, "value": entity_value},
        "hops": hops,
        "nodes": list(nodes.values()),
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "truncated": truncated,
        "cached": False,
    }

    if use_cache and not truncated:
        # 截断的结果不缓存 —— 否则后续正常规模的查询会命中这份不完整数据
        cache.safe_set(
            cache.graph_key(entity_type, entity_value, hops),
            {k: v for k, v in result.items() if k != "cached"},
            settings.CACHE_TTL_GRAPH,
        )

    logger.info("图谱查询: %s=%s hops=%d → %d 节点 / %d 边%s",
                entity_type, entity_value, hops, len(nodes), len(edges),
                "（已截断）" if truncated else "")
    return result


def related_accounts(
    db: Session, *, entity_type: str, entity_value: str, min_accounts: int = 3,
) -> dict:
    """聚集判定辅助：某主体下一跳关联到的**去重账号数**是否达到阈值

    对应 PRD FR-07 验收 3「存在同设备关联 ≥3 个账号的场景时，对应规则可命中」。
    """
    neighbors = find_neighbors(db, entity_type, entity_value)
    accounts = sorted({v for t, v in neighbors if t == "用户"})
    return {
        "entity_type": entity_type,
        "entity_value": entity_value,
        "account_count": len(accounts),
        "accounts": accounts,
        "reached_threshold": len(accounts) >= min_accounts,
        "threshold": min_accounts,
    }
