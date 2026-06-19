---
name: laptop-self-hosting
description: Set up free, no-credit-card self-hosting on a personal laptop or desktop with a permanent public URL, using Cloudflare Tunnel to expose a localhost backend and Supabase as a signaling layer so a publicly-hosted frontend (e.g. Vercel) can detect whether the home machine is online, request it to wake up, and show the visitor a loading or "contact admin" state while waiting. Use this whenever someone wants to host an app for free on their own hardware instead of paying for cloud hosting, needs to expose a local server to the public internet without port forwarding or a credit card, or wants an "approve before it goes live" gate on a backend that isn't always running. Also covers a lightweight admin dashboard for live RAM/CPU monitoring and concurrency limits on the self-hosted machine.
---

# Laptop Self-Hosting with Wake-on-Demand

This skill covers turning a personal machine into a free, publicly reachable backend,
with an optional approval gate so the owner controls when it actually serves traffic.

## When to use the simple version instead

Before building the full wake/approval flow below, check whether the user actually
needs it. If the machine is realistically online whenever visitors would show up (e.g.
the owner starts it before sharing the link), skip straight to "Simple mode" — it's far
less code and has fewer failure modes. Only build the full approval-gate flow if the
user specifically wants visitors to be able to trigger a wake request while the owner
is away or undecided about going live.

## Architecture

```
Visitor Browser
     |
     v
Public frontend (Vercel, free, always up)
     |
     v
Supabase tables: server_status, wake_requests   <-- signaling layer, already free
     |
     v
Cloudflare Tunnel  <-->  Backend process on the local machine
```

Supabase is the messenger because the local machine sits behind NAT/a home router and
can't receive inbound connections directly — but it *can* poll or subscribe outward to
Supabase, which is free with no card and likely already in the stack for the app's data.

## Step 1 — Cloudflare Tunnel (the public URL)

Install `cloudflared` (no account needed for a quick tunnel; a free Cloudflare account
gives a stable named tunnel instead of a rotating URL):

```bash
# Windows: download cloudflared.exe from
# https://github.com/cloudflare/cloudflared/releases

# Quick tunnel (URL changes each run):
cloudflared.exe tunnel run --url localhost:8000

# Named tunnel (stable URL, requires free Cloudflare account + a domain
# you control, or use Cloudflare's free subdomain options):
cloudflared.exe tunnel login
cloudflared.exe tunnel create my-backend
cloudflared.exe tunnel route dns my-backend backend.yourdomain.com
cloudflared.exe tunnel run my-backend
```

Prefer the named tunnel if the user wants the URL to stay constant — a rotating URL
means the frontend's API base URL has to be updated every restart, which defeats a lot
of the point.

## Step 2 — Supabase schema for signaling

```sql
create table server_status (
  id int primary key default 1,
  status text not null default 'offline', -- 'offline' | 'online' | 'starting'
  tunnel_url text,
  updated_at timestamptz not null default now()
);
insert into server_status (id) values (1);

create table wake_requests (
  id uuid primary key default gen_random_uuid(),
  message text,
  created_at timestamptz not null default now(),
  resolved boolean not null default false
);
```

`server_status` is a single row the frontend polls. `wake_requests` is an append-only
log of "someone wants the server on" events, optionally carrying a message for the
admin when the owner didn't respond the first time.

## Step 3 — Listener script on the local machine

A small always-running script that:
1. Polls (or subscribes via Supabase Realtime to) `wake_requests` for new unresolved
   rows.
2. On a new request, shows a native popup/notification with Accept / Ignore.
3. On Accept: starts the backend process (if not already running), confirms the tunnel
   is up, writes `status='online'` and the current `tunnel_url` to `server_status`.
4. On Ignore or no response within a timeout (suggest 2 minutes as a default, adjust
   based on user preference): leaves `status='offline'`.

See `scripts/wake_listener.py` for a working skeleton (polling-based, since polling is
simpler to get right than Realtime subscriptions and is plenty fast at this scale —
swap in Realtime later only if poll latency becomes a real problem).

For the popup itself, `plyer` (cross-platform notifications) or `pystray` + `tkinter`
(system tray icon with a popup window) both work without a credit card or paid SDK.

## Step 4 — Frontend wake flow

On page load, the frontend should:

1. Fetch `server_status`. If `online`, health-check the `tunnel_url` directly (don't
   trust the DB flag alone — the tunnel can die without the DB knowing) and proceed
   normally if it responds.
2. If `offline` or the health check fails, insert a row into `wake_requests` and show a
   loading state ("Waking up the server — this can take a minute"), polling
   `server_status` every few seconds.
3. If `status` flips to `online` and the health check passes within the timeout, drop
   the loading state and continue.
4. If the timeout elapses with no response, replace the loading state with: "The
   server is currently offline. Please contact the admin to start it," plus a text box.
   Submitting it inserts a new `wake_requests` row carrying that message, re-notifying
   the admin, and shows "Request sent." Rate-limit resubmission per visitor (e.g. once
   every 5 minutes) so the admin doesn't get spammed by one impatient visitor refreshing.

## Step 5 — Admin dashboard

Add a lightweight, secret-protected `/admin` route (a shared-secret query param or
header is enough at this scale — this doesn't need full auth) showing:
- Live RAM/CPU of the backend process (reuse the `memory-leak-diagnosis` skill's
  `psutil` pattern — log/display `process.memory_info().rss` and `psutil.cpu_percent()`).
- Count of active generation jobs / connected users.
- A settable max-concurrency field that the backend actually enforces — reject or queue
  requests past the cap with a clear message, don't silently drop them.
- The list of pending `wake_requests` with an Accept action available from the
  dashboard itself, in addition to the local popup.

## Failure modes to call out to the user

Be upfront about these rather than presenting the setup as flawless:
- If the laptop loses internet or sleeps mid-request, in-flight requests fail — surface
  this to the visitor as a clear error, not a silent hang.
- A rotating quick-tunnel URL means every restart requires updating wherever the URL is
  stored (env var, `server_status` row) — a named tunnel avoids this but needs a domain.
- Polling has a delay (a few seconds) between "admin clicks Accept" and "frontend
  notices" — set expectations accordingly rather than promising instant wake-up.
