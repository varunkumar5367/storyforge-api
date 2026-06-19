import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Set loop policy for Windows Postgres async connection
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT_DIR = Path("c:/Users/varun/OneDrive/Desktop/personal projects/yt/storyforge-api")
load_dotenv(dotenv_path=ROOT_DIR / ".env")
sys.path.append(str(ROOT_DIR))

import database
from services.orchestrator import _run_pipeline_impl
import time
import psutil

def get_memory_usage_mb() -> float:
    try:
        proc = psutil.Process()
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0

async def main():
    print("Initializing database...")
    await database.init_db()
    
    # Get user or create a temporary admin user
    user_id = "test-admin-id"
    async with database.DatabaseConnection(database.DATABASE_URL) as db:
        async with db.execute("SELECT id, username, role FROM users WHERE role = ?", ("admin",)) as cur:
            row = await cur.fetchone()
            if row:
                user_id = row["id"]
                username = row["username"]
                print(f"Using existing admin user: {username} (ID: {user_id})")
            else:
                from database import hash_password
                pw_hash = hash_password("adminpass")
                await db.execute(
                    "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, "admin_tester", pw_hash, "admin", "2026-06-17T23:30:00Z")
                )
                await db.commit()
                print("Created temporary admin user: admin_tester")
    
    # Read the story file
    story_path = ROOT_DIR / "story_1000.txt"
    if not story_path.exists():
        print(f"Error: story_1000.txt not found at {story_path}")
        return
        
    with open(story_path, "r", encoding="utf-8") as f:
        story_text = f.read()
        
    job_id = "e2e-test-" + str(int(time.time()))
    print(f"Creating job {job_id}...")
    
    # Insert job into database
    await database.create_job(
        job_id=job_id,
        story_text=story_text,
        story_filename="story_1000.txt",
        voice="en-US-JennyNeural",
        user_id=user_id
    )
    
    print(f"Initial Memory Usage: {get_memory_usage_mb():.2f} MB")
    print("Starting e2e pipeline run...")
    start_time = time.time()
    
    # Run the pipeline
    await _run_pipeline_impl(job_id, story_text)
    
    end_time = time.time()
    elapsed = end_time - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    
    print("\n" + "="*50)
    print("E2E PIPELINE RUN COMPLETED")
    print(f"Time Taken: {minutes} minutes and {seconds} seconds ({elapsed:.2f} seconds)")
    print(f"Final Memory Usage: {get_memory_usage_mb():.2f} MB")
    
    # Fetch job status from DB
    job = await database.get_job(job_id)
    if job:
        print(f"Status: {job.get('status')}")
        print(f"Progress: {job.get('progress_percent')}%")
        print(f"Error Message: {job.get('error_message')}")
        print(f"Download URLs: {job.get('download_urls')}")
    else:
        print("Job not found in database!")
    
    # Check if the output video actually exists
    video_path = Path(ROOT_DIR) / "output" / job_id / "final" / "episode.mp4"
    if video_path.exists():
        print(f"SUCCESS: Video successfully created at {video_path}")
        print(f"Video size: {video_path.stat().st_size / (1024*1024):.2f} MB")
    else:
        print(f"FAILURE: Video not found at {video_path}")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(main())
