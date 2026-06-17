# NSSM Windows Services — StoryForge AI

Run FastAPI and Cloudflare Tunnel as proper Windows services that **auto-start at boot** — no login required after initial setup.

---

## Why NSSM?

| Feature | Task Scheduler | NSSM |
|---------|----------------|------|
| Starts at boot | After user logon | At boot (before logon)* |
| Restarts on crash | Manual config | Built-in |
| Service Manager UI | No | Yes (`services.msc`) |
| Runs without login | No | Yes (with stored credentials) |

\*Services run under your Windows account (see below) so cloudflared can read tunnel credentials.

---

## Prerequisites

Complete these **before** installing services:

1. **Backend works manually**
   ```powershell
   cd storyforge-api
   .\start_backend.bat
   curl http://127.0.0.1:8000/health
   ```

2. **`.env` configured** (copy from `.env.example`)

3. **Cloudflare Tunnel created** (see [CLOUDFLARE_TUNNEL_SETUP.md](./CLOUDFLARE_TUNNEL_SETUP.md))
   ```powershell
   cloudflared tunnel login
   cloudflared tunnel create storyforge-api
   cloudflared tunnel route dns storyforge-api api.yourdomain.com
   ```

4. **Sleep disabled** (see [WINDOWS_HOSTING.md](./WINDOWS_HOSTING.md))

---

## Step 1 — Install NSSM

```powershell
winget install NSSM.NSSM
```

Close and reopen PowerShell, then verify:

```powershell
nssm version
```

---

## Step 2 — Install Services (Administrator)

Open **PowerShell as Administrator**:

```powershell
cd "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\scripts"
powershell -ExecutionPolicy Bypass -File install_nssm_services.ps1
```

You will be prompted for your **Windows password**. NSSM stores it so services can start at boot under your account (required for `%USERPROFILE%\.cloudflared` credentials).

Optional custom tunnel name or port:

```powershell
powershell -ExecutionPolicy Bypass -File install_nssm_services.ps1 -TunnelName storyforge-api -Port 8000
```

This registers:

| Service | Executable | Purpose |
|---------|------------|---------|
| `StoryForgeAPI` | `.venv\Scripts\python.exe` | Uvicorn on `0.0.0.0:8000` |
| `StoryForgeTunnel` | `cloudflared` | `tunnel run storyforge-api` |

Logs go to `storyforge-api\logs\`:
- `service_api.log` / `service_api_err.log`
- `service_tunnel.log` / `service_tunnel_err.log`

---

## Step 3 — Start Services

Still in Administrator PowerShell:

```powershell
nssm start StoryForgeAPI
Start-Sleep -Seconds 5
nssm start StoryForgeTunnel
```

Or use Windows Service Manager:

```powershell
services.msc
```

Find **StoryForgeAPI** and **StoryForgeTunnel** → Start.

---

## Step 4 — Verify

```powershell
# Local API
curl http://127.0.0.1:8000/health

# Public tunnel
curl https://api.yourdomain.com/health

# Service status
Get-Service StoryForgeAPI, StoryForgeTunnel
nssm status StoryForgeAPI
nssm status StoryForgeTunnel
```

---

## Day-to-Day Commands

```powershell
# Start / stop
nssm start StoryForgeAPI
nssm stop StoryForgeAPI
nssm restart StoryForgeAPI

nssm start StoryForgeTunnel
nssm stop StoryForgeTunnel

# Status
Get-Service StoryForge*
nssm status StoryForgeAPI

# Tail logs
Get-Content "...\storyforge-api\logs\service_api.log" -Tail 30 -Wait
Get-Content "...\storyforge-api\logs\service_tunnel.log" -Tail 30 -Wait
```

---

## After Code or .env Changes

```powershell
nssm restart StoryForgeAPI
# Tunnel usually does not need restart for .env changes
```

After `pip install -r requirements.txt`:

```powershell
nssm restart StoryForgeAPI
```

---

## Uninstall Services

Administrator PowerShell:

```powershell
cd storyforge-api\scripts
powershell -ExecutionPolicy Bypass -File uninstall_nssm_services.ps1
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Service starts then stops immediately | Check `logs\service_api_err.log` — usually missing `.env` or bad `DATABASE_URL` |
| Tunnel service fails | Check `logs\service_tunnel_err.log` — run `cloudflared tunnel run storyforge-api` manually first |
| `Access denied` on cloudflared | Re-run install script — services must run as **your** user, not LocalSystem |
| Password changed | Re-run `install_nssm_services.ps1` to update stored credentials |
| Port 8000 in use | Stop manual `start_backend.bat` first: `nssm stop StoryForgeAPI` |
| API works locally, tunnel 502 | API not running — `nssm start StoryForgeAPI` and wait 5s |

### Manual debug (run as your user, not Administrator)

```powershell
cd storyforge-api
.venv\Scripts\python -m uvicorn main:app --host 0.0.0.0 --port 8000
# separate terminal:
cloudflared tunnel run storyforge-api
```

---

## Security Note

NSSM stores your Windows password to start services at boot. Only install on a machine you control. If you prefer not to store credentials, use **Task Scheduler** instead ([WINDOWS_HOSTING.md](./WINDOWS_HOSTING.md) Option A) — it runs at logon without storing a password.
