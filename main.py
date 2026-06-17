"""
StoryForge AI — Main FastAPI Application Entry Point
Starts the server, registers all routes, and initializes the database.
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

if sys.platform == "win32":
    from dotenv import load_dotenv

    load_dotenv()
    db_url = os.getenv("DATABASE_URL", "./storyforge.db")
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from config import settings
from database import init_db
from routes.admin import router as admin_router
from routes.analyze import router as analyze_router
from routes.auth import router as auth_router
from routes.download import router as download_router
from routes.generate import router as generate_router
from routes.status import router as status_router

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("storyforge")

file_handler = logging.FileHandler("storyforge.log", encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
)
logging.getLogger().addHandler(file_handler)
logging.getLogger("uvicorn").addHandler(file_handler)
logging.getLogger("uvicorn.access").addHandler(file_handler)

OUTPUT_DIR = settings.output_dir


async def server_stats_ping_loop():
    """Periodically ping the database with live CPU, RAM, active tasks, and active users metrics."""
    import psutil
    from datetime import datetime, timezone
    from database import update_server_status
    
    logger.info("Starting background server stats ping loop...")
    while True:
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
            
            # Count active pipeline tasks
            active_tasks = 0
            for task in asyncio.all_tasks():
                name = task.get_name()
                if name and name.startswith("pipeline-"):
                    active_tasks += 1
            
            now_str = datetime.now(timezone.utc).isoformat()
            await update_server_status(
                cpu_usage=cpu,
                ram_usage=ram,
                active_tasks=active_tasks,
                last_ping=now_str
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Failed to update server stats: %s", e)
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Lifespan – runs on startup/shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup and clean up on shutdown."""
    logger.info("StoryForge API starting up …")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    from utils.monitoring import run_startup_diagnostics

    app.state.startup_diagnostics = run_startup_diagnostics()

    await init_db()
    logger.info("Database initialized.")

    from services.orchestrator import cleanup_old_jobs_loop

    cleanup_task = asyncio.create_task(cleanup_old_jobs_loop())
    app.state.cleanup_task = cleanup_task

    ping_task = asyncio.create_task(server_stats_ping_loop())
    app.state.ping_task = ping_task

    yield

    if hasattr(app.state, "cleanup_task"):
        logger.info("Cancelling background cleanup task...")
        app.state.cleanup_task.cancel()
        try:
            await app.state.cleanup_task
        except asyncio.CancelledError:
            pass

    if hasattr(app.state, "ping_task"):
        logger.info("Cancelling background server stats ping task...")
        app.state.ping_task.cancel()
        try:
            await app.state.ping_task
        except asyncio.CancelledError:
            pass

    from database import close_db
    await close_db()

    logger.info("StoryForge API shutting down.")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="StoryForge AI",
    description=(
        "Converts a written story (.txt) into a complete YouTube-ready "
        "video automatically using LLM, generative images, TTS, and FFmpeg."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Trust X-Forwarded-* headers from Cloudflare Tunnel / reverse proxy
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# CORS — origins from FRONTEND_URL configuration only (+ localhost in dev)
cors_kwargs: dict = {
    "allow_origins": settings.cors_origins,
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}
if settings.cors_origin_regex:
    cors_kwargs["allow_origin_regex"] = settings.cors_origin_regex

app.add_middleware(CORSMiddleware, **cors_kwargs)

os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(analyze_router, prefix="/api/analyze", tags=["Analyze"])
app.include_router(generate_router, prefix="/api/generate", tags=["Generate"])
app.include_router(status_router, prefix="/api/status", tags=["Status"])
app.include_router(download_router, prefix="/api/download", tags=["Download"])
app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(admin_router, prefix="/api/admin", tags=["Admin"])


# ---------------------------------------------------------------------------
# Health-check
# ---------------------------------------------------------------------------
@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "StoryForge AI", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    from utils.monitoring import get_health_payload

    return get_health_payload()


@app.get("/health/diagnostics", tags=["Health"])
async def health_diagnostics():
    """Full startup-style diagnostics — disk, RAM, FFmpeg, config summary."""
    from utils.monitoring import run_startup_diagnostics

    return run_startup_diagnostics()


# ---------------------------------------------------------------------------
# Dev entrypoint — on Windows + PostgreSQL use: python run_server.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.platform == "win32":
        load_dotenv()
        db_url = os.getenv("DATABASE_URL", "./storyforge.db")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
        reload_excludes=["*.db", "output/*", "storyforge.db"],
    )
