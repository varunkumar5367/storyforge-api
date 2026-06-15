import sys
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
from database import init_db, DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_db")

async def test_init():
    logger.info(f"Testing database initialization on URL: {DATABASE_URL}")
    try:
        await init_db()
        logger.info("Database schema initialized successfully!")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(test_init())
