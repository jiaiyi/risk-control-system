-- ============================================================
-- 电商风控系统 - 风控表建表脚本
-- 共 9 张表（19 张业务表见 init_business_tables.sql）
--
-- 变更说明（相对尚硅谷参考实现）：
--   1. risk_rule          + is_deleted（逻辑删除）、分类扩至 10 类、动作新增「二次验证」
--   2. risk_event         event_type 扩至 6 类
--   3. risk_feature       entity_type 改为字符串（9 个维度，避免每加维度改表）
--   4. risk_case          + conflict_flag（名单冲突标记）
--   5. risk_blacklist  →  risk_list（三色 × 五类主体，单表建模）
--   6. risk_audit_log     新增：操作审计流水（仅追加）
--   7. risk_challenge     新增：二次验证记录
--   8. risk_list_archive  新增：名单归档表（FR-42：支撑历史名单独立检索）
-- ============================================================

USE ecs;

SET FOREIGN_KEY_CHECKS = 0;

-- ============================
-- 1. 风控规则配置表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_rule` (
  `rule_id`       varchar(50)  NOT NULL COMMENT '规则ID',
  `rule_name`     varchar(100) NOT NULL COMMENT '规则名称',
  `rule_category` enum('订单欺诈','支付风险','账户风险','售后滥用','地址风险','物流风险','登录安全','营销薅羊毛','黄牛囤货','聚集风险') NOT NULL COMMENT '风险场景分类',
  `event_type`    enum('登录','营销领券','下单','支付','售后申请','物流投诉','通用') NOT NULL DEFAULT '通用' COMMENT '适用事件类型',
  `rule_condition` text        NOT NULL COMMENT '条件表达式(JSON)',
  `risk_level`    enum('低','中','高','极高') NOT NULL COMMENT '风险等级',
  `risk_score`    int          NOT NULL COMMENT '命中分值(0-100)',
  `action`        enum('通过','标记','人工审核','二次验证','拒绝') NOT NULL COMMENT '触发动作',
  `is_enabled`    tinyint      NOT NULL DEFAULT '1' COMMENT '是否启用',
  `priority`      int          NOT NULL DEFAULT '0' COMMENT '优先级(越高越先执行)',
  `is_deleted`    tinyint      NOT NULL DEFAULT '0' COMMENT '逻辑删除标记(0正常 1已删除)',
  `description`   text         COMMENT '规则描述',
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`rule_id`),
  KEY `idx_rule_enabled_priority` (`is_enabled`,`is_deleted`,`priority`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控规则配置表';

-- ============================
-- 2. 风控事件审计表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_event` (
  `event_id`        varchar(50) NOT NULL COMMENT '事件ID',
  `event_type`      enum('登录','营销领券','下单','支付','售后申请','物流投诉') NOT NULL COMMENT '事件类型',
  `event_source_id` varchar(50) NOT NULL COMMENT '关联业务ID',
  `user_id`         varchar(50) NOT NULL COMMENT '用户ID',
  `event_data`      text        COMMENT '事件快照(JSON)',
  `create_time`     datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`event_id`),
  KEY `idx_risk_event_user_id` (`user_id`),
  KEY `idx_risk_event_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控事件审计表';

-- ============================
-- 3. 风控特征快照表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_feature` (
  `feature_id`    bigint        NOT NULL AUTO_INCREMENT COMMENT '特征ID',
  `event_id`      varchar(50)   NOT NULL COMMENT '关联事件ID',
  `entity_type`   varchar(20)   NOT NULL COMMENT '实体类型(用户/订单/地址/登录/领券/设备/IP/商品/手机号)',
  `entity_id`     varchar(50)   NOT NULL COMMENT '实体ID',
  `feature_name`  varchar(100)  NOT NULL COMMENT '特征名称',
  `feature_value` decimal(15,4) DEFAULT NULL COMMENT '特征值',
  `compute_time`  datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '计算时间',
  PRIMARY KEY (`feature_id`),
  KEY `idx_risk_feature_event_id` (`event_id`),
  KEY `idx_risk_feature_entity` (`entity_type`,`entity_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控特征快照表';

-- ============================
-- 4. 风控评估结果表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_assessment` (
  `assessment_id` varchar(50) NOT NULL COMMENT '评估ID',
  `event_id`      varchar(50) NOT NULL COMMENT '关联事件ID',
  `user_id`       varchar(50) NOT NULL COMMENT '用户ID',
  `rule_results`  text        COMMENT '规则命中明细(JSON)',
  `rule_count`    int         NOT NULL DEFAULT '0' COMMENT '命中规则数',
  `final_score`   int         NOT NULL COMMENT '最终评分(0-100)',
  `risk_level`    enum('低','中','高','极高') NOT NULL COMMENT '风险等级',
  `decision`      enum('通过','标记','人工审核','二次验证','拒绝') NOT NULL COMMENT '决策',
  `create_time`   datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`assessment_id`),
  KEY `idx_risk_assessment_user_id` (`user_id`),
  KEY `idx_risk_assessment_decision` (`decision`),
  KEY `idx_risk_assessment_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控评估结果表';

-- ============================
-- 5. 风控案件表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_case` (
  `case_id`        varchar(50) NOT NULL COMMENT '案件ID',
  `assessment_id`  varchar(50) NOT NULL COMMENT '关联评估ID',
  `user_id`        varchar(50) NOT NULL COMMENT '用户ID',
  `case_status`    enum('待审核','审核中','已通过','已拒绝','已关闭') NOT NULL DEFAULT '待审核' COMMENT '案件状态',
  `case_category`  varchar(50) DEFAULT NULL COMMENT '案件分类',
  `conflict_flag`  tinyint     NOT NULL DEFAULT '0' COMMENT '是否名单冲突案件(0否 1是)',
  `risk_detail`    text        COMMENT '风险详情(JSON)',
  `reviewer`       varchar(50) DEFAULT NULL COMMENT '审核人',
  `review_comment` text        COMMENT '审核意见',
  `review_time`    datetime    DEFAULT NULL COMMENT '审核时间',
  `source_id`      varchar(50) DEFAULT NULL COMMENT '原始业务ID(业务回溯字段)',
  `event_type`     enum('登录','营销领券','下单','支付','售后申请','物流投诉') DEFAULT NULL COMMENT '触发案件的事件类型',
  `create_time`    datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `update_time`    datetime    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`case_id`),
  KEY `idx_risk_case_status` (`case_status`),
  KEY `idx_risk_case_user_id` (`user_id`),
  KEY `idx_risk_case_conflict` (`conflict_flag`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控案件表';

-- ============================
-- 6. 风控名单表（三色 × 五类主体）
-- ============================
CREATE TABLE IF NOT EXISTS `risk_list` (
  `list_id`      bigint       NOT NULL AUTO_INCREMENT COMMENT '名单ID',
  `list_type`    enum('黑名单','白名单','灰名单') NOT NULL COMMENT '名单类型',
  `entity_type`  enum('用户','手机号','IP','设备','地址') NOT NULL COMMENT '主体类型',
  `entity_value` varchar(200) NOT NULL COMMENT '主体值',
  `reason`       text         COMMENT '加入原因/准入凭据',
  `expire_time`  datetime     DEFAULT NULL COMMENT '过期时间(NULL=永久)',
  `created_by`   varchar(50)  DEFAULT NULL COMMENT '操作人',
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`list_id`),
  UNIQUE KEY `uk_list_type_entity` (`list_type`,`entity_type`,`entity_value`),
  KEY `idx_list_lookup` (`entity_type`,`entity_value`,`expire_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控名单表(黑/白/灰)';

-- ============================
-- 7. 用户风险画像表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_user_profile` (
  `user_id`              varchar(50)   NOT NULL COMMENT '用户ID',
  `risk_score`           int           NOT NULL DEFAULT '0' COMMENT '综合风险评分(0-100,时间衰减加权)',
  `risk_level`           enum('低','中','高','极高') NOT NULL DEFAULT '低' COMMENT '风险等级',
  `total_orders`         int           NOT NULL DEFAULT '0' COMMENT '总订单数',
  `total_refunds`        int           NOT NULL DEFAULT '0' COMMENT '退款次数',
  `refund_rate`          decimal(5,4)  NOT NULL DEFAULT '0.0000' COMMENT '退款率',
  `avg_order_amount`     decimal(10,2) NOT NULL DEFAULT '0.00' COMMENT '平均订单金额',
  `address_count`        int           NOT NULL DEFAULT '0' COMMENT '地址数量',
  `complaint_count`      int           NOT NULL DEFAULT '0' COMMENT '投诉次数',
  `assessment_count`     int           NOT NULL DEFAULT '0' COMMENT '评估次数',
  `last_assessment_time` datetime      DEFAULT NULL COMMENT '最近评估时间',
  `profile_data`         text          COMMENT '扩展画像数据(JSON)',
  `update_time`          datetime      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户风险画像表';

-- ============================
-- 8. 操作审计流水表（仅追加，不提供更新/删除接口）
-- ============================
CREATE TABLE IF NOT EXISTS `risk_audit_log` (
  `log_id`       bigint       NOT NULL AUTO_INCREMENT COMMENT '日志ID',
  `operator`     varchar(50)  NOT NULL COMMENT '操作人',
  `action_type`  enum('规则','名单','案件','配置') NOT NULL COMMENT '操作类别',
  `action_desc`  varchar(100) NOT NULL COMMENT '操作描述(新增/修改/删除/启停/审核/回写)',
  `target_id`    varchar(50)  NOT NULL COMMENT '操作对象ID',
  `before_value` text         COMMENT '变更前值(JSON)',
  `after_value`  text         COMMENT '变更后值(JSON)',
  `create_time`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
  PRIMARY KEY (`log_id`),
  KEY `idx_audit_type_time` (`action_type`,`create_time`),
  KEY `idx_audit_target` (`target_id`),
  KEY `idx_audit_operator` (`operator`,`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='风控操作审计流水表';

-- ============================
-- 9. 二次验证记录表
-- ============================
CREATE TABLE IF NOT EXISTS `risk_challenge` (
  `challenge_id`  varchar(50)  NOT NULL COMMENT '验证ID',
  `assessment_id` varchar(50)  NOT NULL COMMENT '关联评估ID',
  `user_id`       varchar(50)  NOT NULL COMMENT '用户ID',
  `verify_token`  varchar(100) NOT NULL COMMENT '验证凭据(模拟通道占位)',
  `status`        enum('待验证','已验证','已超时') NOT NULL DEFAULT '待验证' COMMENT '验证状态',
  `expire_time`   datetime     NOT NULL COMMENT '验证过期时间',
  `create_time`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`challenge_id`),
  KEY `idx_challenge_status` (`status`,`expire_time`),
  KEY `idx_challenge_assessment` (`assessment_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='二次验证记录表';

-- ============================
-- 10. 名单归档表（FR-42：过期名单清理前的落脚点，支撑历史名单独立检索）
-- ============================
CREATE TABLE IF NOT EXISTS `risk_list_archive` (
  `archive_id`       bigint       NOT NULL AUTO_INCREMENT COMMENT '归档ID',
  `original_list_id` bigint       NOT NULL COMMENT '原名单ID',
  `list_type`        enum('黑名单','白名单','灰名单') NOT NULL COMMENT '名单类型',
  `entity_type`      enum('用户','手机号','IP','设备','地址') NOT NULL COMMENT '主体类型',
  `entity_value`     varchar(200) NOT NULL COMMENT '主体值',
  `reason`           text         COMMENT '原加入原因/准入凭据',
  `expire_time`      datetime     DEFAULT NULL COMMENT '原过期时间',
  `created_by`       varchar(50)  DEFAULT NULL COMMENT '原操作人',
  `create_time`      datetime     DEFAULT NULL COMMENT '原创建时间',
  `archive_time`     datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '归档时间',
  `archived_by`      varchar(50)  DEFAULT NULL COMMENT '归档触发者',
  `archive_reason`   varchar(100) DEFAULT NULL COMMENT '归档原因',
  PRIMARY KEY (`archive_id`),
  KEY `idx_archive_entity` (`entity_type`,`entity_value`),
  KEY `idx_archive_type_time` (`list_type`,`archive_time`),
  KEY `idx_archive_time` (`archive_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='名单归档表';

SET FOREIGN_KEY_CHECKS = 1;
