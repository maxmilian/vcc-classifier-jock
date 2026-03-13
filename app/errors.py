from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AppError(Exception):
    status_code: int
    error_code: str
    message: str
    retryable: bool = False
    provider_status: int | None = None

    def __str__(self) -> str:
        return self.message

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.provider_status is not None:
            payload["provider_status"] = self.provider_status
        return payload


def map_http_error_code(status_code: int) -> str:
    if status_code == 400:
        return "BAD_REQUEST"
    if status_code == 401:
        return "UNAUTHORIZED"
    if status_code == 403:
        return "FORBIDDEN"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 408:
        return "UPSTREAM_TIMEOUT"
    if status_code == 409:
        return "CONFLICT"
    if status_code == 422:
        return "INVALID_REQUEST"
    if status_code == 429:
        return "RATE_LIMITED"
    if status_code >= 500:
        return "INTERNAL_ERROR"
    return "HTTP_ERROR"
