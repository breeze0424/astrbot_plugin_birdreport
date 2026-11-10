"""统一错误分类。

调用方（插件层 / 未来 MCP 入口）根据 `category` 决定如何回应用户，
而不是把底层异常直接抛给用户。
"""

from __future__ import annotations


class BirdReportError(Exception):
    """观鸟查询错误的基类，`category` 用于分类。"""

    category = "unknown"

    def __init__(self, message: str, category: str | None = None):
        super().__init__(message)
        if category is not None:
            self.category = category


class InvalidFilterError(BirdReportError):
    """入参不合法（未知城市、日期格式错误等）。"""

    category = "invalid_filter"


class NetworkError(BirdReportError):
    """网络层失败（超时、连接失败、HTTP 错误）。"""

    category = "network_error"


class ProtocolChangedError(BirdReportError):
    """站点加密协议或响应结构变化，需重新核对页面脚本。"""

    category = "protocol_changed"


class VerificationRequiredError(BirdReportError):
    """触发反滥用验证码（业务码 505/405 或“访问过于频繁”）。"""

    category = "verification_required"


class PartialResultError(BirdReportError):
    """分页期间总数变化、缺页、重复等，结果不完整。"""

    category = "partial_result"


class EmptyResult(BirdReportError):
    """查询成功但该时段无公开报告。"""

    category = "empty"
