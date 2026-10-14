# -*- coding: utf-8 -*-
"""
补充数据生成器 —— 为三张业务表生成可控的测试数据

  1. logistics_complaints_record  物流投诉记录（参考数据包里为空，需补）
  2. user_login_record            用户登录记录（本期新增，支撑「登录」事件）
  3. coupon_receive_record        优惠券领取记录（本期新增，支撑「营销领券」事件）

设计原则：**不仅要生成数据，还要能真实触发对应规则**。
下面按用户分组预置了若干「有故事」的场景，每个场景都标注了它应当命中的规则，
便于后续演示「发起检查 → 规则命中 → 生成案件」的完整链路。

用法（在项目根目录执行）：
    python scripts/gen_extra_data.py

脚本可重复执行（每次先清空这三张表再重建）。
"""
import random
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

sys.path.insert(0, ".")  # 允许从项目根直接运行
from app.database import SessionLocal  # noqa: E402

# ============================================================
# 基础池
# ============================================================
NOW = datetime.now().replace(microsecond=0)

CITIES = ["深圳市", "上海市", "北京市", "广州市", "杭州市", "成都市", "武汉市", "西安市", "南京市", "重庆市"]
COUPON_TYPES = ["满100减10", "满200减30", "满300减50", "新人专享券", "品类折扣券", "运费券", "会员日券"]

# 用于构造「同设备聚集」的设备标识
DEV_SHARED_LOGIN = "D-SHARED-0001"    # 被 3 个账号共用（L002 / C002）
DEV_SHARED_COUPON = "D-SHARED-0002"   # 被 3 个账号共用（C002）
IP_SHARED = "10.99.99.99"             # 被 5 个账号共用（L003 / A004）

RNG = random.Random(20261014)  # 固定种子 → 结果可复现


def _dev(k: str) -> str:
    return f"D-{k}"


def fetch_users(db) -> list[str]:
    rows = db.execute(text("SELECT user_id FROM user_info ORDER BY user_id")).fetchall()
    return [r[0] for r in rows]


def fetch_logistics(db) -> list[str]:
    rows = db.execute(text("SELECT logistics_id FROM logistics LIMIT 200")).fetchall()
    return [r[0] for r in rows]


# ============================================================
# 1. 物流投诉记录
# ============================================================
def gen_complaints(db, users: list[str], logistics_ids: list[str]) -> int:
    """为部分用户生成投诉记录。
    场景：1003 被投诉/发起投诉 6 次 → 命中 R023「高频投诉用户」(>=5)
    """
    db.execute(text("DELETE FROM logistics_complaints_record"))
    plan = {
        "1003": 6,   # R023 高频投诉用户（>=5）
        "1005": 3,   # R012 大量投诉记录（>=3）
        "1007": 2,
        "1002": 1,   # R022 投诉用户下单（>=1）
        "1009": 1,
    }
    complaints = ["包裹破损", "配送超时", "快递员态度差", "物流信息不更新", "未经允许放驿站", "商品与描述不符"]
    n = 0
    for uid, cnt in plan.items():
        for i in range(cnt):
            days_ago = RNG.randint(1, 20)
            db.execute(text(
                "INSERT INTO logistics_complaints_record "
                "(logistics_id, logistics_complaint, complaint_time, user_id) "
                "VALUES (:lid, :c, :t, :u)"
            ), {
                "lid": RNG.choice(logistics_ids),
                "c": RNG.choice(complaints),
                "t": NOW - timedelta(days=days_ago, hours=RNG.randint(0, 23)),
                "u": uid,
            })
            n += 1
    return n


# ============================================================
# 2. 登录记录
# ============================================================
def gen_logins(db, users: list[str]) -> int:
    """生成登录记录，含 5 个可触发规则的场景。

    对照规则：
      L001 短时高频登录失败  login_fail_count_1h >= 5
      L002 同设备多账号登录  device_account_count_24h >= 3
      L003 同IP多账号登录    ip_account_count_24h >= 5
      L004 新设备异地登录    login_is_new_device=1 且 login_city_count_7d >= 3
      L005 短时高频登录      login_count_1h >= 10
    """
    db.execute(text("DELETE FROM user_login_record"))
    rows: list[dict] = []

    def login(uid, t, dev, ip, city, result="成功"):
        rows.append({
            "id": f"LG{len(rows) + 1:06d}",
            "uid": uid, "t": t, "dev": dev, "ip": ip, "city": city, "res": result,
        })

    # ---- 场景 A：普通用户的日常登录（近 7 天，频率正常）----
    for i, uid in enumerate(users):
        for k in range(RNG.randint(4, 7)):
            login(
                uid,
                NOW - timedelta(days=RNG.randint(1, 7), hours=RNG.randint(0, 23)),
                _dev(f"{i * 10 + k:04d}"),
                f"10.{RNG.randint(1, 50)}.{RNG.randint(1, 255)}.{RNG.randint(1, 254)}",
                CITIES[i % len(CITIES)],
            )

    # ---- 场景 B：1002 撞库试登 —— 近 1 小时失败 6 次（L001）----
    for k in range(6):
        login("1002", NOW - timedelta(minutes=50 - k * 7), _dev("9002"), "203.0.113.7", "深圳市", "失败")
    # 1002 另有 2 次成功（保证账号被"撞开"过）
    for k in range(2):
        login("1002", NOW - timedelta(minutes=5 - k * 2), _dev("9002"), "203.0.113.7", "深圳市")

    # ---- 场景 C：1002 新设备异地登录 —— 近 7 天 4 个城市（L004）----
    for k, city in enumerate(["北京市", "上海市", "杭州市", "广州市"]):
        login("1002", NOW - timedelta(days=1, hours=k * 3), _dev("9002"), "203.0.113.7", city)

    # ---- 场景 D：1003 / 1004 / 1005 共用同一设备 —— 3 个账号（L002）----
    for j, uid in enumerate(["1003", "1004", "1005"]):
        login(uid, NOW - timedelta(hours=2, minutes=j * 10), DEV_SHARED_LOGIN, f"172.16.0.{10 + j}", "上海市")

    # ---- 场景 E：1006 / 1007 / 1008 / 1009 / 1010 共用同一 IP —— 5 个账号（L003 / A004）----
    for j, uid in enumerate(["1006", "1007", "1008", "1009", "1010"]):
        login(uid, NOW - timedelta(hours=3, minutes=j * 6), _dev(f"77{j:02d}"), IP_SHARED, "北京市")

    # ---- 场景 F：1010 短时高频登录 —— 近 1 小时 12 次（L005）----
    for k in range(12):
        login("1010", NOW - timedelta(minutes=58 - k * 4), _dev("7710"), IP_SHARED, "北京市")

    for r in rows:
        db.execute(text(
            "INSERT INTO user_login_record (login_id, user_id, login_time, device_id, login_ip, login_city, login_result) "
            "VALUES (:id, :uid, :t, :dev, :ip, :city, :res)"
        ), r)
    return len(rows)


# ============================================================
# 3. 优惠券领取记录
# ============================================================
def gen_coupons(db, users: list[str]) -> int:
    """生成领券记录，含 4 个可触发规则的场景。

    对照规则：
      C001 短时批量领券      coupon_count_1h >= 10
      C002 同设备多账号领券  coupon_device_account_count_24h >= 3
      C003 高频领券          coupon_count_24h >= 20
      C004 全券种扫荡        coupon_type_count_24h >= 5
      C005 大额累计领券      coupon_amount_24h >= 500
    """
    db.execute(text("DELETE FROM coupon_receive_record"))
    rows: list[dict] = []

    def receive(uid, t, dev, ctype, amount, used=False):
        rows.append({
            "id": f"CP{len(rows) + 1:06d}",
            "uid": uid, "t": t, "dev": dev, "ctype": ctype, "amt": amount,
            "status": "已使用" if used else "未使用",
            "use_t": t + timedelta(hours=2) if used else None,
        })

    # ---- 场景 A：普通用户日常领券 ----
    for i, uid in enumerate(users):
        for k in range(RNG.randint(2, 5)):
            receive(
                uid,
                NOW - timedelta(days=RNG.randint(1, 6), hours=RNG.randint(0, 23)),
                _dev(f"{i * 10 + k:04d}"),
                RNG.choice(COUPON_TYPES),
                RNG.choice([10, 20, 30, 50]),
                used=RNG.random() > 0.4,
            )

    # ---- 场景 B：1003 短时批量领券 —— 近 1 小时 12 次（C001）----
    for k in range(12):
        receive("1003", NOW - timedelta(minutes=55 - k * 4), _dev("8003"), "满200减30", 30)

    # ---- 场景 C：1004 高频领券 22 次 + 覆盖 6 个券种（C003 + C004）----
    for k in range(22):
        receive("1004", NOW - timedelta(hours=RNG.randint(1, 23), minutes=RNG.randint(0, 59)),
                _dev("8004"), COUPON_TYPES[k % 6], RNG.choice([10, 20, 30]))

    # ---- 场景 D：1005 大额累计领券 —— 近 24 小时券面合计 600 元（C005）----
    for k in range(6):
        receive("1005", NOW - timedelta(hours=1 + k * 3), _dev("8005"), "满300减50", 100)

    # ---- 场景 E：1006 / 1007 / 1008 共用同一设备领券 —— 3 个账号（C002）----
    for j, uid in enumerate(["1006", "1007", "1008"]):
        for k in range(2):
            receive(uid, NOW - timedelta(hours=4, minutes=j * 10 + k), DEV_SHARED_COUPON, "新人专享券", 50)

    for r in rows:
        db.execute(text(
            "INSERT INTO coupon_receive_record "
            "(receive_id, user_id, coupon_id, coupon_type, coupon_amount, receive_time, device_id, use_status, use_time) "
            "VALUES (:id, :uid, :cid, :ctype, :amt, :t, :dev, :status, :use_t)"
        ), {
            "id": r["id"], "uid": r["uid"], "cid": f"CPN-{RNG.randint(1000, 9999)}",
            "ctype": r["ctype"], "amt": r["amt"], "t": r["t"], "dev": r["dev"],
            "status": r["status"], "use_t": r["use_t"],
        })
    return len(rows)


# ============================================================
# 执行
# ============================================================
def main() -> None:
    db = SessionLocal()
    try:
        users = fetch_users(db)
        logistics_ids = fetch_logistics(db)
        if not users:
            print("✗ user_info 为空，请先导入 sql/init_business_data.sql")
            return
        if not logistics_ids:
            print("✗ logistics 为空，无法生成投诉记录")
            return

        print(f"用户数: {len(users)}  物流单数: {len(logistics_ids)}")
        print(f"时间基准: {NOW}")

        n1 = gen_complaints(db, users, logistics_ids)
        print(f"  ✓ 物流投诉记录  {n1} 条")

        n2 = gen_logins(db, users)
        print(f"  ✓ 登录记录      {n2} 条")

        n3 = gen_coupons(db, users)
        print(f"  ✓ 领券记录      {n3} 条")

        db.commit()
        print("\n全部提交完成。")
        print("\n可触发的规则场景：")
        for line in [
            "  登录 1002  → L001 短时高频登录失败（1h 失败 6 次）",
            "  登录 1002  → L004 新设备异地登录（新设备 + 4 个城市）",
            "  登录 1003/1004/1005 → L002 同设备多账号登录（共用 D-SHARED-0001）",
            "  登录 1006~1010      → L003 同IP多账号登录（共用 10.99.99.99）",
            "  登录 1010  → L005 短时高频登录（1h 12 次）",
            "  领券 1003  → C001 短时批量领券（1h 12 次）",
            "  领券 1004  → C003 高频领券（24h 22 次）+ C004 全券种扫荡（6 种）",
            "  领券 1005  → C005 大额累计领券（24h 券面 600 元）",
            "  领券 1006/1007/1008 → C002 同设备多账号领券（共用 D-SHARED-0002）",
            "  投诉 1003  → R023 高频投诉用户（6 次）",
            "  投诉 1005  → R012 大量投诉记录（3 次）",
        ]:
            print(line)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"✗ 生成失败，已回滚：{type(exc).__name__}: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
