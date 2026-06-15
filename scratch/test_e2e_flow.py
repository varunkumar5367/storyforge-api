# scratch/test_e2e_flow.py
import asyncio
import httpx
import sys
import time
from pathlib import Path

BASE_URL = "http://localhost:8000"

async def run_e2e():
    print("=== STARTING END-TO-END FLOW VERIFICATION ===")
    
    # 1. Check health endpoint
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            print(f"Pinging health check at {BASE_URL}/health ...")
            resp = await client.get(f"{BASE_URL}/health")
            print(f"Health Response: Status {resp.status_code} | {resp.json()}")
            if resp.status_code != 200:
                print("Error: health endpoint not healthy!")
                sys.exit(1)
        except Exception as e:
            print(f"Failed to connect to backend server: {e}")
            print("Make sure the FastAPI server is running on port 8000.")
            print("To start it, run: uvicorn main:app --reload --port 8000")
            sys.exit(1)

        # 2. Upload a small test story
        test_story_text = (
            "A mysterious light appeared in the dark forest. "
            "Leo approached the glowing object with caution. "
            "Suddenly, a voice echoed in his mind, welcoming him to the future."
        )
        
        print("\nUploading a short story to /api/analyze/upload ...")
        files = {
            "file": ("e2e_test_story.txt", test_story_text.encode("utf-8"), "text/plain")
        }
        data = {
            "voice": "en-US-JennyNeural"
        }
        
        try:
            resp = await client.post(f"{BASE_URL}/api/analyze/upload", files=files, data=data)
            print(f"Upload Response: Status {resp.status_code}")
            if resp.status_code != 200:
                print(f"Upload failed: {resp.text}")
                sys.exit(1)
            
            res_data = resp.json()
            job_id = res_data.get("job_id")
            print(f"Job successfully created! Job ID: {job_id}")
            
        except Exception as e:
            print(f"Failed to upload story: {e}")
            sys.exit(1)
            
        # 3. Poll job status
        print(f"\nPolling job status for {job_id}...")
        start_time = time.time()
        max_duration = 300  # 5 minutes max
        
        while time.time() - start_time < max_duration:
            await asyncio.sleep(5)
            try:
                status_resp = await client.get(f"{BASE_URL}/api/status/{job_id}")
                if status_resp.status_code != 200:
                    print(f"Failed to get status (HTTP {status_resp.status_code}): {status_resp.text}")
                    continue
                
                status_data = status_resp.json()
                status = status_data.get("status")
                progress = status_data.get("progress_percent")
                current_step = status_data.get("current_step")
                
                elapsed = int(time.time() - start_time)
                print(f"[{elapsed}s] Status: {status} | Progress: {progress}% | Current Step: {current_step}")
                
                if status == "completed":
                    print("\n[SUCCESS] E2E JOB COMPLETED SUCCESSFULLY!")
                    print(f"Scenes count: {len(status_data.get('scenes', []))}")
                    print(f"Logs: {status_data.get('logs')}")
                    
                    # 4. Check download endpoint
                    dl_resp = await client.get(f"{BASE_URL}/api/download/{job_id}")
                    print(f"Download response (Status {dl_resp.status_code}): {dl_resp.json() if dl_resp.status_code == 200 else dl_resp.text}")
                    break
                elif status == "failed":
                    print(f"\n[FAILURE] E2E JOB FAILED: {status_data.get('error_message')}")
                    print(f"Logs: {status_data.get('logs')}")
                    sys.exit(1)
            except Exception as e:
                print(f"Error checking status: {e}")
                
        else:
            print("\n[TIMEOUT] Job did not complete within 5 minutes.")
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_e2e())
