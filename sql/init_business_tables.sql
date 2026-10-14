-- ============================================
-- 电商风控系统 - 业务表 DDL 初始化脚本
-- 创建 17 张电商业务表 (按外键依赖顺序)
-- ============================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ============================
-- 第一层: 基础维度表 (无外键依赖)
-- ============================

-- 1. 用户信息表
CREATE TABLE IF NOT EXISTS `user_info` (
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户信息表';

-- 2. 区域表
CREATE TABLE IF NOT EXISTS `region` (
  `province` varchar(20) NOT NULL COMMENT '省',
  `city` varchar(20) NOT NULL COMMENT '市',
  `district` varchar(20) NOT NULL COMMENT '区',
  PRIMARY KEY (`province`,`city`,`district`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='区域表';

-- 3. 商品类别表
CREATE TABLE IF NOT EXISTS `product_category` (
  `product_category` varchar(20) NOT NULL COMMENT '商品类别',
  PRIMARY KEY (`product_category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品类别表';

-- 4. 订单状态表
CREATE TABLE IF NOT EXISTS `order_status` (
  `order_status` varchar(20) NOT NULL COMMENT '订单状态',
  `status_code` int DEFAULT NULL COMMENT '状态码',
  PRIMARY KEY (`order_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单状态表';

-- 5. 物流公司表
CREATE TABLE IF NOT EXISTS `logistics_company` (
  `company_name` varchar(20) NOT NULL COMMENT '物流公司名称',
  PRIMARY KEY (`company_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='物流公司表';

-- 6. 售后状态表
CREATE TABLE IF NOT EXISTS `postsale_status` (
  `postsale_status` varchar(20) NOT NULL COMMENT '售后状态',
  `is_refund` tinyint(1) NOT NULL COMMENT '是否退款',
  `is_return` tinyint(1) NOT NULL COMMENT '是否退货',
  `is_exchange` tinyint(1) NOT NULL COMMENT '是否换货',
  `status_code` int DEFAULT NULL COMMENT '状态码',
  PRIMARY KEY (`postsale_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='售后状态表';

-- ============================
-- 第二层: 依赖基础维度表
-- ============================

-- 7. 收货信息表
CREATE TABLE IF NOT EXISTS `receive_info` (
  `receive_id` varchar(50) NOT NULL COMMENT '收货信息ID',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `receiver_name` varchar(50) NOT NULL COMMENT '收货人姓名',
  `receiver_phone` varchar(50) NOT NULL COMMENT '收货人电话',
  `receive_province` varchar(50) NOT NULL COMMENT '收货省',
  `receive_city` varchar(50) NOT NULL COMMENT '收货市',
  `receive_district` varchar(50) NOT NULL COMMENT '收货区',
  `receive_street_address` varchar(50) NOT NULL COMMENT '收货详细地址',
  PRIMARY KEY (`receive_id`),
  UNIQUE KEY `user_id` (`user_id`,`receiver_name`,`receiver_phone`,`receive_province`,`receive_city`,`receive_district`,`receive_street_address`),
  KEY `receive_province` (`receive_province`,`receive_city`,`receive_district`),
  CONSTRAINT `receive_info_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user_info` (`user_id`),
  CONSTRAINT `receive_info_ibfk_2` FOREIGN KEY (`receive_province`, `receive_city`, `receive_district`) REFERENCES `region` (`province`, `city`, `district`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='收货信息表';

-- 8. 商品信息表
CREATE TABLE IF NOT EXISTS `sku_info` (
  `sku_id` varchar(50) NOT NULL COMMENT '商品ID',
  `sku_name` varchar(100) NOT NULL COMMENT '商品名称',
  `sku_price` decimal(10,2) NOT NULL COMMENT '商品单价',
  `sku_category` varchar(20) NOT NULL COMMENT '商品类别',
  `sku_count` int NOT NULL COMMENT '商品库存',
  PRIMARY KEY (`sku_id`),
  KEY `sku_category` (`sku_category`),
  CONSTRAINT `sku_info_ibfk_1` FOREIGN KEY (`sku_category`) REFERENCES `product_category` (`product_category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品信息表';

-- 9. 售后原因表
CREATE TABLE IF NOT EXISTS `postsale_reason` (
  `postsale_reason` varchar(100) NOT NULL COMMENT '售后原因',
  `product_category` varchar(20) DEFAULT NULL COMMENT '商品类别',
  PRIMARY KEY (`postsale_reason`),
  KEY `product_category` (`product_category`),
  CONSTRAINT `postsale_reason_ibfk_1` FOREIGN KEY (`product_category`) REFERENCES `product_category` (`product_category`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='售后原因表';

-- ============================
-- 第三层: 核心业务表
-- ============================

-- 10. 订单表
CREATE TABLE IF NOT EXISTS `order_info` (
  `order_id` varchar(50) NOT NULL COMMENT '订单ID',
  `create_time` timestamp NOT NULL COMMENT '创建时间',
  `payment_time` timestamp NULL DEFAULT NULL COMMENT '支付时间',
  `delivered_time` timestamp NULL DEFAULT NULL COMMENT '签收时间',
  `complete_time` timestamp NULL DEFAULT NULL COMMENT '完成时间',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `receive_id` varchar(50) NOT NULL COMMENT '收货信息ID',
  `order_status` varchar(20) NOT NULL COMMENT '订单状态',
  PRIMARY KEY (`order_id`),
  KEY `user_id` (`user_id`),
  KEY `receive_id` (`receive_id`),
  KEY `order_status` (`order_status`),
  CONSTRAINT `order_info_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user_info` (`user_id`),
  CONSTRAINT `order_info_ibfk_2` FOREIGN KEY (`receive_id`) REFERENCES `receive_info` (`receive_id`),
  CONSTRAINT `order_info_ibfk_3` FOREIGN KEY (`order_status`) REFERENCES `order_status` (`order_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单表';

-- 11. 物流表
CREATE TABLE IF NOT EXISTS `logistics` (
  `logistics_id` varchar(50) NOT NULL COMMENT '物流ID',
  `create_time` timestamp NOT NULL COMMENT '创建时间',
  `delivered_time` timestamp NULL DEFAULT NULL COMMENT '签收时间',
  `logistics_tracking` varchar(500) DEFAULT NULL COMMENT '物流明细',
  `logistics_category` enum('退货','换货退货','换货发货') DEFAULT NULL COMMENT '物流类别',
  PRIMARY KEY (`logistics_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='物流表';

-- ============================
-- 第四层: 关联表和明细表
-- ============================

-- 12. 订单明细表
CREATE TABLE IF NOT EXISTS `order_detail` (
  `order_detail_id` varchar(50) NOT NULL COMMENT '订单明细ID',
  `order_id` varchar(50) NOT NULL COMMENT '订单ID',
  `sku_id` varchar(50) NOT NULL COMMENT '商品ID',
  `sku_name` varchar(100) NOT NULL COMMENT '商品名称',
  `sku_count` int NOT NULL COMMENT '商品数量',
  `total_amount` decimal(10,2) NOT NULL COMMENT '总计金额',
  `discount_amount` decimal(10,2) DEFAULT '0.00' COMMENT '优惠金额',
  `final_amount` decimal(10,2) NOT NULL COMMENT '实付金额',
  PRIMARY KEY (`order_detail_id`),
  KEY `sku_id` (`sku_id`),
  KEY `order_id` (`order_id`),
  CONSTRAINT `order_detail_ibfk_1` FOREIGN KEY (`sku_id`) REFERENCES `sku_info` (`sku_id`),
  CONSTRAINT `order_detail_ibfk_2` FOREIGN KEY (`order_id`) REFERENCES `order_info` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单明细表';

-- 13. 订单与物流关联表
CREATE TABLE IF NOT EXISTS `order_logistics` (
  `order_id` varchar(50) NOT NULL COMMENT '订单ID',
  `logistics_id` varchar(50) NOT NULL COMMENT '物流ID',
  PRIMARY KEY (`order_id`,`logistics_id`),
  KEY `logistics_id` (`logistics_id`),
  CONSTRAINT `order_logistics_ibfk_1` FOREIGN KEY (`order_id`) REFERENCES `order_info` (`order_id`),
  CONSTRAINT `order_logistics_ibfk_2` FOREIGN KEY (`logistics_id`) REFERENCES `logistics` (`logistics_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='订单与物流关联表';

-- 14. 物流投诉内容对照表
CREATE TABLE IF NOT EXISTS `logistics_complaint` (
  `logistics_status` varchar(20) NOT NULL COMMENT '物流状态',
  `logistics_complaint` varchar(100) NOT NULL COMMENT '物流投诉内容',
  PRIMARY KEY (`logistics_status`,`logistics_complaint`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='物流投诉内容对照表';

-- 15. 物流投诉记录表
CREATE TABLE IF NOT EXISTS `logistics_complaints_record` (
  `record_id` bigint NOT NULL AUTO_INCREMENT COMMENT '投诉记录ID',
  `logistics_id` varchar(50) NOT NULL COMMENT '物流ID',
  `logistics_complaint` varchar(500) NOT NULL COMMENT '物流投诉内容',
  `complaint_time` timestamp NOT NULL COMMENT '投诉时间',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  PRIMARY KEY (`record_id`),
  KEY `user_id` (`user_id`),
  KEY `logistics_id` (`logistics_id`),
  CONSTRAINT `logistics_complaints_record_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user_info` (`user_id`),
  CONSTRAINT `logistics_complaints_record_ibfk_2` FOREIGN KEY (`logistics_id`) REFERENCES `logistics` (`logistics_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='物流投诉记录表';

-- ============================
-- 第五层: 售后相关
-- ============================

-- 16. 售后表
CREATE TABLE IF NOT EXISTS `postsale` (
  `postsale_id` varchar(50) NOT NULL COMMENT '售后ID',
  `create_time` timestamp NOT NULL COMMENT '创建时间',
  `complete_time` timestamp NULL DEFAULT NULL COMMENT '完成时间',
  `order_detail_id` varchar(50) NOT NULL COMMENT '订单明细ID',
  `refund_amount` decimal(10,2) DEFAULT '0.00' COMMENT '退款金额',
  `postsale_type` enum('退款','退货','换货') DEFAULT NULL COMMENT '售后类型',
  `postsale_reason` varchar(500) NOT NULL COMMENT '售后原因',
  `postsale_status` varchar(20) NOT NULL COMMENT '售后状态',
  `receive_id` varchar(50) NOT NULL COMMENT '收货信息ID',
  PRIMARY KEY (`postsale_id`),
  KEY `receive_id` (`receive_id`),
  KEY `order_detail_id` (`order_detail_id`),
  KEY `postsale_status` (`postsale_status`),
  CONSTRAINT `postsale_ibfk_1` FOREIGN KEY (`receive_id`) REFERENCES `receive_info` (`receive_id`),
  CONSTRAINT `postsale_ibfk_2` FOREIGN KEY (`order_detail_id`) REFERENCES `order_detail` (`order_detail_id`),
  CONSTRAINT `postsale_ibfk_3` FOREIGN KEY (`postsale_status`) REFERENCES `postsale_status` (`postsale_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='售后表';

-- 17. 售后与物流关联表
CREATE TABLE IF NOT EXISTS `postsale_logistics` (
  `postsale_id` varchar(50) NOT NULL COMMENT '售后ID',
  `logistics_id` varchar(50) NOT NULL COMMENT '物流ID',
  PRIMARY KEY (`postsale_id`,`logistics_id`),
  KEY `logistics_id` (`logistics_id`),
  CONSTRAINT `postsale_logistics_ibfk_1` FOREIGN KEY (`postsale_id`) REFERENCES `postsale` (`postsale_id`),
  CONSTRAINT `postsale_logistics_ibfk_2` FOREIGN KEY (`logistics_id`) REFERENCES `logistics` (`logistics_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='售后与物流关联表';

-- ============================
-- 第五层: 本期新增业务表 (2 张)
-- 说明: 支撑「登录」「营销领券」两类事件的风控
-- ============================

-- 18. 用户登录记录表
CREATE TABLE IF NOT EXISTS `user_login_record` (
  `login_id` varchar(50) NOT NULL COMMENT '登录记录ID',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `login_time` timestamp NOT NULL COMMENT '登录时间',
  `device_id` varchar(100) DEFAULT NULL COMMENT '设备标识',
  `login_ip` varchar(50) DEFAULT NULL COMMENT '登录IP',
  `login_city` varchar(50) DEFAULT NULL COMMENT '登录城市',
  `login_result` enum('成功','失败') NOT NULL DEFAULT '成功' COMMENT '登录结果',
  PRIMARY KEY (`login_id`),
  KEY `idx_login_user_time` (`user_id`,`login_time`),
  KEY `idx_login_device_time` (`device_id`,`login_time`),
  KEY `idx_login_ip_time` (`login_ip`,`login_time`),
  CONSTRAINT `login_record_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user_info` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户登录记录表';

-- 19. 优惠券领取记录表
CREATE TABLE IF NOT EXISTS `coupon_receive_record` (
  `receive_id` varchar(50) NOT NULL COMMENT '领取记录ID',
  `user_id` varchar(50) NOT NULL COMMENT '用户ID',
  `coupon_id` varchar(50) NOT NULL COMMENT '券标识',
  `coupon_type` varchar(50) NOT NULL COMMENT '券种',
  `coupon_amount` decimal(10,2) NOT NULL DEFAULT '0.00' COMMENT '券面金额',
  `receive_time` timestamp NOT NULL COMMENT '领取时间',
  `device_id` varchar(100) DEFAULT NULL COMMENT '设备标识',
  `use_status` enum('未使用','已使用','已过期') NOT NULL DEFAULT '未使用' COMMENT '使用状态',
  `use_time` timestamp NULL DEFAULT NULL COMMENT '使用时间',
  PRIMARY KEY (`receive_id`),
  KEY `idx_coupon_user_time` (`user_id`,`receive_time`),
  KEY `idx_coupon_device_time` (`device_id`,`receive_time`),
  KEY `idx_coupon_type_time` (`coupon_type`,`receive_time`),
  CONSTRAINT `coupon_record_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user_info` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='优惠券领取记录表';

SET FOREIGN_KEY_CHECKS = 1;
