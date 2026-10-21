import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.bootstrap import run_startup_tasks
from app.config import settings
from app.database import Base, engine
from app.migrations import run_pending_column_migrations
from app.routes import (
    admin_auth,
    admin_listings,
    analytics,
    applicant,
    moderation,
    opportunities,
    scraper,
    subscribers,
)
from app.scheduler import shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    Base.metadata.create_all(bind=engine)
    run_pending_column_migrations(engine)
    if settings.ENABLE_SCHEDULER:
        start_scheduler()
    threading.Thread(target=run_startup_tasks, daemon=True).start()
    logger.info("Global Opportunities API is ready.")
    yield
    # ---- shutdown ----
    shutdown_scheduler()
    logger.info("Global Opportunities API stopped.")


app = FastAPI(
    title="Global Opportunities API",
    description=(
        "AI-powered web mining platform that discovers scholarships, "
        "fellowships, grants, and jobs from across the internet."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Credentialed CORS is invalid with a wildcard origin; only enable it
# when explicit origins are configured.
_cors_origins = settings.cors_origin_list()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Security headers (2026-08 audit: none were set anywhere) --------
# Applied to every response via middleware rather than per-route, so a
# new route can never ship without them by omission.
#
# The Content-Security-Policy is path-scoped because this app genuinely
# serves three different kinds of response:
#   - JSON API endpoints (/api/*, /health, /openapi.json) render no page
#     at all, so they get the strictest possible policy.
#   - /docs and /redoc are FastAPI's built-in Swagger UI / ReDoc pages.
#     Verified by actually curling both: Swagger UI loads its JS/CSS
#     from cdn.jsdelivr.net and runs an inline init `<script>`; ReDoc
#     loads its bundle from the same CDN plus a Google Font and injects
#     inline `<style>` at runtime (its CSS-in-JS). A strict policy blanks
#     both pages out, so they get a scoped, looser policy instead of
#     weakening the default for everything else.
#   - Everything else falls through to `frontend()` below, which only
#     serves the static site directly when this app is run without the
#     Cloudflare Worker in front of it (local dev, `docker compose`, or
#     a direct hit on the Render origin). Its policy mirrors
#     `frontend/_headers`, which is what actually applies in production
#     — Cloudflare serves those files at the edge without this app in
#     the loop at all.
_DOCS_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc"}
_API_EXACT_PATHS = {"/health", "/openapi.json", "/api"}
_API_PREFIX = "/api/"

# unsafe-inline is unavoidable here without vendoring/patching FastAPI's
# built-in docs HTML to add a nonce: Swagger's init script is generated
# fresh per request and ReDoc injects styles at runtime, so neither a
# static hash nor a nonce (no per-request templating happens for these
# stock responses) is workable. Scoped to just these two paths so it
# never leaks into the API or frontend policies below.
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

# JSON responses need nothing at all.
_API_CSP = "default-src 'none'; base-uri 'none'"

# Mirrors frontend/_headers — see the comment there for why each source
# is listed (Google Fonts, and a hash for index.html's inline JSON-LD
# block). Duplicated rather than shared because the Worker and this app
# are separate runtimes with no shared config to read from.
_FRONTEND_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'sha256-pDG7ywLQCTavmocE0AIF4eN7Dq/Ibx1SKkzQ6wMOiBg='; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
)


def _csp_for_path(path: str) -> str:
    if path in _DOCS_PATHS:
        return _DOCS_CSP
    if path in _API_EXACT_PATHS or path.startswith(_API_PREFIX):
        return _API_CSP
    return _FRONTEND_CSP


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # DENY instead of a CSP `frame-ancestors` directive — they express
    # the same "never frame this" rule and the task is to pick one, not
    # maintain both in lockstep.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _csp_for_path(request.url.path)
    # HSTS is only safe once the app is actually reachable over HTTPS.
    # SESSION_COOKIE_SECURE=false is the existing flag for "this is
    # plain-http local dev" (see app/config.py) — reused here rather
    # than adding a second flag, so local dev never gets an HSTS header
    # a plain-http server couldn't honour anyway.
    if settings.SESSION_COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
    return response

app.include_router(opportunities.router, prefix="/api/v1")
app.include_router(scraper.router, prefix="/api/v1")
app.include_router(subscribers.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(moderation.router, prefix="/api/v1")
app.include_router(admin_auth.router, prefix="/api/v1")
app.include_router(applicant.router, prefix="/api/v1")


@app.get("/health", tags=["System"])
def health():
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.warning("Health check DB probe failed: %s", exc)

    body = {
        "status": "healthy" if db_ok else "degraded",
        "service": "Global Opportunities",
        "database": "ok" if db_ok else "unavailable",
        "scheduler": settings.ENABLE_SCHEDULER,
    }
    return JSONResponse(content=body, status_code=200 if db_ok else 503)


@app.get("/api", tags=["System"])
async def root():
    return {
        "service": "Global Opportunities API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/{path:path}", include_in_schema=False)
async def frontend(path: str = ""):
    if path.startswith("api"):
        raise HTTPException(status_code=404, detail="Not found")

    if not FRONTEND_DIR.exists():
        return JSONResponse({"detail": "Frontend not available"}, status_code=404)

    # Resolve and confine to FRONTEND_DIR so encoded "../" segments
    # cannot escape the frontend folder.
    try:
        candidate = (FRONTEND_DIR / path).resolve()
        candidate.relative_to(FRONTEND_DIR.resolve())
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Not found") from None

    if candidate.is_file():
        return FileResponse(candidate)

    index = FRONTEND_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not found")
