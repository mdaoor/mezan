from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mizan import __version__
from mizan.config import Settings
from mizan.core.container import Container, build_container, init_storage
from mizan.core.errors import AppError
from mizan.core.jobs import Worker

log = logging.getLogger("mizan.api")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    c = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_storage(c)
        async with c.session() as db:
            from mizan.core.services.loadtest import seed_presets
            await seed_presets(db)
            await db.commit()
        await c.hub.start()
        worker = None
        if c.settings.embedded_worker:
            worker = Worker(c, slots=c.settings.worker_slots)
            worker.start()
        yield
        if worker:
            await worker.stop()
        await c.hub.stop()
        await c.engine.dispose()

    app = FastAPI(title="Mizan", version=__version__, lifespan=lifespan, openapi_url="/api/v1/openapi.json",
                  docs_url="/api/v1/docs", description="Collaborative voice-model evaluation workbench")
    app.state.c = c
    if c.settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=c.settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(AppError)
    async def app_error(_: Request, e: AppError):
        return JSONResponse(jsonable_encoder({"error": {"code": e.code, "message": e.message, **e.extra}}), status_code=e.status)

    from mizan.api.routers import annotation, auth, datasets, loadtests, registry, reports, runs, system, team, ws
    for mod in (auth, system, datasets, annotation, registry, runs, loadtests, reports, team):
        app.include_router(mod.router, prefix="/api/v1")
    app.include_router(ws.router, prefix="/api/v1")
    app.include_router(ws.router)  # spec also names plain /ws

    web = Path(c.settings.web_dir)
    if web.is_dir() and (web / "index.html").exists():
        if (web / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = (web / path).resolve()
            if path and f.is_file() and web.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(web / "index.html")
    return app


def app_factory() -> FastAPI:  # uvicorn --factory mizan.api.app:app_factory
    return create_app()
