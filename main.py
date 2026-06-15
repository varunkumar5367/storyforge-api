"""
StoryForge AI — Main FastAPI Application Entry Point
Starts the server, registers all routes, and initializes the database.
"""

import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

if sys.platform == "win32":
    load_dotenv()
    db_url = os.getenv("DATABASE_URL", "./storyforge.db")
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from database import init_db
from routes.analyze import router as analyze_router
from routes.generate import router as generate_router
from routes.status import router as status_router
from routes.download import router as download_router
from routes.auth import router as auth_router
from routes.admin import router as admin_router

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("storyforge")

# Add file handler for server logs
file_handler = logging.FileHandler("storyforge.log", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))
logging.getLogger().addHandler(file_handler)
logging.getLogger("uvicorn").addHandler(file_handler)
logging.getLogger("uvicorn.access").addHandler(file_handler)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")


# ---------------------------------------------------------------------------
# Lifespan – runs on startup/shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup and clean up on shutdown."""
    logger.info("🚀 StoryForge API starting up …")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    await init_db()
    logger.info("✅ Database initialized.")
    
    # Start background cleanup loop
    from services.orchestrator import cleanup_old_jobs_loop
    cleanup_task = asyncio.create_task(cleanup_old_jobs_loop())
    app.state.cleanup_task = cleanup_task
    
    yield
    
    # Cancel background cleanup loop on shutdown
    if hasattr(app.state, "cleanup_task"):
        logger.info("Cancelling background cleanup task...")
        app.state.cleanup_task.cancel()
        try:
            await app.state.cleanup_task
        except asyncio.CancelledError:
            pass
    logger.info("👋 StoryForge API shutting down.")


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

# CORS – allow the Next.js frontend (and local dev) to hit the API
origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
if frontend_url:
    origins.append(frontend_url)

# Only allow vercel previews in non-production environments
if os.getenv("ENV") != "production":
    origins.append("https://*.vercel.app")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve generated output files as static assets (download links)
os.makedirs(OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(analyze_router,  prefix="/api/analyze",  tags=["Analyze"])
app.include_router(generate_router, prefix="/api/generate", tags=["Generate"])
app.include_router(status_router,   prefix="/api/status",   tags=["Status"])
app.include_router(download_router, prefix="/api/download", tags=["Download"])
app.include_router(auth_router,     prefix="/api/auth",     tags=["Auth"])
app.include_router(admin_router,    prefix="/api/admin",    tags=["Admin"])


# ---------------------------------------------------------------------------
# Health-check
# ---------------------------------------------------------------------------
@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "StoryForge AI", "version": "1.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "healthy"}


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
        reload_excludes=["*.db", "output/*", "storyforge.db"],
    )
