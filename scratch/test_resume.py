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

async def main():
    print("Initializing database...")
    await database.init_db()
    
    # We will resume the previous job e2e-short-test-1781770862
    job_id = "e2e-short-test-1781770862"
    
    # Fetch existing job to get the story text
    job = await database.get_job(job_id)
    if not job:
        print(f"Error: Job {job_id} not found in database!")
        return
        
    story_text = job.get("story_text")
    print(f"Resuming job {job_id}...")
    
    # Reset job status to pending/queued in database first (as the API does)
    await database.update_job(job_id, status="queued", progress_percent=0, error_message=None)
    
    start_time = time.time()
    
    # Run the pipeline again on the same job
    await _run_pipeline_impl(job_id, story_text)
    
    end_time = time.time()
    elapsed = end_time - start_time
    print(f"\nResumed run completed in {elapsed:.2f} seconds.")
    
    # Fetch job status from DB to verify success
    updated_job = await database.get_job(job_id)
    print(f"Status: {updated_job.get('status')}")
    print(f"Progress: {updated_job.get('progress_percent')}%")
    print(f"Error Message: {updated_job.get('error_message')}")
    
    # Verify video file exists and check size
    video_path = Path(ROOT_DIR) / "output" / job_id / "final" / "episode.mp4"
    if video_path.exists():
        print(f"SUCCESS: Video exists at {video_path}")
        print(f"Video size: {video_path.stat().st_size / (1024*1024):.2f} MB")
    else:
        print(f"FAILURE: Video not found at {video_path}")

if __name__ == "__main__":
    asyncio.run(main())
