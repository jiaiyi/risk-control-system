# -*- coding: utf-8 -*-
"""预填演示数据

**为什么需要它**

`init_db.py` 只灌 41 条规则 + 19 张业务只读表的数据，`risk_*` 是空的 ——
所以刚初始化完的仪表盘全是 0，趋势图是一条空轴。

本脚本用**真实链路**造数据：`process_event` 完整走一遍
特征计算 → 规则匹配 → 评分决策 → 落库，然后把时间戳整体回填到最近 N 天，
让「近 7 天趋势」真的有 N 个点，而不是今天一根竖线。

**为什么不直接写 INSERT**

手写 INSERT 的话，`rule_results` JSON、43 条特征快照、案件编号、画像衰减
这几处很容易和引擎口径对不上 —— 页面看着有数据，点进案件详情却发现
「命中规则」和「评分」互相矛盾。走真实链路慢一点，但每条数据都自洽。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/seed_demo.py
    ./.venv/Scripts/python.exe scripts/seed_demo.py --days 14

脚本可重复执行（开头会清空风控运行表，41 条规则不动）。
"""
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ 供 import _smoke

import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app import cache  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.service import case_service, dashboard_service, list_service  # noqa: E402
from app.service.event import process_event  # noqa: E402

# 固定种子 → 每次生成的演示数据完全一致，便于对着截图讲
RNG = random.Random(20261014)
REVIEWER = "张晨曦"

# 每天**必跑**的两条：保证「拒绝」与「人工审核」两档每天都有，
# 拦截率、待审案件不会因为随机略过而变成 0。
MUST_RUN = [
    # 固定用 1004 承载「拒绝」档，且**全程只让 1004 跑这一种场景**：
    # 最终评分会被用户画像的历史分加权混合，同一用户若既跑良性场景又跑高危场景，
    # 良性分会把画像拉低，高危场景最终落成「人工审核」而不是「拒绝」
    # （实测 93 分被画像稀释到 37 分）。承载高危档的用户必须独占。
    ("1004", "登录", "D-SHARED-0001", "172.16.0.10"),
    ("1003", "登录", "D-CLEAN-1003",  "192.168.9.3"),
]

# 其余流量每天随机略过约 30% —— 每天事件数一模一样的话，
# 「近 7 天趋势」会是一条笔直的横线，一眼看出是造的
MAYBE_RUN = [
    # 正常流量 → 通过
    ("1001", "登录",     "D-CLEAN-1001", "192.168.9.1"),
    ("1007", "登录",     "D-CLEAN-1007", "192.168.9.7"),
    ("1001", "营销领券", "D-SAFE-1001",  "192.168.7.1"),
    ("1007", "营销领券", "D-SAFE-1007",  "192.168.7.7"),
    ("1002", "营销领券", "D-SAFE-1002",  "192.168.7.2"),
    # 轻微可疑 → 标记
    ("1008", "登录",     "D-CLEAN-1008", "192.168.9.8"),
    ("1009", "登录",     "D-CLEAN-1009", "192.168.9.9"),
    ("1006", "登录",     "D-CLEAN-1006", "192.168.9.6"),
    ("1010", "营销领券", "D-SAFE-1010",  "192.168.7.10"),
    # 需人工处理
    ("1005", "营销领券", "D-SAFE-1005",  "192.168.7.5"),
]

SKIP_PROB = 0.3

# 上面每个 (用户, 事件类型, 设备, IP) 的档位都是**实测出来的**：
# 先用干净设备/IP 探测每个用户的实际评分，再按档位挑人，不是拍脑袋写的。
# 刻意不用 init_db 预置的那批团伙场景（D-SHARED-0001 / 10.99.99.99）当主力 ——
# 它们每天都命中 L002/L003/A004，跑 7 天等于把同一批高危事件复制 7 份，
# 结果是「人工审核 62%、通过率 12%」，一眼假。

# 下单类事件用来撑起「预估找回资损」。口径是「被拒绝案件的关联订单实付金额合计」，
# 所以 source_id 必须是 order_info 里**真实存在**的订单号 —— 随便编一个，资损永远是 ¥0。
# 订单号由 init_db 随机生成、每次重建都会变，所以只能运行时查，写死必然失效。
_ORDER_SQL = (
    "SELECT oi.order_id, oi.user_id FROM order_info oi "
    "JOIN order_detail od ON od.order_id = oi.order_id "
    "WHERE oi.user_id IN ('1003', '1005') "
    "  AND oi.order_status IN ('待支付', '待发货') "
    "ORDER BY od.final_amount DESC LIMIT :lim"
)
ORDER_DEVICE, ORDER_IP = "D-7700", "10.99.99.99"

# 时间列都在各自的表里，回填时要一起挪，否则「案件创建时间晚于审核时间」
# 这类矛盾会在详情页露馅
_TIME_COLUMNS = {
    "risk_event": ("event_id", ["create_time"]),
    "risk_feature": ("event_id", ["compute_time"]),
    "risk_assessment": ("assessment_id", ["create_time"]),
    "risk_case": ("case_id", ["create_time", "update_time"]),
    "risk_challenge": ("challenge_id", ["create_time", "expire_time"]),
}


def _shift_time(db, table: str, key_col: str, key, delta_sec: int) -> None:
    """把一行（或多行）的时间列整体平移 delta_sec 秒。

    用相对平移而不是直接改绝对值：challenge 的 expire_time - create_time
    是「验证有效期」，案件里也有类似跨度，整体平移能保证这些间隔不变。
    """
    if key is None or not delta_sec:
        return
    cols = _TIME_COLUMNS[table][1]
    sets = ", ".join(f"{c} = DATE_ADD({c}, INTERVAL :d SECOND)" for c in cols)
    db.execute(text(f"UPDATE {table} SET {sets} WHERE {key_col} = :k"),
               {"d": delta_sec, "k": key})


def main() -> int:
    days = 7
    if "--days" in sys.argv:
        days = max(1, min(int(sys.argv[sys.argv.index("--days") + 1]), 30))

    now = datetime.now()
    print("=" * 74)
    print(f"预填演示数据  |  最近 {days} 天  |  每天 {len(MUST_RUN)} 必跑 + "
          f"约 {len(MAYBE_RUN) - int(len(MAYBE_RUN) * SKIP_PROB)} 随机 + 1 笔下单")
    print("=" * 74)

    # ---- 1. 清空风控运行表（可重复执行）；41 条规则不动 ----
    print(f"\n[1/5] 清空风控运行表（{smoke.reset_risk_tables()} 张，规则表保留）")

    # ---- 2. 保证业务数据新鲜，否则场景规则命中不了 ----
    print("[2/5] 检查业务数据新鲜度")
    if not smoke.ensure_scenario_data():
        print("  ✗ 业务数据不可用，请先跑 scripts/init_db.py")
        return 2
    print("  ✓ 登录 / 领券 / 投诉记录均在有效时间窗内")

    # ---- 3. 逐天跑真实风控检查 ----
    db = SessionLocal()
    try:
        # 每个订单只用一次，避免同一个订单号被算多次资损
        order_pool = db.execute(text(_ORDER_SQL), {"lim": days}).fetchall()
    finally:
        db.close()
    if not order_pool:
        print("  ! 没查到可用于「资损」的订单（待支付/待发货），资损将显示为 ¥0")

    print(f"[3/5] 通过真实链路发起约 {days * (len(MUST_RUN) + len(MAYBE_RUN))} 次风控检查")
    db = SessionLocal()
    decisions: dict[str, int] = {}
    created: list[tuple[str, str]] = []      # (case_id, event_type)
    per_day: dict[str, int] = {}
    total = 0

    try:
        for offset in range(days - 1, -1, -1):
            day = (now - timedelta(days=offset)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            # 今天不能生成「未来时间」的事件
            lo = day + timedelta(hours=8)
            hi = day + timedelta(hours=22)
            if offset == 0:
                hi = min(hi, now - timedelta(minutes=2))
            if hi <= lo:
                lo, hi = day, max(now - timedelta(minutes=2), day + timedelta(minutes=1))

            plan = list(MUST_RUN)
            plan += [e for e in MAYBE_RUN if RNG.random() >= SKIP_PROB]
            # 每天一笔下单：source_id 用真实订单号，资损才算得出来
            oi = days - 1 - offset
            if oi < len(order_pool):
                row = order_pool[oi]
                plan.append((row.user_id, "下单", ORDER_DEVICE, ORDER_IP, row.order_id))

            for i, entry in enumerate(plan):
                uid, etype, dev, ip = entry[:4]
                order_id = entry[4] if len(entry) > 4 else None
                when = lo + (hi - lo) * RNG.random()
                out = process_event(
                    db, user_id=uid, event_type=etype,
                    source_id=order_id or f"SEED-D{offset}-{i}",
                    event_data={"device_id": dev, "login_ip": ip, "source": "seed_demo"},
                    device_id=dev, login_ip=ip, order_id=order_id,
                )
                decisions[out.decision] = decisions.get(out.decision, 0) + 1
                per_day[str(when.date())] = per_day.get(str(when.date()), 0) + 1
                total += 1

                # 时间列整体平移，保证「案件创建时间早于审核时间」这类间隔不被破坏
                delta = int((when - out.create_time).total_seconds())
                _shift_time(db, "risk_event", "event_id", out.event_id, delta)
                _shift_time(db, "risk_feature", "event_id", out.event_id, delta)
                _shift_time(db, "risk_assessment", "assessment_id", out.assessment_id, delta)
                _shift_time(db, "risk_case", "case_id", out.case_id, delta)
                _shift_time(db, "risk_challenge", "challenge_id", out.challenge_id, delta)

                if out.case_id:
                    created.append((out.case_id, etype))
        db.commit()
    finally:
        db.close()

    print("  决策分布：" + " · ".join(f"{k} {v}" for k, v in
                                  sorted(decisions.items(), key=lambda x: -x[1])))
    print("  每日事件：" + " · ".join(f"{d[5:]} ×{n}" for d, n in sorted(per_day.items())))

    # ---- 4. 审核掉一部分案件，留一部分待审 ----
    # 只审「登录」类案件：它的处置回写是 no-op（见 case_service.NO_TABLE_ACTIONS），
    # 不会像领券/下单那样去改业务表，预填数据不该有副作用。
    reviewable = [cid for cid, etype in created if etype == "登录"]
    RNG.shuffle(reviewable)
    to_review = reviewable[: int(len(reviewable) * 0.6)]
    reviewed = {"已通过": 0, "已拒绝": 0}

    print(f"[4/5] 处置 {len(to_review)} / {len(created)} 个案件（其余留作待审核）")
    db = SessionLocal()
    try:
        for cid in to_review:
            verdict = "已拒绝" if RNG.random() < 0.55 else "已通过"
            try:
                case_service.review_case(
                    db, case_id=cid, decision=verdict, reviewer=REVIEWER,
                    review_comment=("命中高危规则，判定为恶意行为" if verdict == "已拒绝"
                                    else "业务侧确认为正常用户，解除风控拦截"),
                )
                reviewed[verdict] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ! {cid} 处置跳过：{exc}")
    finally:
        db.close()
    print("  处置结果：" + " · ".join(f"{k} {v}" for k, v in reviewed.items()))

    # ---- 5. 三色名单各来一条，让名单页 / 审计页有内容 ----
    print("[5/5] 预置名单条目（顺带产生审计流水）")
    db = SessionLocal()
    try:
        for kwargs in (
            dict(list_type="黑名单", entity_type="设备", entity_value="D-SHARED-0002",
                 reason="同设备批量领券，判定为团伙薅羊毛"),
            dict(list_type="灰名单", entity_type="IP", entity_value="172.16.0.20",
                 reason="领券聚集 IP，观察 30 天"),
            dict(list_type="白名单", entity_type="用户", entity_value="1007",
                 reason="业务侧确认为正常用户，误报澄清",
                 expire_time=now + timedelta(days=90)),
        ):
            try:
                item = list_service.add_list_item(db, created_by=REVIEWER, **kwargs)
                print(f"  ✓ {item.list_type:<4} {item.entity_type}={item.entity_value}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {kwargs['list_type']} {kwargs['entity_value']} 跳过：{exc}")
    finally:
        db.close()

    # 缓存不清的话仪表盘还会接着返回空数据（overview 有 60s TTL）
    dashboard_service.invalidate_overview_cache()
    cache.safe_delete_prefix("risk:list")
    print("\n  ✓ 已失效仪表盘 / 名单缓存")

    # ---- 汇总 ----
    section("落库结果")
    db = SessionLocal()
    try:
        for label, sql in (
            ("风控事件", "SELECT COUNT(*) c FROM risk_event"),
            ("评估记录", "SELECT COUNT(*) c FROM risk_assessment"),
            ("特征快照", "SELECT COUNT(*) c FROM risk_feature"),
            ("案件总数", "SELECT COUNT(*) c FROM risk_case"),
            ("  待审核", "SELECT COUNT(*) c FROM risk_case WHERE case_status='待审核'"),
            ("用户画像", "SELECT COUNT(*) c FROM risk_user_profile"),
            ("名单条目", "SELECT COUNT(*) c FROM risk_list"),
            ("审计流水", "SELECT COUNT(*) c FROM risk_audit_log"),
        ):
            n = db.execute(text(sql)).first().c
            print(f"  {label:<10} {n:>5}")
        trend = db.execute(text(
            "SELECT DATE(create_time) d, COUNT(*) n FROM risk_assessment "
            "GROUP BY DATE(create_time) ORDER BY d"
        )).fetchall()
        # 与 dashboard_service.loss_prevention 的金额口径保持一致（decision 在 assessment 上）
        blocked = db.execute(text(
            "SELECT COALESCE(SUM(od.final_amount), 0) AS amt "
            "FROM risk_case c "
            "JOIN risk_assessment a ON c.assessment_id = a.assessment_id "
            "JOIN order_info oi ON oi.order_id = c.source_id "
            "JOIN order_detail od ON od.order_id = oi.order_id "
            "WHERE a.decision = '拒绝' AND c.event_type IN ('下单','支付')"
        )).first().amt
    finally:
        db.close()

    check(len(trend) >= min(days, 3),
          f"趋势覆盖 {len(trend)} 个日期（近 {days} 天）—— 趋势图不会是单点")
    check(sum(1 for v in decisions if v in ("通过", "标记")) > 0,
          "决策分布含低风险档（通过 / 标记），通过率不是 0")
    check(decisions.get("拒绝", 0) > 0, "存在拒绝决策，拦截率不是 0")
    check(float(blocked or 0) > 0,
          f"预估找回资损 {float(blocked or 0):,.2f} 元（口径：被拒的下单/支付案件关联订单实付金额）")
    check(len(set(per_day.values())) > 1,
          f"每日事件量有起伏（{sorted(set(per_day.values()))}）—— 趋势不是一条横线")

    print()
    print("=" * 74)
    print("预填完成。打开 http://127.0.0.1:8000/ 看仪表盘。")
    print("=" * 74)
    return smoke.summary()


if __name__ == "__main__":
    sys.exit(main())
