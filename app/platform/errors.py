"""Platform-level exception hierarchy."""

from enum import StrEnum


class ErrorKind(StrEnum):
    VALIDATION      = "invalid_request_error"
    AUTHENTICATION  = "authentication_error"
    RATE_LIMIT      = "rate_limit_exceeded"
    UPSTREAM        = "upstream_error"
    SERVER          = "server_error"


class AppError(Exception):
    """Base exception for all application errors."""

    def __init__(
        self,
        message:    str,
        *,
        kind:       ErrorKind = ErrorKind.SERVER,
        code:       str       = "internal_error",
        status:     int       = 500,
        details:    dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind    = kind
        self.code    = code
        self.status  = status
        self.details = details or {}

    def to_dict(self) -> dict:
        err = {
            "message": self.message,
            "type":    self.kind,
            "code":    self.code,
        }
        if "param" in self.details:
            err["param"] = self.details["param"]
        return {"error": err}


class ValidationError(AppError):
    def __init__(self, message: str, *, param: str = "", code: str = "invalid_value") -> None:
        super().__init__(
            message, kind=ErrorKind.VALIDATION, code=code, status=400,
            details={"param": param},
        )
        self.param = param


class AuthError(AppError):
    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(
            message, kind=ErrorKind.AUTHENTICATION, code="invalid_api_key", status=401,
        )


class RateLimitError(AppError):
    def __init__(self, message: str = "No available accounts") -> None:
        super().__init__(
            message, kind=ErrorKind.RATE_LIMIT, code="rate_limit_exceeded", status=429,
        )


class UpstreamError(AppError):
    def __init__(
        self,
        message: str,
        *,
        status:           int  = 502,
        body:             str  = "",
        retry_after_ms:   int | None = None,
        code:             str  = "upstream_error",
    ) -> None:
        details: dict = {"body": body}
        if retry_after_ms is not None:
            details["retry_after_ms"] = int(retry_after_ms)
        super().__init__(
            message, kind=ErrorKind.UPSTREAM, code=code, status=status,
            details=details,
        )
        self.retry_after_ms = retry_after_ms

    @classmethod
    def from_response(
        cls,
        status: int,
        body:   str = "",
        *,
        message:           str  | None = None,
        retry_after_ms:    int  | None = None,
        code:              str  | None = None,
    ) -> "UpstreamError":
        """Build an UpstreamError from a raw upstream response.

        Centralises the status → code mapping that was previously open-
        coded at every call site:

          401, 403  → ``"unauthorized"`` (auth-related; refresh service
                      may want to mark the account as invalid)
          404       → ``"not_found"``
          408, 504  → ``"timeout"``
          429       → ``"rate_limited"`` (and retry_after_ms is copied
                      from the Retry-After header if present)
          5xx       → ``"server_error"``
          default   → ``"upstream_error"``

        The caller is still free to override ``code`` explicitly.
        """
        if message is None:
            message = f"Upstream returned {status}"
        if code is None:
            code = _STATUS_TO_CODE.get(int(status), "upstream_error")
        # Auto-extract Retry-After when caller didn't pass it explicitly.
        if retry_after_ms is None and int(status) == 429:
            # Body may carry the value as "retry_after: 12" or similar;
            # left to caller to pass the parsed value when meaningful.
            pass
        return cls(
            message,
            status=status,
            body=body,
            retry_after_ms=retry_after_ms,
            code=code,
        )


_STATUS_TO_CODE: dict[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    408: "timeout",
    429: "rate_limited",
    500: "server_error",
    502: "bad_gateway",
    503: "server_unavailable",
    504: "timeout",
}


class StreamIdleTimeout(AppError):
    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            f"Stream idle timeout after {timeout_s}s",
            kind=ErrorKind.UPSTREAM, code="stream_idle_timeout", status=504,
        )


__all__ = [
    "ErrorKind", "AppError",
    "ValidationError", "AuthError", "RateLimitError",
    "UpstreamError", "StreamIdleTimeout",
]
