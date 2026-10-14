"""
电商风控系统 - 全局配置

从 .env 文件与环境变量加载全部配置项。
所有可调参数集中在此，业务代码通过 `from app.config import settings` 读取。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ==================== 应用 ====================
    APP_NAME: str = "电商风控系统"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ==================== MySQL ====================
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "ecs"
    TEST_DB_NAME: str = "ecs_test"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ==================== Redis 缓存 ====================
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 1
    REDIS_PASSWORD: str = ""
    REDIS_ENABLED: bool = True
    CACHE_TTL_FEATURE: int = 60
    CACHE_TTL_PROFILE: int = 300
    CACHE_TTL_GRAPH: int = 300
    CACHE_TTL_RULE: int = 60

    # ==================== LLM ====================
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL_NAME: str = "qwen-plus"
    LLM_TIMEOUT: int = 60
    AGENT_SESSION_TTL: int = 86400

    # ==================== 风控决策阈值 ====================
    # 风控评分链路顺序（不可调换）：
    #   ① max(命中规则分值)
    #   ② + RISK_MULTI_RULE_BONUS × (命中数 - 1)
    #   ③ + RISK_GRAYLIST_BONUS（若命中灰名单）
    #   ④ 一票否决：仅由 rule.risk_level == "极高" 触发，抬到 RISK_VETO_MIN_SCORE
    #   ⑤ 截断至 100
    RISK_PASS_THRESHOLD: int = 30
    RISK_MARK_THRESHOLD: int = 60
    RISK_REVIEW_THRESHOLD: int = 80
    RISK_MULTI_RULE_BONUS: int = 3
    RISK_VETO_MIN_SCORE: int = 90
    RISK_GRAYLIST_BONUS: int = 15

    # ==================== 用户画像动态衰减 ====================
    PROFILE_ALPHA_HIGH: float = 0.7
    PROFILE_ALPHA_MID: float = 0.6
    PROFILE_ALPHA_LOW: float = 0.4
    PROFILE_ACTIVE_THRESHOLD: int = 5

    # ==================== 关联图谱 ====================
    GRAPH_MAX_HOPS: int = 3
    GRAPH_MAX_NODES: int = 200
    GRAPH_DEFAULT_HOPS: int = 2

    # ==================== 名单 ====================
    WHITELIST_MAX_DAYS: int = 90
    GRAYLIST_DEFAULT_DAYS: int = 30
    LIST_IMPORT_MAX_ROWS: int = 5000

    # ==================== 二次验证 ====================
    CHALLENGE_TIMEOUT_MINUTES: int = 5

    # ------------------------------------------------------------------
    # 派生配置
    # ------------------------------------------------------------------
    def get_database_url(self, db_name: str | None = None) -> str:
        """构建 MySQL 连接 URL"""
        name = db_name or self.DB_NAME
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{name}?charset=utf8mb4"
        )

    def get_redis_url(self) -> str:
        """构建 Redis 连接 URL"""
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


# 模块级单例，其他模块直接 from app.config import settings
settings = Settings()
