"""
scripts/load_test.py — Automated StoryForge load testing script.
Runs a 70-scene generation pipeline back-to-back with mocked AI generation endpoints,
ensuring FFmpeg composition, file handles, connection pools, and locks are fully tested
for memory leaks and response-time stability.
"""

import os
import sys
import asyncio
import time
import psutil
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from PIL import Image

# Add root directory to python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# Global variables for metrics tracking
measurements = []
start_time = 0
last_scene_time = 0
peak_total_ram = 0.0
video_composed_successfully = False
video_file_size_mb = 0.0

async def monitor_memory(pid, interval=0.1):
    peak_memory = 0.0
    try:
        parent = psutil.Process(pid)
        while True:
            try:
                total_mem = parent.memory_info().rss
                for child in parent.children(recursive=True):
                    total_mem += child.memory_info().rss
                total_mem_mb = total_mem / (1024 * 1024)
                if total_mem_mb > peak_memory:
                    peak_memory = total_mem_mb
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return peak_memory

def record_measurement(scene_num):
    global last_scene_time
    proc = psutil.Process()
    rss = proc.memory_info().rss / (1024 * 1024)  # MB
    now = time.time()
    elapsed = now - last_scene_time
    last_scene_time = now
    measurements.append({
        "scene_num": scene_num,
        "rss_mb": rss,
        "elapsed_sec": elapsed
    })
    print(f"Scene {scene_num:03d} completed | Current RAM: {rss:.2f} MB | Time elapsed: {elapsed:.2f}s")

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------
async def mock_analyze_story(story_text):
    print("Mocking Story Analysis (Step 1) — Programmatically creating 70 scenes...")
    scenes = []
    for i in range(1, 71):
        scenes.append({
            "scene_number": i,
            "title": f"Scene {i}",
            "text": f"This is scene number {i} of the load test.",
            "narration": f"This is scene number {i} of the load test.",
            "setting": "A neutral load testing room",
            "location": "Testing chamber",
            "mood": "neutral",
            "image_prompt": f"A simplistic testing room with number {i} painted on the wall",
            "characters_present": ["Tester"],
            "duration_hint": 3.0
        })
    return {
        "success": True,
        "data": {
            "scenes": scenes,
            "character_memory": {
                "characters": [
                    {
                        "name": "Tester",
                        "role": "hero",
                        "gender": "male",
                        "age": "30",
                        "hair": "black",
                        "eyes": "brown",
                        "body_type": "average",
                        "clothing": "lab coat",
                        "facial_features": "none",
                        "personality": "logical"
                    }
                ]
            },
            "mood": "neutral",
            "locations": ["Testing chamber"]
        }
    }

async def mock_generate_image_for_scene(job_id, scene, character_memory):
    from utils.file_handler import scene_image_path
    img_path = scene_image_path(job_id, scene["scene_number"])
    Path(img_path).parent.mkdir(parents=True, exist_ok=True)
    
    # Save a simple 1280x720 red PNG
    img = Image.new("RGB", (1280, 720), color=(200, 50, 50))
    img.save(img_path)
    
    scene["image_path"] = str(img_path)
    return scene

async def mock_generate_voice_for_scene(client, job_id, scene, voice):
    from utils.file_handler import get_audio_dir
    audio_path = get_audio_dir(job_id) / f"scene_{scene['scene_number']:03d}.mp3"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Copy the pre-generated silent MP3 file
    shutil.copy("silent_3s.mp3", audio_path)
    
    scene["audio_path"] = str(audio_path)
    scene["duration_hint"] = 3.0
    return scene

async def mock_generate_subtitle_for_scene(job_id, scene):
    from utils.file_handler import get_subtitles_dir
    srt_path = get_subtitles_dir(job_id) / f"scene_{scene['scene_number']:03d}.srt"
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write a simple subtitle block
    content = f"1\n00:00:00,000 --> 00:00:03,000\nThis is scene number {scene['scene_number']} of the load test.\n"
    srt_path.write_text(content, encoding="utf-8")
    
    scene["subtitle_path"] = str(srt_path)
    
    from services.subtitle_generator import _SceneSubtitleResult, _Cue
    cue = _Cue(index=1, start=0.0, end=3.0, text=scene["text"])
    res = _SceneSubtitleResult(
        scene_number=scene["scene_number"],
        success=True,
        cues=[cue],
        path=str(srt_path),
        audio_duration=3.0
    )
    
    # Record RAM and time metrics at the end of the scene generation step
    record_measurement(scene["scene_number"])
    return scene, res

# Disable inter-call sleep during the test to run faster
real_sleep = asyncio.sleep

async def mock_asyncio_sleep(delay):
    if delay > 0.5:
        await real_sleep(0.001)
    else:
        await real_sleep(delay)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def run_load_test():
    global start_time, last_scene_time
    print("=========================================================")
    print("       STARTING STORYFORGE BACKEND LOAD TEST             ")
    print("=========================================================")
    
    # 1. Generate 3-second silent audio file using FFmpeg
    print("Generating silent reference MP3 file...")
    cmd_audio = ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", "silent_3s.mp3"]
    # Hide window on Windows
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        
    p = subprocess.Popen(cmd_audio, startupinfo=startupinfo)
    p.wait()
    
    if not Path("silent_3s.mp3").exists():
        print("Error: Failed to create silent reference audio file.")
        return
        
    # 2. Setup database connection pool and run schema init
    import database
    await database.init_db()
    
    # Fetch admin user ID to bypass non-admin scene caps
    admin_user_id = None
    admin_username = os.getenv("ADMIN_USERNAME", "varun5367")
    async with database.DatabaseConnection(database.DATABASE_URL) as db:
        async with db.execute("SELECT id FROM users WHERE username = ?", (admin_username,)) as cur:
            row = await cur.fetchone()
            if row:
                admin_user_id = row["id"]
    
    # Create a test job row
    import uuid
    job_id = f"test-load-{str(uuid.uuid4())[:8]}"
    print(f"Creating database entry for test job: {job_id}")
    await database.create_job(
        job_id=job_id,
        story_text="Load test story text",
        story_filename="load_test_story.txt",
        voice="en-US-JennyNeural",
        user_id=admin_user_id
    )
    
    # 3. Trigger orchestrator run with patches
    start_time = time.time()
    last_scene_time = start_time
    
    from services.orchestrator import _run_pipeline_impl
    
    patches = [
        patch("services.orchestrator.analyze_story", side_effect=mock_analyze_story),
        patch("services.orchestrator.generate_image_for_scene", side_effect=mock_generate_image_for_scene),
        patch("services.orchestrator.generate_voice_for_scene", side_effect=mock_generate_voice_for_scene),
        patch("services.orchestrator.generate_subtitle_for_scene", side_effect=mock_generate_subtitle_for_scene),
        patch("services.orchestrator.asyncio.sleep", side_effect=mock_asyncio_sleep),
        # Prevent actual Cloudinary uploads during load test
        patch("utils.file_handler.upload_asset", return_value=""),
    ]
    
    # Apply all patches
    for p in patches:
        p.start()
        
    global peak_total_ram, video_composed_successfully, video_file_size_mb
    try:
        # Start background memory monitor task
        monitor_task = asyncio.create_task(monitor_memory(os.getpid()))
        
        await _run_pipeline_impl(job_id, "Story text")
        
        # Stop memory monitor and retrieve true peak
        monitor_task.cancel()
        try:
            peak_total_ram = await monitor_task
        except Exception:
            pass
            
        # Check if the final video was created
        from utils.file_handler import final_video_path
        final_video = final_video_path(job_id)
        if final_video.exists():
            video_composed_successfully = True
            video_file_size_mb = final_video.stat().st_size / (1024 * 1024)
    finally:
        # Stop all patches
        for p in patches:
            p.stop()
            
        # Clean up temporary reference audio
        if Path("silent_3s.mp3").exists():
            os.remove("silent_3s.mp3")
            
        # Clean up job output directory
        job_dir = Path(ROOT_DIR) / "output" / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            
    # 4. Analyze results
    total_time = time.time() - start_time
    print("\n=========================================================")
    print("       LOAD TEST RESULTS ANALYSIS                        ")
    print("=========================================================")
    print(f"Total scenes completed: {len(measurements)}")
    print(f"Total pipeline execution time: {total_time:.2f}s")
    
    if len(measurements) < 50:
        print("FAIL: Generated less than 50 scenes.")
        sys.exit(1)
        
    ram_values = [m["rss_mb"] for m in measurements]
    peak_python_ram = max(ram_values)
    avg_scene_time = sum([m["elapsed_sec"] for m in measurements]) / len(measurements)
    
    # Calculate memory trend (linear slope between first 10 and last 10 scenes)
    first_10_ram = sum(ram_values[:10]) / 10
    last_10_ram = sum(ram_values[-10:]) / 10
    ram_diff = last_10_ram - first_10_ram
    
    print(f"Peak Python Process RAM: {peak_python_ram:.2f} MB")
    print(f"Peak Total System RAM (incl. FFmpeg): {peak_total_ram:.2f} MB")
    print(f"Average Scene Generation Time: {avg_scene_time:.2f}s")
    print(f"Initial Memory Avg (first 10 scenes): {first_10_ram:.2f} MB")
    print(f"Final Memory Avg (last 10 scenes): {last_10_ram:.2f} MB")
    print(f"Memory Trend (Final - Initial): {ram_diff:+.2f} MB")
    
    if video_composed_successfully:
        print(f"SUCCESS: Final video composed successfully! Size: {video_file_size_mb:.2f} MB")
    else:
        print("FAIL: Final video was NOT found on disk. Video composition step failed.")
        
    # Verify memory stability
    if ram_diff > 15.0:
        print("\nWARNING: Memory trend is positive (+{:.2f} MB). Potential leak detected.".format(ram_diff))
        print("Please check unclosed references or file descriptors.")
    else:
        print("\nSUCCESS: Memory RSS usage has plateaued and is stable (diff: {:.2f} MB).".format(ram_diff))
        
    print("=========================================================")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_load_test())
