# -*- coding: utf-8 -*-
"""验收场景测试 —— 补齐此前未被覆盖的 AC

这里只放**在别处没有覆盖**的验收项，其余 AC 的证据分散在
`test_engine_*.py` / `test_*_service.py` / `test_api_*.py` 里（对照关系见
`specs/验收报告.md`）。

本文件构造的是**业务只读表**的数据（order_info / order_detail / postsale /
receive_info / user_login_record），这类数据 `conftest` 不会自动清理，
所以：
  · 全部用 `ACCX-` 前缀，与演示数据隔离；
  · 文件内自带 autouse 清理夹具，跑完即删，不留残留。
"""
import pytest
from sqlalchemy import text

from app.engine.decision import run_risk_check

PREFIX = "ACCX-"


# ============================================================
# 清理：本文件造的业务数据必须自己删干净
# ============================================================
@pytest.fixture(autouse=True)
def _clean_acc_business_rows(db):
    """清理本文件造的业务数据。

    ⚠ 顺序必须**自底向上**（先删引用方、再删被引用方），且要覆盖**全部**引用
    路径 —— 这些业务表之间有 8 条外键，漏一条就会 1451 把整批删除回滚掉，
    表现成「下一个用例插入主键冲突」（残留没删掉）。完整依赖图：

        postsale              → order_detail, receive_info
        order_detail          → order_info, sku_info
        order_info            → user_info, receive_info, order_status
        coupon_receive_record → user_info, receive_info
        logistics_complaints_record / user_login_record → user_info
        receive_info          → user_info, region
    """
    yield
    # ⚠ 两个坑：
    #   1. 顺序必须自底向上（先删引用方、再删被引用方），否则 1451 会把整批删除回滚掉；
    #   2. 不能只按自己造的主键前缀过滤 —— `gen_extra_data` 会在 session 开始时
    #      给 user_info 里的**每个**用户补造登录/领券记录，那些行的 login_id 是随机值，
    #      只能按 `user_id` 过滤才删得干净。
    for sql in (
        "DELETE FROM postsale WHERE receive_id LIKE 'ACCX-%' "
        "                      OR order_detail_id LIKE 'ACCX-%'",
        "DELETE FROM order_detail WHERE order_id LIKE 'ACCX-%'",
        "DELETE FROM order_info WHERE user_id LIKE 'ACCX-%'",
        "DELETE FROM coupon_receive_record WHERE user_id LIKE 'ACCX-%'",
        "DELETE FROM logistics_complaints_record WHERE user_id LIKE 'ACCX-%'",
        "DELETE FROM user_login_record WHERE user_id LIKE 'ACCX-%'",
        "DELETE FROM receive_info WHERE user_id LIKE 'ACCX-%'",
        "DELETE FROM user_info WHERE user_id LIKE 'ACCX-%'",
    ):
        db.execute(text(sql))
    db.commit()


# ============================================================
# 造数据小工具
# ============================================================
def _ensure_user(db, user_id: str) -> None:
    db.execute(text("INSERT IGNORE INTO user_info (user_id) VALUES (:u)"), {"u": user_id})
    db.commit()


def _insert_receive(db, *, receive_id: str, user_id: str,
                    phone: str = "13900001234", addr: str = "验收测试路 99 号") -> None:
    # ⚠ receive_info 的 (province, city, district) 上有复合外键指向 region，
    #   随手编一个「验收省/验收市/验收区」会被 FK 挡下（1452）。
    region = db.execute(text(
        "SELECT province, city, district FROM region LIMIT 1")).first()
    db.execute(text(
        "INSERT INTO receive_info (receive_id, user_id, receiver_name, receiver_phone, "
        "receive_province, receive_city, receive_district, receive_street_address) "
        "VALUES (:rid, :uid, '验收测试', :phone, :prov, :city, :dist, :addr)"),
        {"rid": receive_id, "uid": user_id, "phone": phone,
         "prov": region.province, "city": region.city, "dist": region.district,
         "addr": addr})
    db.commit()


def _insert_order(db, *, order_id: str, user_id: str, amount: float,
                  receive_id: str, status: str = "待支付",
                  sku_id: str | None = None, sku_count: int = 1) -> None:
    # sku_id 受外键约束指向 sku_info，默认取一个真实存在的 SKU
    if sku_id is None:
        sku_id = db.execute(text("SELECT sku_id FROM sku_info LIMIT 1")).scalar()
    db.execute(text(
        "INSERT INTO order_info (order_id, create_time, user_id, receive_id, order_status) "
        "VALUES (:oid, NOW(), :uid, :rid, :st)"),
        {"oid": order_id, "uid": user_id, "rid": receive_id, "st": status})
    db.execute(text(
        "INSERT INTO order_detail (order_detail_id, order_id, sku_id, sku_name, sku_count, "
        "total_amount, final_amount) VALUES (:did, :oid, :sku, '验收测试商品', :cnt, :amt, :amt)"),
        {"did": f"{order_id}-D1", "oid": order_id, "sku": sku_id,
         "cnt": sku_count, "amt": amount})
    db.commit()


def _insert_login(db, *, login_id: str, user_id: str, device_id: str, ip: str) -> None:
    db.execute(text(
        "INSERT INTO user_login_record (login_id, user_id, login_time, device_id, login_ip, login_result) "
        "VALUES (:lid, :uid, NOW(), :dev, :ip, '成功')"),
        {"lid": login_id, "uid": user_id, "dev": device_id, "ip": ip})
    db.commit()


def _hits(out) -> list[str]:
    return [h.rule_id for h in out.hits]


# ============================================================
# AC-01 高风险订单被拒绝
# ============================================================
def test_ac01_extreme_amount_order_is_rejected(db):
    """AC-01：存在金额 ≥10000 的订单 → 决策=拒绝，评分 ≥90，命中高额类规则"""
    user = PREFIX + "U01"
    rec = PREFIX + "REC-01"
    order_id = PREFIX + "ORD-01"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    _insert_order(db, order_id=order_id, user_id=user, amount=12000, receive_id=rec)

    out = run_risk_check(db, user_id=user, event_type="下单", source_id=order_id,
                         order_id=order_id, receive_id=rec)

    assert "R002" in _hits(out), f"应命中 R002（单笔极端高额订单），实际 {_hits(out)}"
    assert out.final_score >= 90
    assert out.decision == "拒绝"
    assert out.breakdown.veto is True, "R002 是「极高」等级，应触发一票否决"


def test_ac01_threshold_boundary(db):
    """AC-01 边界：9000 元不满足 R002 的 10000 门槛（不该误伤）"""
    user = PREFIX + "U01B"
    rec = PREFIX + "REC-01B"
    order_id = PREFIX + "ORD-01B"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    _insert_order(db, order_id=order_id, user_id=user, amount=9000, receive_id=rec)

    out = run_risk_check(db, user_id=user, event_type="下单", source_id=order_id,
                         order_id=order_id, receive_id=rec)
    assert "R002" not in _hits(out)


# ============================================================
# AC-38 商品维度囤货识别（G-14）
# ============================================================
def test_ac38_single_sku_stockpiling_detected(db):
    """AC-38：单账号 24h 内购买同一 SKU 共 12 件 → 命中 B001「单 SKU 超量购买」"""
    user = PREFIX + "U38"
    rec = PREFIX + "REC-38"
    order_id = PREFIX + "ORD-38"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    _insert_order(db, order_id=order_id, user_id=user, amount=100 * 12,
                  receive_id=rec, sku_count=12)

    out = run_risk_check(db, user_id=user, event_type="下单", source_id=order_id,
                         order_id=order_id, receive_id=rec)

    assert "B001" in _hits(out), f"应命中 B001，实际 {_hits(out)}"
    assert out.features["sku_buy_count_24h"] == 12


def test_ac38_split_order_evasion_detected(db):
    """AC-38 的进阶形态（B002 拆单规避限购）：5 单 × 2 件 = 10 件 → 命中 B002"""
    user = PREFIX + "U38B"
    rec = PREFIX + "REC-38B"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    for i in range(5):
        _insert_order(db, order_id=f"{PREFIX}ORD-38B-{i}", user_id=user, amount=200,
                      receive_id=rec, sku_count=2)

    out = run_risk_check(db, user_id=user, event_type="下单",
                         source_id=f"{PREFIX}ORD-38B-4",
                         order_id=f"{PREFIX}ORD-38B-4", receive_id=rec)

    assert "B002" in _hits(out), f"应命中 B002（拆单规避限购），实际 {_hits(out)}"
    assert out.features["sku_order_count_24h"] == 5
    assert out.features["sku_buy_count_24h"] == 10


# ============================================================
# AC-39 同地址多账号聚集（G-15）
# ============================================================
def test_ac39_same_address_multi_account_detected(db):
    """AC-39：同一收货地址关联 3 个账号且均有退款记录 → 命中 A001 / A002

    ⚠ PRD 把 A001 与 A002 写在同一行，但两条规则的适用事件类型不同
      （`A001 → 下单`、`A002 → 售后申请`），因此需要**两次检查**分别验证。
      另外 A001 标题是「同地址多账号**下单**」——它验证的是地址聚集本身，
      与「是否有退款」无关；退款维度由 A002 承担。
    """
    rec = PREFIX + "REC-39"
    users = [PREFIX + "A1", PREFIX + "A2", PREFIX + "A3"]

    _ensure_user(db, users[0])
    _insert_receive(db, receive_id=rec, user_id=users[0])

    for u in users:
        _ensure_user(db, u)
        _insert_order(db, order_id=f"{PREFIX}ORD-39-{u}", user_id=u, amount=300,
                      receive_id=rec)

    # 给首个账号造一条「退款」售后 —— A002 要求 user_refund_rate ≥ 0.5
    db.execute(text(
        "INSERT INTO postsale (postsale_id, create_time, order_detail_id, refund_amount, "
        "postsale_type, postsale_reason, postsale_status, receive_id) "
        "VALUES (:pid, NOW(), :did, 300, '退款', '验收测试：恶意退款', '审核中', :rid)"),
        {"pid": PREFIX + "PTS-39", "did": f"{PREFIX}ORD-39-{users[0]}-D1", "rid": rec})
    db.commit()

    # ---- 下单事件 → A001（同地址多账号下单）----
    out_order = run_risk_check(db, user_id=users[0], event_type="下单",
                               source_id=f"{PREFIX}ORD-39-{users[0]}",
                               order_id=f"{PREFIX}ORD-39-{users[0]}",
                               receive_id=rec)
    assert out_order.features["addr_account_count"] == 3
    assert "A001" in _hits(out_order), f"应命中 A001，实际 {_hits(out_order)}"

    # ---- 售后事件 → A002（同地址多账号退款）----
    out_after = run_risk_check(db, user_id=users[0], event_type="售后申请",
                               source_id=PREFIX + "AFTER-39", receive_id=rec)
    hits = _hits(out_after)
    assert "A002" in hits, f"应命中 A002（同地址多账号退款），实际 {hits}"
    assert out_after.decision == "拒绝", "A002 是「极高」等级，应触发一票否决"
    assert out_after.case_id, "高风险应自动建案，供人工复核时查看关联账号"


def test_ac39_address_clustering_is_per_address(db):
    """边界：地址聚集必须按**具体地址**统计，不能跨地址串味"""
    rec_a = PREFIX + "REC-39A"
    rec_b = PREFIX + "REC-39B"

    _ensure_user(db, PREFIX + "B1")
    _ensure_user(db, PREFIX + "B2")
    # receive_info 上有 (user_id + 完整地址) 的唯一约束，同用户的第二个地址
    # 必须换一个街道号，否则插不进去
    _insert_receive(db, receive_id=rec_a, user_id=PREFIX + "B1", addr="验收测试路 99 号")
    _insert_receive(db, receive_id=rec_b, user_id=PREFIX + "B1", addr="验收测试路 100 号")

    _insert_order(db, order_id=PREFIX + "ORD-A1", user_id=PREFIX + "B1",
                  amount=100, receive_id=rec_a)
    _insert_order(db, order_id=PREFIX + "ORD-B1", user_id=PREFIX + "B2",
                  amount=100, receive_id=rec_b)

    out = run_risk_check(db, user_id=PREFIX + "B1", event_type="下单",
                         source_id=PREFIX + "ORD-A1",
                         order_id=PREFIX + "ORD-A1", receive_id=rec_a)
    assert out.features["addr_account_count"] == 1, "只应统计该地址下的账号"
    assert "A001" not in _hits(out)


# ============================================================
# AC-40 同设备集中下单（G-19）
# ============================================================
def test_ac40_same_device_concentrated_orders(db):
    """AC-40：同一设备 1 小时内产生 6 笔下单 → 命中 A003「同设备短时集中下单」"""
    device = PREFIX + "DEV-40"
    user = PREFIX + "U40"
    rec = PREFIX + "REC-40"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    _insert_login(db, login_id=PREFIX + "LG-40", user_id=user,
                  device_id=device, ip="203.0.113.240")

    for i in range(6):
        _insert_order(db, order_id=f"{PREFIX}ORD-40-{i}", user_id=user, amount=100,
                      receive_id=rec)

    out = run_risk_check(db, user_id=user, event_type="下单",
                         source_id=f"{PREFIX}ORD-40-5",
                         order_id=f"{PREFIX}ORD-40-5",
                         device_id=device, receive_id=rec)

    assert out.features["device_order_count_1h"] == 6
    assert "A003" in _hits(out), f"应命中 A003，实际 {_hits(out)}"


# ============================================================
# AC-29 关联图谱查询
# ============================================================
def test_ac29_graph_shows_accounts_sharing_device(db):
    """AC-29：三个账号共用同一设备 → 图谱应查询出该设备下的其他账号"""
    from app.service import graph_service

    device = PREFIX + "DEV-29"
    accounts = [PREFIX + "G1", PREFIX + "G2", PREFIX + "G3"]

    for idx, u in enumerate(accounts, start=1):
        _ensure_user(db, u)
        _insert_login(db, login_id=f"{PREFIX}LG-G{idx}", user_id=u,
                      device_id=device, ip="203.0.113.29")

    result = graph_service.query_graph(db, entity_type="设备", entity_value=device,
                                       hops=2)

    blob = str(result["nodes"])
    for u in accounts:
        assert u in blob, f"图谱节点里没有账号 {u}（nodes={blob[:200]}）"

    assert result["center"]
    assert "truncated" in result


# ============================================================
# AC-36 资损防护统计（数值对账）
# ============================================================
def test_ac36_blocked_amount_matches_order_total(db):
    """AC-36：资损卡片的金额 = 被拒绝决策关联业务单据金额合计

    本用例开始时风控运行表是空的（`_clean_state` 保证），所以可以做**精确对账**：
    只造一张 15000 元的订单并让它被拒绝，卡片金额就应当正好是 15000。
    """
    from app.service import dashboard_service

    user = PREFIX + "U36"
    rec = PREFIX + "REC-36"
    order_id = PREFIX + "ORD-36"

    _ensure_user(db, user)
    _insert_receive(db, receive_id=rec, user_id=user)
    _insert_order(db, order_id=order_id, user_id=user, amount=15000, receive_id=rec)

    out = run_risk_check(db, user_id=user, event_type="下单", source_id=order_id,
                         order_id=order_id, receive_id=rec)
    assert out.decision == "拒绝"

    result = dashboard_service.loss_prevention(db, days=7)

    assert result["blocked_amount"] == pytest.approx(15000.0), \
        f"资损金额应与被拒订单金额一致，实际 {result['blocked_amount']}"
    assert result["blocked_cases"] >= 1
    assert result["blocked_users"] >= 1


# ============================================================
# 课程原文 2.3.9 示例场景（端到端）
# ============================================================
def test_course_scenario_wool_party_coupon(db):
    """课程 2.3.9 场景一：羊毛党批量领券（同设备/同 IP 聚集领券）

    对应规则 C002（同设备多账号领券）—— 用既有场景数据复现。
    """
    out = run_risk_check(db, user_id="1006", event_type="营销领券",
                         source_id="ACCX-COURSE-WOOL",
                         device_id="D-SHARED-0002", login_ip="172.16.0.20")
    assert "C002" in _hits(out), f"应命中 C002，实际 {_hits(out)}"


def test_course_scenario_refund_fraud(db):
    """课程 2.3.9 场景二：高价值商品恶意退款欺诈（收货地址多账号关联退款记录）

    对应规则 A002 —— 与 AC-39 同一判定维度，这里用**独立构造**再验一次，
    因为这条正是课程原文点名要求的场景。
    """
    rec = PREFIX + "REC-COURSE"
    users = [PREFIX + "C1", PREFIX + "C2"]

    _ensure_user(db, users[0])
    _insert_receive(db, receive_id=rec, user_id=users[0])

    for u in users:
        _ensure_user(db, u)
        _insert_order(db, order_id=f"{PREFIX}ORD-C-{u}", user_id=u, amount=4000,
                      receive_id=rec)

    db.execute(text(
        "INSERT INTO postsale (postsale_id, create_time, order_detail_id, refund_amount, "
        "postsale_type, postsale_reason, postsale_status, receive_id) "
        "VALUES (:pid, NOW(), :did, 4000, '退款', '未收到货', '审核中', :rid)"),
        {"pid": PREFIX + "PTS-C", "did": f"{PREFIX}ORD-C-{users[0]}-D1", "rid": rec})
    db.commit()

    out = run_risk_check(db, user_id=users[0], event_type="售后申请",
                         source_id=PREFIX + "AFTER-C", receive_id=rec)

    hits = _hits(out)
    assert "A002" in hits, f"应命中 A002（同地址多账号退款），实际 {hits}"
    assert out.decision == "拒绝"
