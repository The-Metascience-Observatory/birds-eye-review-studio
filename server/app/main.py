"""FastAPI application factory."""
from __future__ import annotations

import logging
import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .config import apply_public_defaults, assert_public_posture, settings
from .ratelimit import limiter
from .db.session import init_models
from .routers import (
    admin,
    agent,
    auth,
    dashboard,
    drafts,
    evidence,
    jobs,
    llm_session,
    publish,
    review_config,
    reviews,
)
from .services import jobs as jobs_svc

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Skeleton path: create tables directly. Alembic is optional this phase and
    # deliberately NOT imported here, so it can never block startup/tests.
    await init_models()
    # Best-effort: warm the arq pool so enqueue is fast. If redis is down we log
    # and continue — non-engine endpoints must still work. Enqueue itself never
    # depends on app.state (it uses jobs_svc.get_arq_pool lazily), so tests that
    # skip lifespan (ASGITransport) are unaffected.
    app.state.arq_pool = None
    try:
        app.state.arq_pool = await jobs_svc.get_arq_pool()
    except Exception as exc:  # pragma: no cover - depends on live redis
        logger.warning("arq pool unavailable at startup (%s); continuing", exc)
    from .services import reconcile
    reconciler = asyncio.create_task(reconcile.run())
    try:
        yield
    finally:
        reconciler.cancel()
        with suppress(asyncio.CancelledError):
            await reconciler
        try:
            await jobs_svc.close_arq_pool()
        except Exception:  # pragma: no cover
            pass


def create_app() -> FastAPI:
    # Refuse a public process that still has the local-dev secrets, then fill
    # in rate limits / token TTL / CORS so forgetting those is not a hole.
    # Local-dev (PUBLIC_MODE=false) is a no-op, including the test suite.
    assert_public_posture(settings)
    apply_public_defaults(settings)

    # A public instance publishes no interactive docs and no OpenAPI schema:
    # they hand an anonymous visitor a complete, accurate map of every endpoint
    # and its request shape. Local dev keeps them — /docs is how you drive the
    # API by hand.
    doc_urls: dict = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None}
        if settings.PUBLIC_MODE
        else {}
    )
    app = FastAPI(
        title="Bird's Eye Review Studio API",
        version=settings.VERSION,
        lifespan=lifespan,
        **doc_urls,
    )

    # Per-IP limits on the auth endpoints (see app/ratelimit.py). Disabled
    # unless RATELIMIT_* is configured, so dev and the test suite are unaffected.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        # Dev convenience: also allow loopback/private-LAN origins on any port
        # (see CORS_ORIGIN_REGEX; set it to "" for strict production behavior).
        allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # An UNHANDLED exception propagates past CORSMiddleware to Starlette's outer
    # ServerErrorMiddleware, which returns a bare `500 Internal Server Error`
    # with no CORS headers at all. The browser then reports the response as a
    # CORS/network failure, and the studio's fetch wrapper says "Cannot reach
    # the API … is this origin in CORS_ORIGINS?" — sending the reader after a
    # configuration problem that does not exist while hiding the real traceback.
    #
    # Registering the handler here puts it on ExceptionMiddleware, which runs
    # INSIDE CORSMiddleware, so the response goes back out through CORS.
    # The exception TEXT, however, must not go to the client on a public
    # instance: exception strings routinely carry absolute filesystem paths, SQL
    # fragments and internal identifiers. Log the full traceback against a random
    # reference and hand the client only that reference, so a bug report is still
    # traceable to a specific log line without leaking internals to everyone.
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        ref = uuid4().hex[:12]
        logger.exception(
            "Unhandled error [ref=%s] on %s %s",
            ref,
            request.method,
            request.url.path,
        )
        if settings.PUBLIC_MODE:
            return JSONResponse(
                status_code=500,
                content={
                    "detail": (
                        "Internal server error. Quote reference "
                        f"{ref} when reporting this."
                    ),
                    "ref": ref,
                },
            )
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}", "ref": ref},
        )

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"status": "ok", "version": settings.VERSION}

    app.include_router(auth.router)
    app.include_router(reviews.router)
    app.include_router(evidence.router)
    app.include_router(dashboard.router)
    app.include_router(publish.router)
    app.include_router(review_config.router)
    app.include_router(drafts.router)
    app.include_router(jobs.router)
    app.include_router(agent.router)
    app.include_router(admin.router)
    app.include_router(llm_session.router)
    return app


app = create_app()
