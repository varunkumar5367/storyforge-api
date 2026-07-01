"""
scratch/trigger_test_job.py — Create and queue a short test job in the database.
Allows testing the laptop listener + full local GPU pipeline end-to-end.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Set up paths so we can import database
sys.path.append(str(Path(__file__).resolve().parent.parent))

from database import DatabaseConnection, DATABASE_URL

STORY_TEXT = """[STORY CONFIGURATION
Title: "Martian Sunrise"
Tone & Mood: "Sci-Fi Epic"
Visual Style: "Anime / Manga"
Aspect Ratio: "Vertical 9:16"
Subtitles style: "neon"
]

Commander Vance stood on the edge of the Martian valley, his visor reflecting the crimson sunrise.

He picked up a strange key, and a blue holographic map blossomed into the dusty air.
"""

async def main():
    print(f"Connecting to database: {DATABASE_URL}")
    async with DatabaseConnection(DATABASE_URL) as db:
        # 1. Ensure we have at least one user in the database
        async with db.execute("SELECT id, username FROM users LIMIT 1") as cur:
            user = await cur.fetchone()
            if not user:
                print("No user found in database. Creating a default test user...")
                user_id = str(uuid.uuid4())
                username = "test_user"
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, username, "dummy_hash", "admin", now)
                )
                await db.commit()
            else:
                user_id = user["id"]
                username = user["username"]
                
        print(f"Using user: {username} (ID: {user_id})")
        
        # 2. Insert the test job
        job_id = "test_job_" + str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        
        print(f"Queueing job: {job_id}...")
        # Since AUTO_APPROVE is enabled, the listener will auto-approve and run it immediately!
        await db.execute(
            """
            INSERT INTO jobs (id, status, progress_percent, current_step, story_text, story_filename, created_at, voice, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, "pending_approval", 0, "pending_approval", STORY_TEXT, "test_martian_sunrise.txt", now, "en-US-JennyNeural", user_id)
        )
        await db.commit()
        print(f"Job successfully inserted! Check the laptop listener GUI / log.")
        
        # 3. Monitor the job status
        print("\nMonitoring job progress (Ctrl+C to stop)...")
        last_step = None
        last_progress = -1
        
        while True:
            await asyncio.sleep(2)
            async with db.execute("SELECT status, progress_percent, current_step, error_message FROM jobs WHERE id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    print("Job not found in database!")
                    break
                    
                status = row["status"]
                progress = row["progress_percent"]
                step = row["current_step"]
                err = row["error_message"]
                
                if step != last_step or progress != last_progress:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Status: {status} | Step: {step} | Progress: {progress}%")
                    last_step = step
                    last_progress = progress
                    
                if status == "completed":
                    print("\n🎉 End-to-end local generation SUCCESS! Job completed.")
                    break
                elif status == "failed":
                    print(f"\n❌ Local generation FAILED: {err}")
                    break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")
