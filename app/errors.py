"""API 错误类型：统一输出 `{"error": ...}`。"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, **extra):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.extra = extra
