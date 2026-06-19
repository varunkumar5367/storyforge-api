import sys
import asyncio
import os
import re
import time
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT_DIR = Path("c:/Users/varun/OneDrive/Desktop/personal projects/yt/storyforge-api")
load_dotenv(dotenv_path=ROOT_DIR / ".env")
sys.path.append(str(ROOT_DIR))

import database

async def main():
    print("Starting Cloudflare Tunnel...")
    cmd_cf = ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"]
    
    # Hide window on Windows
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    tunnel_proc = subprocess.Popen(
        cmd_cf,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT_DIR),
        startupinfo=startupinfo
    )
    
    tunnel_url = None
    start_time = time.time()
    print("Waiting for Cloudflare Tunnel URL...")
    while time.time() - start_time < 20:
        line = tunnel_proc.stderr.readline()
        if not line:
            break
        print(f"CF Tunnel Log: {line.strip()}")
        match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", line)
        if match:
            tunnel_url = match.group(0)
            break
            
    if not tunnel_url:
        print("Error: Could not capture Cloudflare Tunnel URL. Exiting...")
        tunnel_proc.terminate()
        return

    print(f"\nCaptured Tunnel URL: {tunnel_url}")
    
    # Update Supabase database server status
    print("Updating server status in Supabase...")
    await database.init_db()
    now_str = datetime.now(timezone.utc).isoformat()
    await database.update_server_status(
        status="online",
        tunnel_url=tunnel_url,
        last_ping=now_str
    )
    
    # Launch backend FastAPI server
    print("Starting FastAPI Backend Server...")
    env = os.environ.copy()
    env["BACKEND_PUBLIC_URL"] = tunnel_url
    env["ENV"] = "production"
    
    cmd_back = [sys.executable, "run_server.py"]
    backend_proc = subprocess.Popen(
        cmd_back,
        env=env,
        cwd=str(ROOT_DIR),
        startupinfo=startupinfo
    )
    
    print("\n" + "="*60)
    print("StoryForge Server and Cloudflare Tunnel are now ONLINE!")
    print(f"Public URL: {tunnel_url}")
    print("="*60 + "\n")
    
    # Monitor and print logs
    try:
        while True:
            await asyncio.sleep(5)
            # Update last ping periodically to keep status online
            now_str = datetime.now(timezone.utc).isoformat()
            try:
                await database.update_server_status(
                    status="online",
                    tunnel_url=tunnel_url,
                    last_ping=now_str
                )
            except Exception as e:
                print(f"Error updating server status in database (temporary drop?): {e}")
            
            # Check if processes are alive
            if tunnel_proc.poll() is not None:
                print("Cloudflare Tunnel exited unexpectedly!")
                break
            if backend_proc.poll() is not None:
                print("FastAPI Backend Server exited unexpectedly!")
                break
    except KeyboardInterrupt:
        print("Shutting down processes...")
    finally:
        # Cleanup
        try:
            tunnel_proc.terminate()
        except:
            pass
        try:
            backend_proc.terminate()
        except:
            pass
        async with database.DatabaseConnection(database.DATABASE_URL) as db:
            await db.execute(
                "UPDATE server_status SET status = 'offline' WHERE id = 'current'"
            )
            await db.commit()
        print("Server is now offline.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
