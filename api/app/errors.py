"""Error taxonomy for the agentic pipeline + transient-error classification.

Every failure the API can produce is one of these ``AgenticError`` subclasses, each
carrying a stable ``code``, an HTTP status, the ``stage`` it happened in, and a
``retryable`` flag. ``classify()`` decides whether an arbitrary upstream exception is
transient (worth a retry) — used by the per-stage retry wrapper.
"""
from __future__ import annotations


class AgenticError(Exception):
    """Base class. Subclasses set ``code``/``http_status``/``retryable`` defaults."""
    code = "internal_error"
    http_status = 500
    retryable = False

    def __init__(self, message: str, *, stage: str | None = None,
                 retryable: bool | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.stage = stage
        if retryable is not None:
            self.retryable = retryable
        self.retry_after = retry_after

    def to_body(self, request_id: str | None = None) -> dict:
        return {"error": {
            "code": self.code, "message": self.message, "stage": self.stage,
            "request_id": request_id, "retryable": self.retryable,
        }}


class BadRequestError(AgenticError):
    code = "bad_request"
    http_status = 400


class NotFoundError(AgenticError):
    code = "not_found"
    http_status = 404


class RetrievalError(AgenticError):
    """Critical retrieval failure (e.g. Postgres unavailable)."""
    code = "retrieval_error"
    http_status = 503
    retryable = True


class PlannerError(AgenticError):
    """Planner agent produced nothing usable / model failed."""
    code = "planner_error"
    http_status = 502
    retryable = True


class SQLWriterError(AgenticError):
    code = "sql_writer_error"
    http_status = 502
    retryable = True


class UpstreamAuthError(AgenticError):
    """OIDC/STS/Entra or upstream 401/403 — not retryable without new creds."""
    code = "upstream_auth_error"
    http_status = 502
    retryable = False


class UpstreamRateLimited(AgenticError):
    code = "upstream_rate_limited"
    http_status = 429
    retryable = True


class UpstreamTimeout(AgenticError):
    code = "upstream_timeout"
    http_status = 504
    retryable = True


class DependencyUnavailable(AgenticError):
    """A dependency (PG / embedding) is down."""
    code = "dependency_unavailable"
    http_status = 503
    retryable = True


class JobTimeout(AgenticError):
    code = "job_timeout"
    http_status = 504
    retryable = False


# --- classification helpers -------------------------------------------------

_EXPIRED_MARKERS = (
    "expiredtoken", "expiredtokenexception", "invalidsignatureexception",
    "security token included in the request is expired",
    "the provided token has expired", "unrecognizedclientexception",
)
_RATE_MARKERS = ("throttling", "toomanyrequests", "rate limit", "429",
                 "throttlingexception")
_AUTH_MARKERS = ("accessdenied", "unauthorized", "forbidden", "401", "403",
                 "invalidclienttokenid", "signaturedoesnotmatch")


def is_expired_credentials(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in _EXPIRED_MARKERS)


def classify(exc: Exception) -> bool:
    """Return True if ``exc`` looks transient (retry may help)."""
    if isinstance(exc, AgenticError):
        return exc.retryable
    name = type(exc).__name__.lower()
    text = f"{name}: {exc}".lower()
    if any(m in text for m in _AUTH_MARKERS) and "throttl" not in text:
        return False
    if any(m in text for m in _RATE_MARKERS):
        return True
    if is_expired_credentials(exc):
        return True
    # network-ish
    transient_names = ("connectionerror", "timeout", "connecttimeout",
                       "readtimeout", "operationalerror", "endpointconnectionerror",
                       "connectionreseterror", "protocolerror")
    if any(t in name for t in transient_names):
        return True
    if any(code in text for code in ("500", "502", "503", "504",
                                     "serviceunavailable", "internalservererror")):
        return True
    return False


def to_agentic(exc: Exception, *, stage: str | None = None) -> AgenticError:
    """Best-effort map an arbitrary upstream exception to the taxonomy."""
    if isinstance(exc, AgenticError):
        if stage and not exc.stage:
            exc.stage = stage
        return exc
    text = f"{type(exc).__name__}: {exc}".lower()
    if is_expired_credentials(exc) or any(m in text for m in _AUTH_MARKERS):
        if any(m in text for m in _RATE_MARKERS):
            return UpstreamRateLimited(str(exc), stage=stage)
        return UpstreamAuthError(str(exc), stage=stage)
    if any(m in text for m in _RATE_MARKERS):
        return UpstreamRateLimited(str(exc), stage=stage)
    if "timeout" in text:
        return UpstreamTimeout(str(exc), stage=stage)
    return AgenticError(str(exc), stage=stage, retryable=classify(exc))
