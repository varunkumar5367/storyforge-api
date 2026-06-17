"""
run_server.py — Uvicorn entrypoint with correct Windows event loop for PostgreSQL.

On Windows, psycopg async requires SelectorEventLoop (not ProactorEventLoop).
Use this instead of calling uvicorn directly:

    python run_server.py
    python run_server.py --port 8000
"""

from __future__ import annotations

import asyncio
import os
import sys

if sys.platform == "win32":
    from dotenv import load_dotenv

    load_dotenv()
    db_url = os.getenv("DATABASE_URL", "./storyforge.db")
    is_postgres = db_url.startswith("postgresql://") or db_url.startswith("postgres://")
    if is_postgres:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
    import uvicorn

    from config import settings

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
    )
