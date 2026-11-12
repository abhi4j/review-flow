"""Domain errors and their mapping to RFC 9457 problem+json responses.

Each error declares the HTTP status and a client-safe `detail`. Internal context (upstream
messages, validation traces) is logged by the raiser, never echoed to the client.
"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


class ServiceError(Exception):
    status: int = 500
    title: str = "Internal Server Error"

    def __init__(self, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


class InvalidRequest(ServiceError):
    status, title = 422, "Invalid request"


class RepositoryNotAllowed(ServiceError):
    status, title = 403, "Repository not allowed"


class RepositoryNotFound(ServiceError):
    status, title = 404, "Repository not found"


class UpstreamRateLimited(ServiceError):
    status, title = 503, "Upstream rate limit reached"


class UpstreamError(ServiceError):
    status, title = 502, "Upstream error"


class NarrativeUnavailable(ServiceError):
    status, title = 503, "Narrative unavailable"


class NarrativeFailed(ServiceError):
    status, title = 502, "Narrative generation failed"


def problem(
    status: int,
    title: str,
    detail: str,
    request: Request,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        **extra,
    }
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def handle_service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return problem(exc.status, exc.title, exc.detail, request, headers)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"location": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
        for err in exc.errors()
    ]
    return problem(422, "Invalid request", "Request parameters failed validation.", request,
                   errors=errors)


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return problem(exc.status_code, str(exc.detail), str(exc.detail), request)
