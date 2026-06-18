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
        self.dialog.title("StoryForge - Wake Request")
        self.dialog.attributes("-topmost", True)
        self.dialog.geometry("450x260")
        self.dialog.resizable(False, False)
        
        # Center the dialog relative to parent
        x = parent.winfo_x() + (parent.winfo_width() - 450) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 260) // 2
        self.dialog.geometry(f"+{x}+{y}")
        
        self.result = None
        self.timeout = timeout
        
        # Main Frame
        frame = ttk.Frame(self.dialog, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title_label = ttk.Label(frame, text="Incoming Wake Request", font=("Segoe UI", 16, "bold"), foreground="#8b5cf6")
        title_label.pack(anchor=tk.W, pady=(0, 10))
        
        # Description
        desc_text = "A visitor is requesting to start the video generation backend."
        desc_label = ttk.Label(frame, text=desc_text, font=("Segoe UI", 10), wraplength=400)
        desc_label.pack(anchor=tk.W, pady=(0, 8))
        
        # Custom Message Box
        if message_text:
            msg_frame = ttk.LabelFrame(frame, text="User Message", padding="8")
            msg_frame.pack(fill=tk.X, pady=(0, 10))
            msg_label = ttk.Label(msg_frame, text=message_text, font=("Segoe UI", 9, "italic"), wraplength=380)
            msg_label.pack(anchor=tk.W)
        else:
            ttk.Label(frame, text="No message provided.", font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, pady=(0, 12))
            
        # Countdown Timer
        self.countdown_label = ttk.Label(frame, text=f"Auto-ignoring in {self.timeout} seconds...", font=("Segoe UI", 9, "bold"), foreground="#ef4444")
        self.countdown_label.pack(anchor=tk.W, pady=(0, 15))
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        ignore_btn = tk.Button(
            btn_frame, 
            text="Ignore", 
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
            text="Accept & Start Server", 
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
            self.countdown_label.config(text=f"Auto-ignoring in {self.timeout} seconds...")
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
        self.geometry("500x420")
        self.resizable(False, False)
        
        # Style Configuration
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # Colors & Font styling
        self.configure(bg="#0f172a") # dark slate
        self.style.configure(".", background="#0f172a", foreground="white")
        self.style.configure("TFrame", background="#0f172a")
        self.style.configure("TLabel", background="#0f172a", foreground="white")
        
        # Main Layout
        self.main_frame = ttk.Frame(self, padding="20")
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Header
        self.header_label = ttk.Label(self.main_frame, text="StoryForge Backend", font=("Segoe UI", 18, "bold"), foreground="#a78bfa")
        self.header_label.pack(anchor=tk.W, pady=(0, 5))
        
        self.sub_label = ttk.Label(self.main_frame, text="Always-on background listener daemon for wake requests", font=("Segoe UI", 9), foreground="#94a3b8")
        self.sub_label.pack(anchor=tk.W, pady=(0, 20))
        
        # Status Card
        self.status_card = ttk.LabelFrame(self.main_frame, text="System Status", padding="15")
        self.status_card.pack(fill=tk.X, pady=(0, 20))
        
        status_row = ttk.Frame(self.status_card)
        status_row.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(status_row, text="Server State: ", font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.state_indicator = ttk.Label(status_row, text="OFFLINE", font=("Segoe UI", 11, "bold"), foreground="#ef4444")
        self.state_indicator.pack(side=tk.LEFT)
        
        url_row = ttk.Frame(self.status_card)
        url_row.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(url_row, text="Tunnel URL: ", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.url_label = ttk.Entry(url_row, font=("Consolas", 9), width=35, background="#1e293b", foreground="#a78bfa")
        self.url_label.pack(side=tk.LEFT, padx=(5, 5))
        self.url_label.insert(0, "Not established")
        self.url_label.config(state="readonly")
        
        self.copy_btn = tk.Button(url_row, text="Copy", command=self.copy_url, font=("Segoe UI", 8), bg="#334155", fg="white", borderwidth=0, padx=8, pady=2)
        self.copy_btn.pack(side=tk.LEFT)
        
        # Performance Gauges
        self.perf_card = ttk.LabelFrame(self.main_frame, text="Resources & Activity", padding="15")
        self.perf_card.pack(fill=tk.X, pady=(0, 20))
        
        perf_row = ttk.Frame(self.perf_card)
        perf_row.pack(fill=tk.X)
        
        self.cpu_label = ttk.Label(perf_row, text="CPU: 0.0%", font=("Segoe UI", 10))
        self.cpu_label.pack(side=tk.LEFT, expand=True)
        
        self.ram_label = ttk.Label(perf_row, text="RAM: 0.0%", font=("Segoe UI", 10))
        self.ram_label.pack(side=tk.LEFT, expand=True)
        
        self.tasks_label = ttk.Label(perf_row, text="Active Tasks: 0", font=("Segoe UI", 10))
        self.tasks_label.pack(side=tk.LEFT, expand=True)
        
        # Control Buttons
        self.control_row = ttk.Frame(self.main_frame)
        self.control_row.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.exit_btn = tk.Button(
            self.control_row, 
            text="Exit Listener", 
            command=self.on_exit, 
            font=("Segoe UI", 10), 
            bg="#374151", 
            fg="white", 
            activebackground="#4b5563", 
            activeforeground="white", 
            borderwidth=0, 
            padx=15, 
            pady=8
        )
        self.exit_btn.pack(side=tk.LEFT)
        
        self.toggle_btn = tk.Button(
            self.control_row, 
            text="Start Server Manually", 
            command=self.toggle_server, 
            font=("Segoe UI", 10, "bold"), 
            bg="#8b5cf6", 
            fg="white", 
            activebackground="#7c3aed", 
            activeforeground="white", 
            borderwidth=0, 
            padx=20, 
            pady=8
        )
        self.toggle_btn.pack(side=tk.RIGHT)
        
        # Start the loops
        self.poll_requests()
        self.poll_system_stats()
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
        self.state_indicator.config(text="ONLINE", foreground="#10b981") # green
        self.url_label.config(state="normal")
        self.url_label.delete(0, tk.END)
        self.url_label.insert(0, tunnel_url)
        self.url_label.config(state="readonly")
        
        self.toggle_btn.config(state="normal", text="Stop Server Manually", bg="#ef4444", activebackground="#dc2626")
        
    def on_server_stopped(self):
        self.state_indicator.config(text="OFFLINE", foreground="#ef4444") # red
        self.url_label.config(state="normal")
        self.url_label.delete(0, tk.END)
        self.url_label.insert(0, "Not established")
        self.url_label.config(state="readonly")
        
        self.toggle_btn.config(state="normal", text="Start Server Manually", bg="#8b5cf6", activebackground="#7c3aed")
        
    def reset_buttons(self):
        self.toggle_btn.config(state="normal", text="Start Server Manually", bg="#8b5cf6", activebackground="#7c3aed")
        
    def poll_requests(self):
        """Poll database for pending wake requests."""
        if not is_running_server:
            # Only process wake requests if we are currently offline
            fut = run_async(database.list_wake_requests(limit=1))
            # Schedule check for result
            self.after(2000, lambda: self.check_request_future(fut))
        else:
            self.after(3000, self.poll_requests)
            
    def check_request_future(self, fut):
        if not fut.done():
            self.after(100, lambda: self.check_request_future(fut))
            return
            
        try:
            requests = fut.result()
            if requests:
                req = requests[0]
                if req["status"] == "pending":
                    logger.info("Found pending wake request: %s", req["id"])
                    # Pop up wake request dialog box
                    dialog = WakeRequestDialog(self, req["message"])
                    result = dialog.show()
                    
                    if result:
                        logger.info("Admin accepted wake request %s.", req["id"])
                        # Mark accepted in DB
                        run_async(database.review_wake_request(req["id"], "accepted"))
                        # Start server
                        self.start_server_bg()
                    else:
                        logger.info("Admin ignored/denied wake request %s.", req["id"])
                        # Mark ignored in DB
                        run_async(database.review_wake_request(req["id"], "ignored"))
        except Exception as e:
            logger.error("Error polling wake requests: %s", e)
            
        self.after(3000, self.poll_requests)
        
    def poll_system_stats(self):
        """Check system RAM/CPU and update dashboard."""
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        self.cpu_label.config(text=f"CPU: {cpu:.1f}%")
        self.ram_label.config(text=f"RAM: {ram:.1f}%")
        
        if is_running_server:
            # Query DB for active tasks
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

if __name__ == "__main__":
    logger.info("Starting StoryForge Server Dashboard Dashboard Application...")
    app = ListenerDashboard()
    app.mainloop()
