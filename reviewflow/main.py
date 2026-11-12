import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from reviewflow.api import Services, health, router
from reviewflow.config import Settings, get_settings
from reviewflow.errors import (
    ServiceError,
    handle_http_error,
    handle_service_error,
    handle_validation_error,
    problem,
)
from reviewflow.github import GitHubClient, create_http_client
from reviewflow.logs import configure_logging, request_id_var
from reviewflow.narrative.llm import LLM, create_llm
from reviewflow.narrative.service import NarrativeService
from reviewflow.store import Store
from reviewflow.sync import SyncCoordinator

log = logging.getLogger("reviewflow")

STATIC_DIR = Path(__file__).parent / "static"
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(
    settings: Settings | None = None,
    *,
    github_http: httpx.AsyncClient | None = None,
    llm: LLM | None = None,
) -> FastAPI:
    """App factory. Tests inject settings, a mocked GitHub HTTP client and a fake LLM."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = await Store.open(settings.database_path)
        http = github_http or create_http_client(settings.github_token)
        github = GitHubClient(http, page_size=settings.github_page_size,
                              min_rate_budget=settings.github_min_rate_budget)
        sync = SyncCoordinator(store, github, ttl=timedelta(seconds=settings.sync_ttl_seconds))
        model = llm or create_llm(settings)
        app.state.services = Services(settings, store, sync, NarrativeService(model, store))
        log.info("started", extra={"fields": {
            "llm_model": model.model if model else None,
            "database": str(settings.database_path),
        }})
        try:
            yield
        finally:
            await http.aclose()
            await store.close()

    app = FastAPI(
        title="Review Flow Insights",
        version="0.1.0",
        description="Code-review flow metrics for GitHub repositories, with an LLM narrative "
                    "whose every number is verified against the computed metrics.",
        lifespan=lifespan,
    )
    app.add_exception_handler(ServiceError, handle_service_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.include_router(router)
    app.include_router(health)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception:
                log.exception("unhandled error")
                response = problem(500, "Internal Server Error",
                                   "An unexpected error occurred.", request)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path == "/" or request.url.path.startswith("/static/"):
                response.headers["Content-Security-Policy"] = _UI_CSP
            log.info("request", extra={"fields": {
                "method": request.method, "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }})
            return response
        finally:
            request_id_var.reset(token)

    return app
