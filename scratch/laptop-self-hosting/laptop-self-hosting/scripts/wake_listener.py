"""
Skeleton listener for the wake-on-demand flow described in the
laptop-self-hosting skill. Polls Supabase for new wake_requests, shows a
popup, and on Accept starts the backend + confirms the tunnel is reachable.

Fill in:
  - SUPABASE_URL / SUPABASE_KEY (use the anon/service key already set up
    for the project's Supabase instance — no new account needed)
  - start_backend() / is_tunnel_up() for the actual project's commands

Requires: pip install supabase requests plyer  (no credit card needed for any of these)
"""

import subprocess
import time

import requests
from plyer import notification
from supabase import create_client

SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_KEY = "YOUR_ANON_OR_SERVICE_KEY"
TUNNEL_HEALTH_URL = "http://localhost:8000/health"
POLL_INTERVAL_SECONDS = 5
ACCEPT_TIMEOUT_SECONDS = 120

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
backend_process = None


def is_tunnel_up() -> bool:
    try:
        r = requests.get(TUNNEL_HEALTH_URL, timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


def start_backend():
    global backend_process
    if backend_process is None or backend_process.poll() is not None:
        # Replace with the real start command for this project.
        backend_process = subprocess.Popen(["python", "main.py"])
    # Give it a moment, then confirm.
    for _ in range(20):
        if is_tunnel_up():
            return True
        time.sleep(1)
    return False


def set_status(status: str, tunnel_url: str | None = None):
    payload = {"status": status}
    if tunnel_url is not None:
        payload["tunnel_url"] = tunnel_url
    supabase.table("server_status").update(payload).eq("id", 1).execute()


def handle_wake_request(request_row):
    notification.notify(
        title="Wake request received",
        message=f"A visitor wants the server online. Message: "
                 f"{request_row.get('message') or '(none)'}\n"
                 f"Run accept_request() within {ACCEPT_TIMEOUT_SECONDS}s to start it.",
        timeout=ACCEPT_TIMEOUT_SECONDS,
    )
    # NOTE: plyer notifications don't capture a click response on every OS.
    # For a real Accept/Ignore button, swap this for a small tkinter popup
    # or a pystray menu item — this skeleton just shows the simplest path.
    # Replace this input() with whatever UI mechanism the user prefers.
    answer = input("Accept this wake request? [y/N]: ").strip().lower()
    if answer == "y":
        set_status("starting")
        if start_backend():
            set_status("online", TUNNEL_HEALTH_URL.replace("/health", ""))
        else:
            set_status("offline")
    supabase.table("wake_requests").update({"resolved": True}).eq(
        "id", request_row["id"]
    ).execute()


def poll_loop():
    seen_ids = set()
    while True:
        rows = (
            supabase.table("wake_requests")
            .select("*")
            .eq("resolved", False)
            .execute()
            .data
        )
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                handle_wake_request(row)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    poll_loop()
