"""FastAPI application factory."""
from __future__ import annotations

import logging
from hmac import compare_digest
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if get_settings().jobs_enabled and not getattr(app.state, "testing", False):
        from app.jobs import start_scheduler
        scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


def create_app(testing: bool = False) -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="Proof-First Outreach", lifespan=lifespan)
    app.state.testing = testing
    behind_sites = bool(cfg.sites_origin_token)
    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret,
                       session_cookie="proof_first_session" if behind_sites else "session",
                       same_site="lax" if behind_sites else "strict", https_only=behind_sites,
                       max_age=60 * 60 * 12)

    if behind_sites:
        @app.middleware("http")
        async def require_site_gateway(request: Request, call_next):
            if request.method == "GET" and request.url.path == "/livez":
                return Response(status_code=204)
            supplied = request.headers.get("x-proof-first-origin-token", "")
            if not compare_digest(supplied.encode(), cfg.sites_origin_token.encode()):
                return Response(status_code=404)
            return await call_next(request)

    from pathlib import Path
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "ui" / "static")), name="static")

    from app.api import routes_system
    app.include_router(routes_system.router)
    for mod in _optional_routers():
        app.include_router(mod.router)

    @app.exception_handler(303)
    async def _redirect(_: Request, exc):  # noqa: ANN001
        return RedirectResponse(exc.headers["Location"], status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "send_enabled": cfg.send_enabled}

    return app


def _optional_routers():
    import importlib
    names = ["app.api.routes_leads", "app.api.routes_messages", "app.api.routes_integrations",
             "app.api.routes_meta", "app.ui.routes"]
    out = []
    for n in names:
        try:
            out.append(importlib.import_module(n))
        except ModuleNotFoundError as e:
            if e.name != n:
                raise
    return out


app = create_app()

