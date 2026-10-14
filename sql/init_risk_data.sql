-- ============================================
-- 电商风控系统 - 预置规则数据初始化
-- 41 条规则覆盖 10 大风险场景
--
--   ① 订单欺诈   R001~R005    ② 支付风险   R006~R009
--   ③ 账户风险   R010~R013    ④ 售后滥用   R014~R017
--   ⑤ 地址风险   R018~R021    ⑥ 物流风险   R022~R024
--   ⑦ 登录安全   L001~L005  ← 本期新增（对应 6 类事件中的「登录」）
--   ⑧ 营销薅羊毛 C001~C005  ← 本期新增（对应「营销领券」）
--   ⑨ 黄牛囤货   B001~B003  ← 本期新增
--   ⑩ 聚集风险   A001~A004  ← 本期新增
--
-- 注：⑦~⑩ 的阈值均为初始建议值，正式上线前需用仿真回放（FR-14）校准误伤率
-- ============================================

USE ecs;

-- 清空已有规则 (方便重复执行)
TRUNCATE TABLE risk_rule;

-- ============================
-- 场景一: 订单欺诈 (5条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R001', '单笔超高金额订单', '订单欺诈', '下单',
 '{"field": "order_total_amount", "op": ">=", "value": 5000}',
 '高', 70, '人工审核', 1, 90,
 '单笔订单实付金额≥5000元，可能存在欺诈风险');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R002', '单笔极端高额订单', '订单欺诈', '下单',
 '{"field": "order_total_amount", "op": ">=", "value": 10000}',
 '极高', 95, '拒绝', 1, 100,
 '单笔订单实付金额≥10000元，一票否决');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R003', '深夜下单', '订单欺诈', '下单',
 '{"field": "order_is_night", "op": "==", "value": 1}',
 '中', 30, '标记', 1, 40,
 '凌晨0-6点下单，标记关注');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R004', '极速支付', '订单欺诈', '下单',
 '{"field": "order_pay_interval_sec", "op": "between", "value": [0, 5]}',
 '高', 60, '人工审核', 1, 70,
 '下单到支付间隔≤5秒，疑似机器下单');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R005', '高折扣率订单', '订单欺诈', '下单',
 '{"field": "order_discount_rate", "op": ">=", "value": 0.7}',
 '高', 65, '人工审核', 1, 60,
 '订单折扣率≥70%，疑似利用优惠漏洞');

-- ============================
-- 场景二: 支付风险 (4条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R006', '用户7天内高频下单', '支付风险', '支付',
 '{"field": "user_orders_7d", "op": ">=", "value": 10}',
 '高', 70, '人工审核', 1, 80,
 '7天内下单≥10次，疑似刷单');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R007', '用户30天内大量下单', '支付风险', '支付',
 '{"field": "user_orders_30d", "op": ">=", "value": 30}',
 '极高', 90, '拒绝', 1, 95,
 '30天内下单≥30次，严重刷单嫌疑，一票否决');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R008', '订单金额远超用户平均', '支付风险', '支付',
 '{"and": [{"field": "order_total_amount", "op": ">=", "value": 2000}, {"field": "user_avg_order_amount", "op": "<", "value": 500}]}',
 '高', 65, '人工审核', 1, 75,
 '当前订单金额≥2000且用户历史平均<500，消费行为异常');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R009', '用户累计消费异常', '支付风险', '支付',
 '{"field": "user_total_amount", "op": ">=", "value": 50000}',
 '中', 40, '标记', 1, 50,
 '用户累计消费≥5万元，标记为高消费用户');

-- ============================
-- 场景三: 账户风险 (4条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R010', '高取消率用户', '账户风险', '通用',
 '{"and": [{"field": "user_cancel_count", "op": ">=", "value": 5}, {"field": "user_total_orders", "op": ">=", "value": 5}]}',
 '中', 45, '标记', 1, 55,
 '取消订单≥5次且总订单≥5，取消行为异常');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R011', '新用户大额订单', '账户风险', '下单',
 '{"and": [{"field": "user_total_orders", "op": "<=", "value": 2}, {"field": "order_total_amount", "op": ">=", "value": 3000}]}',
 '高', 75, '人工审核', 1, 85,
 '历史订单≤2且当前订单≥3000元，新用户大额消费风险');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R012', '用户有大量投诉记录', '账户风险', '通用',
 '{"field": "user_complaint_count", "op": ">=", "value": 3}',
 '高', 60, '人工审核', 1, 65,
 '投诉次数≥3，账户存在风险');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R013', '多地址用户', '账户风险', '通用',
 '{"field": "user_address_count", "op": ">=", "value": 5}',
 '中', 35, '标记', 1, 45,
 '用户收货地址≥5个，关注账户共享可能');

-- ============================
-- 场景四: 售后滥用 (4条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R014', '高退款率用户', '售后滥用', '售后申请',
 '{"and": [{"field": "user_refund_rate", "op": ">=", "value": 0.5}, {"field": "user_total_orders", "op": ">=", "value": 5}]}',
 '高', 70, '人工审核', 1, 80,
 '退款率≥50%且订单≥5单，疑似恶意退款');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R015', '极端退款率用户', '售后滥用', '售后申请',
 '{"and": [{"field": "user_refund_rate", "op": ">=", "value": 0.8}, {"field": "user_total_orders", "op": ">=", "value": 3}]}',
 '极高', 92, '拒绝', 1, 98,
 '退款率≥80%且订单≥3单，严重售后滥用，一票否决');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R016', '高售后率用户', '售后滥用', '售后申请',
 '{"and": [{"field": "user_postsale_rate", "op": ">=", "value": 0.4}, {"field": "user_total_orders", "op": ">=", "value": 5}]}',
 '中', 50, '标记', 1, 60,
 '售后率≥40%且订单≥5，售后行为需关注');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R017', '高退款金额用户', '售后滥用', '售后申请',
 '{"field": "user_refund_amount", "op": ">=", "value": 5000}',
 '高', 55, '人工审核', 1, 55,
 '累计退款金额≥5000元，退款金额异常');

-- ============================
-- 场景五: 地址风险 (4条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R018', '新收货地址下单', '地址风险', '下单',
 '{"field": "addr_is_new", "op": "==", "value": 1}',
 '低', 15, '通过', 1, 20,
 '使用新收货地址下单，低风险标记');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R019', '新地址+大额订单', '地址风险', '下单',
 '{"and": [{"field": "addr_is_new", "op": "==", "value": 1}, {"field": "order_total_amount", "op": ">=", "value": 2000}]}',
 '高', 70, '人工审核', 1, 82,
 '新地址且订单金额≥2000元，地址欺诈风险');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R020', '多省份地址用户', '地址风险', '通用',
 '{"field": "addr_province_count", "op": ">=", "value": 3}',
 '中', 40, '标记', 1, 50,
 '用户地址覆盖≥3个省份，地址分散异常');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R021', '大量地址用户+高金额', '地址风险', '下单',
 '{"and": [{"field": "addr_total_count", "op": ">=", "value": 4}, {"field": "order_total_amount", "op": ">=", "value": 1500}]}',
 '高', 65, '人工审核', 1, 70,
 '地址≥4个且订单金额≥1500元，疑似代收/转单');

-- ============================
-- 场景六: 物流风险 (3条)
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R022', '投诉用户下单', '物流风险', '下单',
 '{"field": "user_complaint_count", "op": ">=", "value": 1}',
 '低', 20, '通过', 1, 30,
 '有物流投诉记录的用户下单，低风险标记');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R023', '高频投诉用户', '物流风险', '物流投诉',
 '{"field": "user_complaint_count", "op": ">=", "value": 5}',
 '高', 60, '人工审核', 1, 70,
 '投诉次数≥5次，疑似恶意投诉');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('R024', '大量商品类别订单', '物流风险', '下单',
 '{"field": "order_category_count", "op": ">=", "value": 5}',
 '中', 35, '标记', 1, 40,
 '单笔订单包含≥5个商品类别，购买行为异常');

-- ============================
-- 场景七: 登录安全 (5条) —— 本期新增
-- 数据来源: user_login_record
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('L001', '短时高频登录失败', '登录安全', '登录',
 '{"field": "login_fail_count_1h", "op": ">=", "value": 5}',
 '高', 70, '人工审核', 1, 78,
 '近1小时登录失败≥5次，典型撞库试登特征');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('L002', '同设备多账号登录', '登录安全', '登录',
 '{"field": "device_account_count_24h", "op": ">=", "value": 3}',
 '高', 75, '人工审核', 1, 85,
 '近24小时同一设备登录≥3个账号，疑似批量养号或团伙作业');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('L003', '同IP多账号登录', '登录安全', '登录',
 '{"field": "ip_account_count_24h", "op": ">=", "value": 5}',
 '高', 65, '人工审核', 1, 72,
 '近24小时同一IP登录≥5个账号，疑似代理池批量操作');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('L004', '新设备异地登录', '登录安全', '登录',
 '{"and": [{"field": "login_is_new_device", "op": "==", "value": 1}, {"field": "login_city_count_7d", "op": ">=", "value": 3}]}',
 '极高', 90, '拒绝', 1, 92,
 '首次使用的设备且近7天登录涉及≥3个城市，高度疑似盗号，一票否决');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('L005', '短时高频登录', '登录安全', '登录',
 '{"field": "login_count_1h", "op": ">=", "value": 10}',
 '中', 40, '标记', 1, 55,
 '近1小时登录≥10次，登录频率明显高于人工操作');

-- ============================
-- 场景八: 营销薅羊毛 (5条) —— 本期新增
-- 数据来源: coupon_receive_record
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('C001', '短时批量领券', '营销薅羊毛', '营销领券',
 '{"field": "coupon_count_1h", "op": ">=", "value": 10}',
 '高', 70, '人工审核', 1, 80,
 '近1小时领券≥10次，人工无法达到的频率，疑似脚本');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('C002', '同设备多账号领券', '营销薅羊毛', '营销领券',
 '{"field": "coupon_device_account_count_24h", "op": ">=", "value": 3}',
 '极高', 90, '拒绝', 1, 93,
 '近24小时同一设备≥3个账号领券，批量注册薅羊毛，一票否决');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('C003', '高频领券', '营销薅羊毛', '营销领券',
 '{"field": "coupon_count_24h", "op": ">=", "value": 20}',
 '高', 65, '人工审核', 1, 74,
 '近24小时领券≥20次，领券行为异常');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('C004', '全券种扫荡', '营销薅羊毛', '营销领券',
 '{"field": "coupon_type_count_24h", "op": ">=", "value": 5}',
 '中', 45, '标记', 1, 58,
 '近24小时领取≥5种不同类型的券，把可领券种全部领一遍，非真实消费意图');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('C005', '大额累计领券', '营销薅羊毛', '营销领券',
 '{"field": "coupon_amount_24h", "op": ">=", "value": 500}',
 '高', 60, '人工审核', 1, 68,
 '近24小时累计券面金额≥500元，套利风险');

-- ============================
-- 场景九: 黄牛囤货 (3条) —— 本期新增
-- 数据来源: order_detail / sku_info
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('B001', '单SKU超量购买', '黄牛囤货', '下单',
 '{"field": "sku_buy_count_24h", "op": ">=", "value": 10}',
 '高', 70, '人工审核', 1, 76,
 '单个SKU近24小时购买≥10件，疑似黄牛囤货');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('B002', '拆单规避限购', '黄牛囤货', '下单',
 '{"and": [{"field": "sku_order_count_24h", "op": ">=", "value": 5}, {"field": "sku_buy_count_24h", "op": ">=", "value": 8}]}',
 '高', 65, '人工审核', 1, 70,
 '同一SKU分≥5笔订单共购买≥8件，疑似拆单绕过限购');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('B003', '高价值商品囤积', '黄牛囤货', '下单',
 '{"and": [{"field": "order_total_amount", "op": ">=", "value": 3000}, {"field": "sku_buy_count_24h", "op": ">=", "value": 5}]}',
 '极高', 90, '拒绝', 1, 91,
 '高客单且同一SKU≥5件，高价值商品囤积，一票否决');

-- ============================
-- 场景十: 聚集风险 (4条) —— 本期新增
-- 数据来源: receive_info / order_info / user_login_record 的跨账号聚合
-- ============================

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('A001', '同地址多账号下单', '聚集风险', '下单',
 '{"field": "addr_account_count", "op": ">=", "value": 3}',
 '高', 70, '人工审核', 1, 79,
 '同一收货地址关联≥3个账号，疑似代收转单团伙');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('A002', '同地址多账号退款', '聚集风险', '售后申请',
 '{"and": [{"field": "addr_account_count", "op": ">=", "value": 2}, {"field": "user_refund_rate", "op": ">=", "value": 0.5}]}',
 '极高', 90, '拒绝', 1, 94,
 '收货地址关联≥2个账号且退款率≥50%，恶意退款团伙，一票否决（对应2.3示例场景二）');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('A003', '同设备短时集中下单', '聚集风险', '下单',
 '{"field": "device_order_count_1h", "op": ">=", "value": 5}',
 '高', 65, '人工审核', 1, 73,
 '同一设备近1小时下单≥5笔，疑似批量刷单');

INSERT INTO risk_rule (rule_id, rule_name, rule_category, event_type, rule_condition, risk_level, risk_score, action, is_enabled, priority, description) VALUES
('A004', '同IP批量注册', '聚集风险', '通用',
 '{"field": "ip_register_account_count", "op": ">=", "value": 5}',
 '高', 70, '人工审核', 1, 77,
 '同一IP注册≥5个账号，批量注册特征');
