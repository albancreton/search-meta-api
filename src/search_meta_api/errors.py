from __future__ import annotations

from enum import StrEnum

AUTH_STATUSES = frozenset({401, 403})
QUOTA_STATUSES = frozenset({402, 429, 432, 433})
TRANSIENT_STATUSES = frozenset({408})


class ErrorClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    MALFORMED = "MALFORMED"


def classify_status(status: int) -> ErrorClass:
    """Map an HTTP status to a failover class.

    Request validation happens at the edge, so every failure reaching this
    point is the provider's problem, not the caller's. There is deliberately
    no fail-fast class.
    """
    if status in AUTH_STATUSES:
        return ErrorClass.AUTH
    if status in QUOTA_STATUSES:
        return ErrorClass.QUOTA
    if status in TRANSIENT_STATUSES or status >= 500:
        return ErrorClass.TRANSIENT
    return ErrorClass.MALFORMED


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        error_class: ErrorClass,
        status: int | None = None,
        message: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        self.provider = provider
        self.error_class = error_class
        self.status = status
        self.retry_after_s = retry_after_s
        super().__init__(f"{provider}: {error_class} status={status} {message}".strip())
