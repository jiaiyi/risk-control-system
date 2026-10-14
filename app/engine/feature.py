# -*- coding: utf-8 -*-
"""
特征引擎 —— 计算 43 个风控特征

维度划分（共 9 个）：
    user_    14  用户       ┐
    order_    8  订单       ├─ 复用参考实现
    addr_     4  地址       ┘  （addr 新增 1 个聚集特征）
    login_    4  登录       ┐
    coupon_   6  领券       │
    device_   2  设备       ├─ 本期新增（支撑 6 类事件与聚集识别）
    ip_       2  IP         │
    sku_      2  商品       │
    phone_    1  手机号     ┘

统一契约：全部返回 `dict[str, float]`，规则引擎通过特征名字符串消费。

缓存策略（SPEC 4.5）：
  · 用户 / 地址 / 登录 / 领券 / 设备 / IP 维度 → 走 Redis，TTL 60s
  · **订单 / 商品维度 → 强制实时计算，禁止缓存**（含本次订单的实时金额）
"""
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import cache
from app.config import settings

logger = logging.getLogger(__name__)

# 特征名前缀 → 实体类型（落 risk_feature 快照时使用）
# 用可扩展字典而非 if/elif：新增特征维度只需加一行
PREFIX_ENTITY = {
    "user_": "用户",
    "order_": "订单",
    "sku_": "订单",
    "addr_": "地址",
    "login_": "登录",
    "coupon_": "领券",
    "device_": "设备",
    "ip_": "IP",
    "phone_": "手机号",
}


def entity_type_of(feature_name: str) -> str:
    """按特征名前缀推断实体类型；未匹配的归入「用户」"""
    for prefix, etype in PREFIX_ENTITY.items():
        if feature_name.startswith(prefix):
            return etype
    return "用户"


def _since(**kw) -> datetime:
    return datetime.now() - timedelta(**kw)


# ============================================================
# 1. 用户维度（14 个）
# ============================================================
def compute_user_features(db: Session, user_id: str) -> dict[str, float]:
    f: dict[str, float] = {}

    # 1. 历史订单总数
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM order_info WHERE user_id = :uid"),
                     {"uid": user_id}).first()
    f["user_total_orders"] = float(row.cnt) if row else 0

    # 2. 近 30 天订单数
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM order_info WHERE user_id = :uid AND create_time >= :since"),
                     {"uid": user_id, "since": _since(days=30)}).first()
    f["user_orders_30d"] = float(row.cnt) if row else 0

    # 3. 近 7 天订单数
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM order_info WHERE user_id = :uid AND create_time >= :since"),
                     {"uid": user_id, "since": _since(days=7)}).first()
    f["user_orders_7d"] = float(row.cnt) if row else 0

    # 4. 历史总消费金额
    row = db.execute(text("""
        SELECT COALESCE(SUM(od.final_amount), 0) AS total
        FROM order_detail od JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
    """), {"uid": user_id}).first()
    f["user_total_amount"] = float(row.total) if row else 0

    # 5. 平均订单金额（派生）
    total_orders = f["user_total_orders"]
    f["user_avg_order_amount"] = round(f["user_total_amount"] / total_orders, 2) if total_orders > 0 else 0

    # 6. 最大单笔订单金额
    row = db.execute(text("""
        SELECT COALESCE(MAX(order_total), 0) AS max_amount FROM (
            SELECT od.order_id, SUM(od.final_amount) AS order_total
            FROM order_detail od JOIN order_info oi ON od.order_id = oi.order_id
            WHERE oi.user_id = :uid GROUP BY od.order_id
        ) t
    """), {"uid": user_id}).first()
    f["user_max_order_amount"] = float(row.max_amount) if row else 0

    # 7. 退款总次数
    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM postsale p
        JOIN order_detail od ON p.order_detail_id = od.order_detail_id
        JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid AND p.postsale_type = '退款'
    """), {"uid": user_id}).first()
    f["user_refund_count"] = float(row.cnt) if row else 0

    # 8. 售后总次数
    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM postsale p
        JOIN order_detail od ON p.order_detail_id = od.order_detail_id
        JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
    """), {"uid": user_id}).first()
    f["user_postsale_count"] = float(row.cnt) if row else 0

    # 9 / 10. 退款率、售后率（派生）
    f["user_refund_rate"] = round(f["user_refund_count"] / total_orders, 4) if total_orders > 0 else 0
    f["user_postsale_rate"] = round(f["user_postsale_count"] / total_orders, 4) if total_orders > 0 else 0

    # 11. 退款总金额
    row = db.execute(text("""
        SELECT COALESCE(SUM(p.refund_amount), 0) AS total_refund FROM postsale p
        JOIN order_detail od ON p.order_detail_id = od.order_detail_id
        JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
    """), {"uid": user_id}).first()
    f["user_refund_amount"] = float(row.total_refund) if row else 0

    # 12. 取消订单次数
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM order_info WHERE user_id = :uid AND order_status = '已取消'"),
                     {"uid": user_id}).first()
    f["user_cancel_count"] = float(row.cnt) if row else 0

    # 13. 投诉次数
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM logistics_complaints_record WHERE user_id = :uid"),
                     {"uid": user_id}).first()
    f["user_complaint_count"] = float(row.cnt) if row else 0

    # 14. 收货地址数量
    row = db.execute(text("SELECT COUNT(*) AS cnt FROM receive_info WHERE user_id = :uid"),
                     {"uid": user_id}).first()
    f["user_address_count"] = float(row.cnt) if row else 0

    return f


# ============================================================
# 2. 订单维度（8 个）—— 禁止缓存
# ============================================================
def compute_order_features(db: Session, order_id: str | None) -> dict[str, float]:
    f: dict[str, float] = {}
    if not order_id:
        return {k: 0 for k in [
            "order_total_amount", "order_item_count", "order_sku_count", "order_discount_amount",
            "order_discount_rate", "order_pay_interval_sec", "order_is_night", "order_category_count",
        ]}

    row = db.execute(text("""
        SELECT COALESCE(SUM(final_amount), 0) AS total,
               COUNT(*) AS item_count,
               COALESCE(SUM(sku_count), 0) AS sku_total,
               COALESCE(SUM(discount_amount), 0) AS discount_total,
               COALESCE(SUM(total_amount), 0) AS original_total
        FROM order_detail WHERE order_id = :oid
    """), {"oid": order_id}).first()
    if row:
        f["order_total_amount"] = float(row.total)
        f["order_item_count"] = float(row.item_count)
        f["order_sku_count"] = float(row.sku_total)
        f["order_discount_amount"] = float(row.discount_total)
        original = float(row.original_total)
        f["order_discount_rate"] = round(float(row.discount_total) / original, 4) if original > 0 else 0
    else:
        for k in ["order_total_amount", "order_item_count", "order_sku_count",
                  "order_discount_amount", "order_discount_rate"]:
            f[k] = 0

    # 下单到支付时间差（未支付取哨兵值 -1，见 SPEC 4.3 边界值约定）
    row = db.execute(text("SELECT create_time, payment_time FROM order_info WHERE order_id = :oid"),
                     {"oid": order_id}).first()
    if row and row.payment_time and row.create_time:
        f["order_pay_interval_sec"] = max(float((row.payment_time - row.create_time).total_seconds()), 0)
    else:
        f["order_pay_interval_sec"] = -1

    # 是否夜间下单（0~6 点）
    f["order_is_night"] = 1.0 if (row and row.create_time and 0 <= row.create_time.hour < 6) else 0.0

    # 订单商品类别数
    row2 = db.execute(text("""
        SELECT COUNT(DISTINCT si.sku_category) AS cat_cnt
        FROM order_detail od JOIN sku_info si ON od.sku_id = si.sku_id
        WHERE od.order_id = :oid
    """), {"oid": order_id}).first()
    f["order_category_count"] = float(row2.cat_cnt) if row2 else 0
    return f


# ============================================================
# 3. 地址维度（4 个，含新增的聚集特征）
# ============================================================
def compute_address_features(db: Session, user_id: str, receive_id: str | None = None) -> dict[str, float]:
    f: dict[str, float] = {}

    row = db.execute(text("SELECT COUNT(*) AS cnt FROM receive_info WHERE user_id = :uid"),
                     {"uid": user_id}).first()
    f["addr_total_count"] = float(row.cnt) if row else 0

    row = db.execute(text("SELECT COUNT(DISTINCT receive_province) AS cnt FROM receive_info WHERE user_id = :uid"),
                     {"uid": user_id}).first()
    f["addr_province_count"] = float(row.cnt) if row else 0

    if receive_id:
        # 该地址被使用过几次（<=1 视为新地址）
        row = db.execute(text("SELECT COUNT(*) AS cnt FROM order_info WHERE user_id = :uid AND receive_id = :rid"),
                         {"uid": user_id, "rid": receive_id}).first()
        f["addr_is_new"] = 1.0 if (row and row.cnt <= 1) else 0.0

        # 【新增】同地址关联的不同账号数 —— 聚集识别
        row = db.execute(text("""
            SELECT COUNT(DISTINCT oi.user_id) AS cnt FROM order_info oi
            WHERE oi.receive_id = :rid
        """), {"rid": receive_id}).first()
        f["addr_account_count"] = float(row.cnt) if row else 0
    else:
        f["addr_is_new"] = 0.0
        f["addr_account_count"] = 0.0

    return f


# ============================================================
# 4. 登录维度（4 个）—— 本期新增
# ============================================================
def compute_login_features(db: Session, user_id: str, device_id: str | None = None) -> dict[str, float]:
    f: dict[str, float] = {}

    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM user_login_record
        WHERE user_id = :uid AND login_time >= :since
    """), {"uid": user_id, "since": _since(hours=1)}).first()
    f["login_count_1h"] = float(row.cnt) if row else 0

    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM user_login_record
        WHERE user_id = :uid AND login_result = '失败' AND login_time >= :since
    """), {"uid": user_id, "since": _since(hours=1)}).first()
    f["login_fail_count_1h"] = float(row.cnt) if row else 0

    row = db.execute(text("""
        SELECT COUNT(DISTINCT login_city) AS cnt FROM user_login_record
        WHERE user_id = :uid AND login_time >= :since
    """), {"uid": user_id, "since": _since(days=7)}).first()
    f["login_city_count_7d"] = float(row.cnt) if row else 0

    if device_id:
        row = db.execute(text("""
            SELECT COUNT(*) AS cnt FROM user_login_record
            WHERE user_id = :uid AND device_id = :dev
        """), {"uid": user_id, "dev": device_id}).first()
        # 该设备在此账号上出现 <= 1 次（即只有本次）→ 视为首次使用
        f["login_is_new_device"] = 1.0 if (row and row.cnt <= 1) else 0.0
    else:
        f["login_is_new_device"] = 0.0
    return f


# ============================================================
# 5. 营销领券维度（6 个）—— 本期新增
# ============================================================
def compute_coupon_features(db: Session, user_id: str, device_id: str | None = None) -> dict[str, float]:
    f: dict[str, float] = {}

    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM coupon_receive_record
        WHERE user_id = :uid AND receive_time >= :since
    """), {"uid": user_id, "since": _since(hours=1)}).first()
    f["coupon_count_1h"] = float(row.cnt) if row else 0

    row = db.execute(text("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(coupon_amount), 0) AS amt,
               COUNT(DISTINCT coupon_type) AS type_cnt
        FROM coupon_receive_record WHERE user_id = :uid AND receive_time >= :since
    """), {"uid": user_id, "since": _since(hours=24)}).first()
    f["coupon_count_24h"] = float(row.cnt) if row else 0
    f["coupon_amount_24h"] = float(row.amt) if row else 0
    f["coupon_type_count_24h"] = float(row.type_cnt) if row else 0

    # 【聚集】同设备领券的不同账号数
    if device_id:
        row = db.execute(text("""
            SELECT COUNT(DISTINCT user_id) AS cnt FROM coupon_receive_record
            WHERE device_id = :dev AND receive_time >= :since
        """), {"dev": device_id, "since": _since(hours=24)}).first()
        f["coupon_device_account_count_24h"] = float(row.cnt) if row else 0
    else:
        f["coupon_device_account_count_24h"] = 0.0

    # 历史用券率（已使用 / 已领取）
    row = db.execute(text("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN use_status = '已使用' THEN 1 ELSE 0 END) AS used
        FROM coupon_receive_record WHERE user_id = :uid
    """), {"uid": user_id}).first()
    total = float(row.total) if row and row.total else 0
    used = float(row.used) if row and row.used else 0
    f["coupon_use_rate"] = round(used / total, 4) if total > 0 else 0
    return f


# ============================================================
# 6. 设备维度（2 个）—— 跨账号聚合
# ============================================================
def compute_device_features(db: Session, device_id: str | None) -> dict[str, float]:
    f = {"device_account_count_24h": 0.0, "device_order_count_1h": 0.0}
    if not device_id:
        return f

    # 近 24h 该设备活跃的不同账号数
    row = db.execute(text("""
        SELECT COUNT(DISTINCT user_id) AS cnt FROM user_login_record
        WHERE device_id = :dev AND login_time >= :since
    """), {"dev": device_id, "since": _since(hours=24)}).first()
    f["device_account_count_24h"] = float(row.cnt) if row else 0

    # 近 1h 该设备关联账号的下单数
    row = db.execute(text("""
        SELECT COUNT(*) AS cnt FROM order_info oi
        WHERE oi.create_time >= :since1h
          AND oi.user_id IN (
              SELECT DISTINCT user_id FROM user_login_record
              WHERE device_id = :dev AND login_time >= :since24h
          )
    """), {"dev": device_id, "since1h": _since(hours=1), "since24h": _since(hours=24)}).first()
    f["device_order_count_1h"] = float(row.cnt) if row else 0
    return f


# ============================================================
# 7. IP 维度（2 个）—— 跨账号聚合
# ============================================================
def compute_ip_features(db: Session, login_ip: str | None) -> dict[str, float]:
    f = {"ip_account_count_24h": 0.0, "ip_register_account_count": 0.0}
    if not login_ip:
        return f

    row = db.execute(text("""
        SELECT COUNT(DISTINCT user_id) AS cnt FROM user_login_record
        WHERE login_ip = :ip AND login_time >= :since
    """), {"ip": login_ip, "since": _since(hours=24)}).first()
    f["ip_account_count_24h"] = float(row.cnt) if row else 0

    # 说明：user_info 无注册时间/注册 IP 字段，这里用「该 IP 出现过的账号数」近似
    row = db.execute(text("SELECT COUNT(DISTINCT user_id) AS cnt FROM user_login_record WHERE login_ip = :ip"),
                     {"ip": login_ip}).first()
    f["ip_register_account_count"] = float(row.cnt) if row else 0
    return f


# ============================================================
# 8. 商品维度（2 个）—— 黄牛囤货识别，禁止缓存
# ============================================================
def compute_sku_features(db: Session, user_id: str, order_id: str | None) -> dict[str, float]:
    f = {"sku_buy_count_24h": 0.0, "sku_order_count_24h": 0.0}
    if not order_id:
        return f

    # 取本次订单涉及的 SKU，统计该用户近 24h 对它们的购买量
    row = db.execute(text("""
        SELECT COALESCE(SUM(od.sku_count), 0) AS buy_cnt,
               COUNT(DISTINCT od.order_id) AS order_cnt
        FROM order_detail od JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
          AND oi.create_time >= :since
          AND od.sku_id IN (SELECT sku_id FROM order_detail WHERE order_id = :oid)
    """), {"uid": user_id, "oid": order_id, "since": _since(hours=24)}).first()
    f["sku_buy_count_24h"] = float(row.buy_cnt) if row else 0
    f["sku_order_count_24h"] = float(row.order_cnt) if row else 0
    return f


# ============================================================
# 9. 手机号维度（1 个）—— 跨账号聚合
# ============================================================
def compute_phone_features(db: Session, receive_id: str | None) -> dict[str, float]:
    f = {"phone_refund_rate_24h": 0.0}
    if not receive_id:
        return f

    row = db.execute(text("SELECT receiver_phone FROM receive_info WHERE receive_id = :rid"),
                     {"rid": receive_id}).first()
    if not row or not row.receiver_phone:
        return f
    phone = row.receiver_phone

    row = db.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM order_info oi JOIN receive_info ri ON oi.receive_id = ri.receive_id
             WHERE ri.receiver_phone = :phone AND oi.create_time >= :since) AS order_cnt,
          (SELECT COUNT(*) FROM postsale p
             JOIN order_detail od ON p.order_detail_id = od.order_detail_id
             JOIN order_info oi ON od.order_id = oi.order_id
             JOIN receive_info ri ON oi.receive_id = ri.receive_id
             WHERE ri.receiver_phone = :phone AND p.create_time >= :since
               AND p.postsale_type = '退款') AS refund_cnt
    """), {"phone": phone, "since": _since(hours=24)}).first()
    order_cnt = float(row.order_cnt) if row and row.order_cnt else 0
    refund_cnt = float(row.refund_cnt) if row and row.refund_cnt else 0
    f["phone_refund_rate_24h"] = round(refund_cnt / order_cnt, 4) if order_cnt > 0 else 0
    return f


# ============================================================
# 汇总入口（含缓存编排）
# ============================================================
# 可缓存的维度 → (缓存键后缀, TTL)。订单/商品不在其中（强制实时）
def _cached(db: Session, entity: str, cache_id: str, builder, *args,
            write_cache: bool = True) -> dict[str, float]:
    """通用「先查缓存、未命中则计算并回填」包装

    `write_cache=False` 时**只读缓存、不回填** —— 规则仿真（T-16）要求零副作用，
    不能因为跑一次仿真就把特征写进 Redis。读缓存本身无副作用，仍可加速。
    """
    key = cache.feature_key(entity, cache_id)
    hit = cache.safe_get(key)
    if isinstance(hit, dict):
        return {k: float(v) for k, v in hit.items()}
    val = builder(db, *args)
    if write_cache:
        cache.safe_set(key, val, settings.CACHE_TTL_FEATURE)
    return val


def compute_all_features(
    db: Session,
    user_id: str,
    order_id: str | None = None,
    receive_id: str | None = None,
    device_id: str | None = None,
    login_ip: str | None = None,
    write_cache: bool = True,
) -> dict[str, float]:
    """计算全部 43 个特征

    参数来自风控请求与事件上下文：
      · user_id    必传
      · order_id   下单/支付类事件传入
      · receive_id 有收货地址时传入
      · device_id  登录/领券事件自带；下单/支付类事件取该用户最近一次登录的设备
      · login_ip   同上
      · write_cache 是否把计算结果回填 Redis（仿真场景传 False 保证零副作用）
    """
    features: dict[str, float] = {}
    wc = write_cache

    # ---- 可缓存维度（TTL 60s）----
    # ⚠️ 缓存键必须包含**所有影响结果的输入**：
    #    login / coupon 维度的结果依赖 device_id（如 login_is_new_device、
    #    coupon_device_account_count_24h），若只用 user_id 做键，
    #    换个设备查询会命中旧缓存 → 返回错误的聚集特征值。
    features.update(_cached(db, "user", user_id, compute_user_features, user_id,
                            write_cache=wc))
    features.update(_cached(db, "addr", f"{user_id}:{receive_id or '-'}",
                            compute_address_features, user_id, receive_id,
                            write_cache=wc))
    features.update(_cached(db, "login", f"{user_id}:{device_id or '-'}",
                            compute_login_features, user_id, device_id,
                            write_cache=wc))
    features.update(_cached(db, "coupon", f"{user_id}:{device_id or '-'}",
                            compute_coupon_features, user_id, device_id,
                            write_cache=wc))
    if device_id:
        features.update(_cached(db, "device", device_id, compute_device_features, device_id,
                                write_cache=wc))
    else:
        features.update(compute_device_features(db, None))
    if login_ip:
        features.update(_cached(db, "ip", login_ip, compute_ip_features, login_ip,
                                write_cache=wc))
    else:
        features.update(compute_ip_features(db, None))

    # ---- 强制实时：订单 / 商品维度（禁止缓存）----
    features.update(compute_order_features(db, order_id))
    features.update(compute_sku_features(db, user_id, order_id))

    # ---- 跨账号聚合：手机号维度 ----
    features.update(compute_phone_features(db, receive_id))

    return features


# 期望的特征总数（用于自检与测试断言）
EXPECTED_FEATURE_COUNT = 43
