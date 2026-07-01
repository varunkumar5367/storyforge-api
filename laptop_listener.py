"""
laptop_listener.py — StoryForge Self-Hosted Laptop Listener Daemon.
Runs a background loop polling Supabase for wake requests and offers a native GUI dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import subprocess
import threading
import time
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, messagebox
import psutil

# Windows Selector Event Loop Policy for psycopg
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("storyforge.listener")

# Load environment
from dotenv import load_dotenv
load_dotenv()

# Import Database Helper
import database

# Global state variables
backend_process = None
tunnel_process = None
tunnel_url = None
is_running_server = False
db_loop = None  # Asyncio loop running in the database thread

# GPU mode — True = GPU (VRAM), False = CPU (RAM)
# Initialise from env so setting persists across listener restarts via .env
use_gpu_mode: bool = os.environ.get("FORCE_CPU", "0").strip().lower() not in ("1", "true", "yes")


def _get_vram_info() -> tuple[float, float] | None:
    """Return (used_MB, total_MB) VRAM or None if CUDA not available."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        used = torch.cuda.memory_allocated(0) / 1024 ** 2
        total = torch.cuda.get_device_properties(0).total_memory / 1024 ** 2
        return used, total
    except Exception:
        return None

def kill_process_tree(pid: int):
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        # Give them a moment to terminate, then kill
        gone, alive = psutil.wait_procs(children, timeout=3)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        try:
            parent.terminate()
            parent.wait(timeout=3)
        except Exception:
            try:
                parent.kill()
            except Exception:
                pass
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        logger.error("Error killing process tree for PID %s: %s", pid, e)

# ---------------------------------------------------------------------------
# Background DB Worker Thread
# ---------------------------------------------------------------------------
def run_db_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

db_loop = asyncio.new_event_loop()
db_thread = threading.Thread(target=run_db_loop, args=(db_loop,), daemon=True)
db_thread.start()

def run_async(coro):
    """Run a coroutine in the background database thread loop and return a Future."""
    return asyncio.run_coroutine_threadsafe(coro, db_loop)


# ---------------------------------------------------------------------------
# Wake Request Dialog (Tkinter popup)
# ---------------------------------------------------------------------------
class WakeRequestDialog:
    def __init__(self, parent, message_text, timeout=120):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("StoryForge - Job Approval")
        self.dialog.attributes("-topmost", True)
        self.dialog.geometry("480x340")
        self.dialog.resizable(True, True)
        
        # Center the dialog relative to parent
        x = parent.winfo_x() + (parent.winfo_width() - 480) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 340) // 2
        self.dialog.geometry(f"+{x}+{y}")
        
        self.result = None
        self.timeout = timeout
        
        # Main Frame
        frame = ttk.Frame(self.dialog, padding="15")
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title_label = ttk.Label(frame, text="Video Generation Request", font=("Segoe UI", 16, "bold"), foreground="#8b5cf6")
        title_label.pack(anchor=tk.W, pady=(0, 5))
        
        # Description
        desc_text = "A user is requesting to render a video on your laptop."
        desc_label = ttk.Label(frame, text=desc_text, font=("Segoe UI", 10), wraplength=420)
        desc_label.pack(anchor=tk.W, pady=(0, 5))
        
        # Custom Message Box
        if message_text:
            msg_frame = ttk.LabelFrame(frame, text="Details", padding="8")
            msg_frame.pack(fill=tk.X, pady=(0, 8))
            msg_label = ttk.Label(msg_frame, text=message_text, font=("Segoe UI", 9, "italic"), wraplength=400)
            msg_label.pack(anchor=tk.W)
        else:
            ttk.Label(frame, text="No details provided.", font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=(0, 10))
            
        # Countdown Timer
        self.countdown_label = ttk.Label(frame, text=f"Auto-declining in {self.timeout} seconds...", font=("Segoe UI", 9, "bold"), foreground="#ef4444")
        self.countdown_label.pack(anchor=tk.W, pady=(0, 10))
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM, expand=True)

        
        ignore_btn = tk.Button(
            btn_frame, 
            text="Decline", 
            command=self.on_ignore, 
            font=("Segoe UI", 10), 
            bg="#374151", 
            fg="white", 
            activebackground="#4b5563", 
            activeforeground="white", 
            borderwidth=0, 
            padx=15, 
            pady=6
        )
        ignore_btn.pack(side=tk.RIGHT, padx=5)
        
        accept_btn = tk.Button(
            btn_frame, 
            text="Accept & Render", 
            command=self.on_accept, 
            font=("Segoe UI", 10, "bold"), 
            bg="#8b5cf6", 
            fg="white", 
            activebackground="#7c3aed", 
            activeforeground="white", 
            borderwidth=0, 
            padx=20, 
            pady=6
        )
        accept_btn.pack(side=tk.RIGHT, padx=5)
        
        self.update_countdown()
        
    def update_countdown(self):
        if not self.dialog.winfo_exists():
            return
        if self.timeout <= 0:
            self.on_ignore()
        else:
            self.countdown_label.config(text=f"Auto-declining in {self.timeout} seconds...")
            self.timeout -= 1
            self.dialog.after(1000, self.update_countdown)
            
    def show(self):
        self.dialog.grab_set()
        self.dialog.wait_window()
        return self.result

    def on_accept(self):
        self.result = True
        self.dialog.destroy()
        
    def on_ignore(self):
        self.result = False
        self.dialog.destroy()


# ---------------------------------------------------------------------------
# Main GUI Dashboard
# ---------------------------------------------------------------------------
class ListenerDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("StoryForge — Server Dashboard")
        self.geometry("560x530")
        self.resizable(False, False)

        # Style Configuration
        self.style = ttk.Style()
        self.style.theme_use('clam')

        # Colors & Font styling
        self.configure(bg="#0f172a")
        self.style.configure(".", background="#0f172a", foreground="white")
        self.style.configure("TFrame", background="#0f172a")
        self.style.configure("TLabel", background="#0f172a", foreground="white")
        self.style.configure("TLabelframe", background="#0f172a", foreground="#94a3b8")
        self.style.configure("TLabelframe.Label", background="#0f172a", foreground="#94a3b8", font=("Segoe UI", 9, "bold"))
        self.style.configure("Horizontal.TProgressbar", troughcolor="#1e293b", background="#10b981", thickness=14)
        self.style.configure("VRAM.Horizontal.TProgressbar", troughcolor="#1e293b", background="#8b5cf6", thickness=14)

        # Main Layout
        self.main_frame = ttk.Frame(self, padding="20")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # ── Header ─────────────────────────────────────────────────────────
        self.header_label = ttk.Label(self.main_frame, text="StoryForge Backend", font=("Segoe UI", 18, "bold"), foreground="#a78bfa")
        self.header_label.pack(anchor=tk.W, pady=(0, 2))
        self.sub_label = ttk.Label(self.main_frame, text="Laptop GPU Listener — AI generation daemon", font=("Segoe UI", 9), foreground="#94a3b8")
        self.sub_label.pack(anchor=tk.W, pady=(0, 14))

        # ── Server Status Card ─────────────────────────────────────────────
        self.status_card = ttk.LabelFrame(self.main_frame, text="Server Status", padding="12")
        self.status_card.pack(fill=tk.X, pady=(0, 12))

        status_row = ttk.Frame(self.status_card)
        status_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(status_row, text="State: ", font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.state_indicator = ttk.Label(status_row, text="● OFFLINE", font=("Segoe UI", 11, "bold"), foreground="#ef4444")
        self.state_indicator.pack(side=tk.LEFT)

        url_row = ttk.Frame(self.status_card)
        url_row.pack(fill=tk.X)
        ttk.Label(url_row, text="Tunnel: ", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.url_label = ttk.Entry(url_row, font=("Consolas", 9), width=38, background="#1e293b", foreground="#a78bfa")
        self.url_label.pack(side=tk.LEFT, padx=(5, 5))
        self.url_label.insert(0, "Not established")
        self.url_label.config(state="readonly")
        self.copy_btn = tk.Button(url_row, text="Copy", command=self.copy_url, font=("Segoe UI", 8), bg="#334155", fg="white", borderwidth=0, padx=8, pady=2)
        self.copy_btn.pack(side=tk.LEFT)

        # ── GPU / RAM Mode Toggle Card ──────────────────────────────────────
        self.gpu_card = ttk.LabelFrame(self.main_frame, text="Compute Mode", padding="12")
        self.gpu_card.pack(fill=tk.X, pady=(0, 12))

        gpu_top = ttk.Frame(self.gpu_card)
        gpu_top.pack(fill=tk.X, pady=(0, 8))

        self.mode_label = ttk.Label(gpu_top, text="", font=("Segoe UI", 11, "bold"))
        self.mode_label.pack(side=tk.LEFT)

        self.gpu_toggle_btn = tk.Button(
            gpu_top,
            text="",
            command=self.toggle_compute_mode,
            font=("Segoe UI", 9, "bold"),
            borderwidth=0, padx=14, pady=4
        )
        self.gpu_toggle_btn.pack(side=tk.RIGHT)
        self._refresh_gpu_mode_ui()

        # VRAM progress bar
        vram_bar_row = ttk.Frame(self.gpu_card)
        vram_bar_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(vram_bar_row, text="VRAM Usage:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.vram_pct_label = ttk.Label(vram_bar_row, text="N/A", font=("Segoe UI", 9), foreground="#a78bfa")
        self.vram_pct_label.pack(side=tk.RIGHT)
        self.vram_bar = ttk.Progressbar(self.gpu_card, style="VRAM.Horizontal.TProgressbar", orient="horizontal", length=400, mode="determinate")
        self.vram_bar.pack(fill=tk.X, pady=(4, 0))

        # ── Resource Gauges ─────────────────────────────────────────────────
        self.perf_card = ttk.LabelFrame(self.main_frame, text="System Resources", padding="12")
        self.perf_card.pack(fill=tk.X, pady=(0, 12))

        perf_row = ttk.Frame(self.perf_card)
        perf_row.pack(fill=tk.X, pady=(0, 6))
        self.cpu_label = ttk.Label(perf_row, text="CPU: —", font=("Segoe UI", 10))
        self.cpu_label.pack(side=tk.LEFT, expand=True)
        self.ram_label = ttk.Label(perf_row, text="RAM: —", font=("Segoe UI", 10))
        self.ram_label.pack(side=tk.LEFT, expand=True)
        self.tasks_label = ttk.Label(perf_row, text="Active Tasks: 0", font=("Segoe UI", 10))
        self.tasks_label.pack(side=tk.LEFT, expand=True)

        # CPU progress bar
        self.cpu_bar = ttk.Progressbar(self.perf_card, orient="horizontal", length=400, mode="determinate")
        self.cpu_bar.pack(fill=tk.X, pady=(0, 4))
        # RAM progress bar
        self.ram_bar = ttk.Progressbar(self.perf_card, orient="horizontal", length=400, mode="determinate")
        self.ram_bar.pack(fill=tk.X)

        # ── Control Buttons ─────────────────────────────────────────────────
        self.control_row = ttk.Frame(self.main_frame)
        self.control_row.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        self.exit_btn = tk.Button(
            self.control_row, text="Exit Listener", command=self.on_exit,
            font=("Segoe UI", 10), bg="#374151", fg="white",
            activebackground="#4b5563", activeforeground="white",
            borderwidth=0, padx=15, pady=8
        )
        self.exit_btn.pack(side=tk.LEFT)

        self.toggle_btn = tk.Button(
            self.control_row, text="Start Server Manually", command=self.toggle_server,
            font=("Segoe UI", 10, "bold"), bg="#8b5cf6", fg="white",
            activebackground="#7c3aed", activeforeground="white",
            borderwidth=0, padx=20, pady=8
        )
        self.toggle_btn.pack(side=tk.RIGHT)

        # Start server and tunnel automatically on listener boot
        self.start_server_bg()

        # Start the loops
        self.poll_requests()
        self.poll_system_stats()
        self.poll_vram()
        self.protocol("WM_DELETE_WINDOW", self.on_exit)
        
    def copy_url(self):
        global tunnel_url
        if tunnel_url:
            self.clipboard_clear()
            self.clipboard_append(tunnel_url)
            messagebox.showinfo("Copied", "Tunnel URL copied to clipboard.")
            
    def toggle_server(self):
        global is_running_server
        if is_running_server:
            # Stop
            self.stop_server_bg()
        else:
            # Start
            self.start_server_bg()
            
    def start_server_bg(self):
        self.toggle_btn.config(state="disabled", text="Starting...")
        threading.Thread(target=self.start_server_flow, daemon=True).start()
        
    def stop_server_bg(self):
        self.toggle_btn.config(state="disabled", text="Stopping...")
        threading.Thread(target=self.stop_server_flow, daemon=True).start()
        
    def start_server_flow(self):
        global backend_process, tunnel_process, tunnel_url, is_running_server
        
        logger.info("Starting cloudflared tunnel...")
        cmd_cf = ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"]
        
        # Hide window for cloudflared subprocess on Windows
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        tunnel_process = subprocess.Popen(
            cmd_cf, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            encoding="utf-8",
            startupinfo=startupinfo
        )
        
        # Read stderr to extract the trycloudflare URL
        cf_url = None
        start_time = time.time()
        while time.time() - start_time < 20:
            line = tunnel_process.stderr.readline()
            if not line:
                break
            logger.info("CF LOG: %s", line.strip())
            match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", line)
            if match:
                cf_url = match.group(0)
                break
                
        if not cf_url:
            logger.error("Failed to retrieve Cloudflare Tunnel URL.")
            self.after(0, lambda: messagebox.showerror("Tunnel Error", "Could not capture Cloudflare Tunnel URL. Check if cloudflared is installed."))
            self.after(0, self.reset_buttons)
            try:
                tunnel_process.terminate()
            except Exception:
                pass
            return
            
        tunnel_url = cf_url
        logger.info("Tunnel URL established: %s", tunnel_url)
        
        # Start backend, injecting BACKEND_PUBLIC_URL
        env = os.environ.copy()
        env["BACKEND_PUBLIC_URL"] = tunnel_url
        env["ENV"] = "production"
        
        logger.info("Starting FastAPI backend...")
        cmd_back = [sys.executable, "run_server.py"]
        backend_process = subprocess.Popen(
            cmd_back,
            env=env,
            startupinfo=startupinfo
        )
        
        is_running_server = True
        
        # Update server_status table in database
        now_str = datetime.now(timezone.utc).isoformat()
        run_async(database.update_server_status(
            status="online",
            tunnel_url=tunnel_url,
            last_ping=now_str
        ))
        
        self.after(0, self.on_server_started)
        
    def stop_server_flow(self):
        global backend_process, tunnel_process, tunnel_url, is_running_server
        
        logger.info("Stopping backend and tunnel processes...")
        if backend_process:
            logger.info("Stopping backend process tree...")
            kill_process_tree(backend_process.pid)
            backend_process = None
            
        if tunnel_process:
            logger.info("Stopping tunnel process tree...")
            kill_process_tree(tunnel_process.pid)
            tunnel_process = None
            
        tunnel_url = None
        is_running_server = False
        
        # Update DB status
        now_str = datetime.now(timezone.utc).isoformat()
        run_async(database.update_server_status(
            status="offline",
            tunnel_url=None,
            last_ping=now_str
        ))
        
        self.after(0, self.on_server_stopped)
        
    def on_server_started(self):
        self.state_indicator.config(text="\u25cf ONLINE", foreground="#10b981")
        self.url_label.config(state="normal")
        self.url_label.delete(0, tk.END)
        self.url_label.insert(0, tunnel_url)
        self.url_label.config(state="readonly")
        self.toggle_btn.config(state="normal", text="Stop Server Manually", bg="#ef4444", activebackground="#dc2626")
        
    def on_server_stopped(self):
        self.state_indicator.config(text="\u25cf OFFLINE", foreground="#ef4444")
        self.url_label.config(state="normal")
        self.url_label.delete(0, tk.END)
        self.url_label.insert(0, "Not established")
        self.url_label.config(state="readonly")
        self.toggle_btn.config(state="normal", text="Start Server Manually", bg="#8b5cf6", activebackground="#7c3aed")
        
    def reset_buttons(self):
        self.toggle_btn.config(state="normal", text="Start Server Manually", bg="#8b5cf6", activebackground="#7c3aed")

    # ── GPU / RAM Mode Toggle ──────────────────────────────────────────────
    def _refresh_gpu_mode_ui(self):
        """Sync mode label and toggle button text with current use_gpu_mode state."""
        global use_gpu_mode
        if use_gpu_mode:
            self.mode_label.config(text="\u26a1 GPU Mode (VRAM)  ", foreground="#a78bfa")
            self.gpu_toggle_btn.config(text="Switch to CPU (RAM)", bg="#1e293b", fg="#94a3b8",
                                       activebackground="#334155", activeforeground="white")
        else:
            self.mode_label.config(text="\U0001f4be CPU Mode (RAM)  ", foreground="#f59e0b")
            self.gpu_toggle_btn.config(text="Switch to GPU (VRAM)", bg="#8b5cf6", fg="white",
                                       activebackground="#7c3aed", activeforeground="white")

    def toggle_compute_mode(self):
        """Toggle between GPU (VRAM) and CPU (RAM) compute mode."""
        global use_gpu_mode
        use_gpu_mode = not use_gpu_mode
        if use_gpu_mode:
            os.environ["FORCE_CPU"] = "0"
            logger.info("Compute mode switched to: GPU (VRAM)")
        else:
            os.environ["FORCE_CPU"] = "1"
            logger.info("Compute mode switched to: CPU (RAM)")
        self._refresh_gpu_mode_ui()

    def poll_vram(self):
        """Update VRAM usage bar every 2 seconds."""
        info = _get_vram_info()
        if info is not None:
            used_mb, total_mb = info
            pct = (used_mb / total_mb) * 100 if total_mb > 0 else 0
            self.vram_bar["value"] = pct
            self.vram_pct_label.config(
                text=f"{used_mb:.0f} MB / {total_mb:.0f} MB  ({pct:.1f}%)",
                foreground="#ef4444" if pct > 85 else "#a78bfa"
            )
        else:
            self.vram_bar["value"] = 0
            self.vram_pct_label.config(text="No GPU detected", foreground="#64748b")
        self.after(2000, self.poll_vram)
        
    def poll_requests(self):
        """Poll database for pending video generation jobs (status = 'pending_approval')."""
        global active_jobs, max_concurrent_jobs
        if is_running_server and active_jobs < max_concurrent_jobs:
            fut = run_async(get_next_pending_job())
            self.after(2000, lambda: self.check_job_future(fut))
        else:
            self.after(3000, self.poll_requests)
            
    def check_job_future(self, fut):
        if not fut.done():
            self.after(100, lambda: self.check_job_future(fut))
            return
            
        try:
            job = fut.result()
            if job:
                logger.info("Found pending job approval request: %s from user %s", job["id"], job["username"])
                filename = job.get("story_filename") or "Untitled Story"
                word_count = len((job.get("story_text") or "").split())
                msg = f"User '{job['username']}' requested a video for story:\n\"{filename}\" ({word_count} words)\n\nVoice: {job['voice']}"
                
                # Auto-approve if AUTO_APPROVE is enabled in the environment
                auto_approve = os.getenv("AUTO_APPROVE", "False").lower() == "true"
                if auto_approve:
                    logger.info("AUTO_APPROVE is active. Automatically accepting job request %s.", job["id"])
                    result = True
                else:
                    dialog = WakeRequestDialog(self, msg, timeout=120)
                    result = dialog.show()
                
                if result:
                    logger.info("Admin accepted job request %s.", job["id"])
                    run_async(run_pipeline_task(job["id"], job["story_text"]))
                else:
                    logger.info("Admin declined/ignored job request %s.", job["id"])
                    run_async(decline_job(job["id"]))
        except Exception as e:
            logger.error("Error checking job future: %s", e)
            
        self.after(3000, self.poll_requests)
        
    def poll_system_stats(self):
        """Check system RAM/CPU and update dashboard gauges."""
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        self.cpu_label.config(text=f"CPU: {cpu:.1f}%")
        self.ram_label.config(text=f"RAM: {ram:.1f}%")
        self.cpu_bar["value"] = cpu
        self.ram_bar["value"] = ram

        if is_running_server:
            fut = run_async(database.get_server_status())
            self.after(500, lambda: self.update_active_tasks(fut))
        else:
            self.tasks_label.config(text="Active Tasks: 0")
            self.after(3000, self.poll_system_stats)

    def update_active_tasks(self, fut):
        if not fut.done():
            self.after(100, lambda: self.update_active_tasks(fut))
            return
        try:
            status = fut.result()
            active_tasks = status.get("active_tasks", 0)
            self.tasks_label.config(text=f"Active Tasks: {active_tasks}")
        except Exception:
            pass
        self.after(3000, self.poll_system_stats)
        
    def on_exit(self):
        if messagebox.askyesno("Exit Listener", "Are you sure you want to stop the listener daemon? This will also stop the backend server if running."):
            self.toggle_btn.config(state="disabled")
            self.exit_btn.config(state="disabled")
            threading.Thread(target=self.shutdown_and_exit, daemon=True).start()

    def shutdown_and_exit(self):
        logger.info("Shutting down listener daemon...")
        self.stop_server_flow()
        time.sleep(1)
        db_loop.call_soon_threadsafe(db_loop.stop)
        self.after(0, self.destroy_and_exit)

    def destroy_and_exit(self):
        self.destroy()
        sys.exit(0)


# ---------------------------------------------------------------------------
# Background Pipeline Execution Helpers
# ---------------------------------------------------------------------------
active_jobs = 0
max_concurrent_jobs = 1

async def get_next_pending_job():
    from database import DatabaseConnection, DATABASE_URL
    async with DatabaseConnection(DATABASE_URL) as db:
        async with db.execute(
            "SELECT id, story_text, story_filename, voice, user_id FROM jobs WHERE status = 'pending_approval' ORDER BY created_at ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            job_data = dict(row)
            
            # Fetch username
            username = "unknown"
            async with db.execute("SELECT username FROM users WHERE id = ?", (job_data["user_id"],)) as ucur:
                urow = await ucur.fetchone()
                if urow:
                    username = urow["username"]
            job_data["username"] = username
            
            # Lock the job immediately so no other poll grabs it
            await db.execute(
                "UPDATE jobs SET status = 'prompting_approval', current_step = 'prompting_approval' WHERE id = ?",
                (job_data["id"],)
            )
            await db.commit()
            return job_data

async def run_pipeline_task(job_id: str, story_text: str):
    global active_jobs
    active_jobs += 1
    logger.info("Starting pipeline task locally for job %s...", job_id)
    try:
        from services.orchestrator import _run_pipeline_impl
        from config import settings
        
        # Override backend public url so download URLs point to the local tunnel URL
        global tunnel_url
        settings.backend_public_url = tunnel_url
        logger.info("Set backend_public_url = %s", tunnel_url)
        
        await _run_pipeline_impl(job_id, story_text)
        logger.info("Pipeline task completed locally for job %s.", job_id)
    except Exception as e:
        logger.error("Error executing pipeline task locally for job %s: %s", job_id, e)
    finally:
        active_jobs -= 1

async def decline_job(job_id: str):
    from database import update_job
    from services.orchestrator import _append_log
    await update_job(
        job_id,
        status="failed",
        current_step="failed",
        error_message="Request declined by host laptop.",
        completed_at=datetime.now(timezone.utc).isoformat()
    )
    await _append_log(job_id, "FAILED: Request declined by host laptop.")


if __name__ == "__main__":
    logger.info("Starting StoryForge Server Dashboard Dashboard Application...")
    app = ListenerDashboard()
    app.mainloop()
