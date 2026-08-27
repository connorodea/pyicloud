"""FastAPI application for the unified booking page."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from calendar_hub.config import Settings, get_settings
from calendar_hub.connectors import ConnectorError
from calendar_hub.models import (
    AvailabilityResponse,
    BookingRequest,
    BookingResponse,
    EventType,
)
from calendar_hub.services import (
    BookingInfrastructureError,
    CalendarHubService,
    RateLimitError,
    SlotUnavailableError,
    VerificationError,
    serialize_service_error,
)

PACKAGE_ROOT = Path(__file__).resolve().parent


def create_app(
    settings: Settings | None = None,
    service: CalendarHubService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    service = service or CalendarHubService(settings)
    app = FastAPI(
        title="Calendar Hub",
        version="0.1.0",
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
    )
    app.state.settings = settings
    app.state.calendar_service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.host_allowlist)
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_ROOT / "static"),
        name="static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers[
            "Permissions-Policy"
        ] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
            "style-src 'self'; frame-src https://challenges.cloudflare.com; "
            "connect-src 'self' https://challenges.cloudflare.com; img-src 'self' data:; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            PACKAGE_ROOT / "templates" / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nAllow: /\n")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/config")
    def public_config() -> dict[str, object]:
        return {
            "owner_name": settings.page_owner_name,
            "owner_timezone": settings.owner_timezone,
            "turnstile_site_key": settings.turnstile_site_key,
            "event_types": [
                item.model_dump(mode="json") for item in service.event_types()
            ],
        }

    @app.get("/api/v1/availability", response_model=AvailabilityResponse)
    def availability(
        response: Response,
        event_type: EventType,
        from_date: date,
        days: int = Query(default=14, ge=1, le=31),
        timezone: str = Query(default="UTC", min_length=1, max_length=100),
    ) -> AvailabilityResponse:
        response.headers["Cache-Control"] = "private, no-store"
        try:
            return service.availability(
                event_type=event_type,
                first_day=from_date,
                days=days,
                visitor_timezone=timezone,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ConnectorError, BookingInfrastructureError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=serialize_service_error(exc),
            ) from exc

    @app.post(
        "/api/v1/bookings",
        response_model=BookingResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_booking(request: Request, payload: BookingRequest) -> BookingResponse:
        remote_ip = _client_ip(request, settings)
        try:
            service.enforce_rate_limit(remote_ip)
            service.verify_turnstile(payload.turnstile_token, remote_ip)
            return service.book(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except VerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RateLimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except SlotUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ConnectorError, BookingInfrastructureError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=serialize_service_error(exc),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=serialize_service_error(exc),
            ) from exc

    @app.exception_handler(404)
    async def not_found(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    return app


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cloudflare_ip:
            return cloudflare_ip
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


app = create_app()
