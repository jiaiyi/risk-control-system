# -*- coding: utf-8 -*-
"""服务层统一业务异常

错误码体系（SPEC 6.3）：
    1xxx  `BIZ_*`   业务规则错误（可重试 / 需用户修正）
    2xxx  `SYS_*`   系统或依赖错误
    3xxx  `AUTH_*`  鉴权（本期未启用，预留）

各服务的异常只需继承 `ServiceError` 并声明 `code` / `http_status`
（子类通常连 `__init__` 都不用写）。接口层用**一个**异常处理器即可统一转成
`{code, message, detail}` 响应体 —— 不用在每个 Router 里逐个 try/except。

关键约定（SPEC 6.3）：
    · 缓存不可用属**警告级**，只记日志不返回错误（NFR-14）
    · `BIZ_WRITEBACK_FAILED` 场景 HTTP 仍返回 200，由响应体 `writeback.success=false` 表达
"""
from typing import Optional


class ServiceError(Exception):
    """所有服务层业务异常的基类"""

    code: str = "BIZ_ERROR"
    http_status: int = 400

    def __init__(self, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class SystemServiceError(ServiceError):
    """系统 / 依赖类错误（2xxx）"""

    code = "SYS_ERROR"
    http_status = 500


class CacheUnavailableError(SystemServiceError):
    """缓存不可用 —— **警告级**，不阻断风控主链路（NFR-14 / SPEC 6.3 约定 1）"""

    code = "SYS_CACHE_UNAVAILABLE"
