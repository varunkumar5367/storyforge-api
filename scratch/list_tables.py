import asyncio
import os
import sys
import psycopg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

async def main():
    db_url = os.getenv("DATABASE_URL")
    print(f"Connecting to: {db_url}")
    conn = await psycopg.AsyncConnection.connect(db_url)
    async with conn.cursor() as cur:
        await cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        tables = await cur.fetchall()
        print("Tables found:")
        for t in tables:
            print(f" - {t[0]}")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
