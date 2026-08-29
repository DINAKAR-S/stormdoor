"""Error types and the OpenAI-compatible error envelope.

Clients that already speak the OpenAI API expect errors shaped like
``{"error": {"message", "type", "code", "param"}}``. Anything that leaves this
gateway keeps that shape so an existing SDK's error handling still works.
"""

from __future__ import annotations

from typing import Any


class StormdoorError(Exception):
    """Base class for every error the gateway raises deliberately."""

    status_code: int = 500
    error_type: str = "internal_error"
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, param: str | None = None):
        super().__init__(message)
        self.message = message
        self.param = param
        if code is not None:
            self.code = code

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": self.param,
            }
        }


class AuthError(StormdoorError):
    status_code = 401
    error_type = "invalid_request_error"
    code = "invalid_api_key"


class ForbiddenError(StormdoorError):
    status_code = 403
    error_type = "invalid_request_error"
    code = "model_not_allowed"


class BadRequest(StormdoorError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "invalid_request"


class UnknownModel(StormdoorError):
    status_code = 404
    error_type = "invalid_request_error"
    code = "model_not_found"


class BudgetExceeded(StormdoorError):
    """Refused before the upstream call, not reconciled after it.

    Carries the numbers that produced the decision so the caller can see
    exactly why the request was turned away.
    """

    status_code = 402
    error_type = "insufficient_quota"
    code = "budget_exceeded"

    def __init__(self, message: str, *, spent_usd: float, budget_usd: float, estimate_usd: float):
        super().__init__(message)
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        self.estimate_usd = estimate_usd

    def envelope(self) -> dict[str, Any]:
        env = super().envelope()
        env["error"]["budget"] = {
            "spent_usd": round(self.spent_usd, 6),
            "budget_usd": round(self.budget_usd, 6),
            "estimated_cost_usd": round(self.estimate_usd, 6),
        }
        return env


class RateLimited(StormdoorError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"

    def __init__(self, message: str, *, retry_after_s: float, limit: str):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.limit = limit

    def envelope(self) -> dict[str, Any]:
        env = super().envelope()
        env["error"]["retry_after_s"] = round(self.retry_after_s, 3)
        env["error"]["limit"] = self.limit
        return env


class ProviderError(StormdoorError):
    """An upstream failure, classified so the router knows what to retry.

    ``retryable`` is the field the fallback engine will read. Connection
    errors, timeouts, 408, 409, 429 and 5xx are retryable; a 400 or a 404 is
    the caller's problem and retrying it just burns latency.
    """

    error_type = "api_error"
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int = 502,
        retryable: bool = False,
        upstream_status: int | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.upstream_status = upstream_status

    def envelope(self) -> dict[str, Any]:
        env = super().envelope()
        env["error"]["provider"] = self.provider
        env["error"]["retryable"] = self.retryable
        if self.upstream_status is not None:
            env["error"]["upstream_status"] = self.upstream_status
        return env


class ChaosInjected(ProviderError):
    """A fault this gateway created on purpose.

    It behaves exactly like a real provider failure so the same code paths run,
    but it is tagged in the usage ledger so a failure drill is never confused
    with a real outage.
    """

    code = "chaos_injected"

    def __init__(self, message: str, *, fault: str, status_code: int = 503):
        super().__init__(
            message,
            provider="stormdoor-chaos",
            status_code=status_code,
            retryable=status_code >= 500 or status_code == 429,
            upstream_status=status_code,
        )
        self.fault = fault

    def envelope(self) -> dict[str, Any]:
        env = super().envelope()
        env["error"]["chaos_fault"] = self.fault
        return env
