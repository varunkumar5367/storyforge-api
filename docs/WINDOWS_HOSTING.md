# Windows Laptop Hosting — StoryForge AI

Operational checklist for reliable 24/7 backend hosting on your Windows laptop.

---

## 1. FFmpeg Threads

Default is **4 threads** (`FFMPEG_THREADS=4` in `.env`).

Benchmark on your machine:

```powershell
cd storyforge-api
.venv\Scripts\python scratch\benchmark_ffmpeg_threads.py
.venv\Scripts\python scratch\benchmark_ffmpeg_threads.py --threads 2 4 6 8
```

Use the fastest setting that does not cause OOM during full pipeline runs (20+ scenes).

---

## 2. Disable Sleep (Required)

If the laptop sleeps, the Cloudflare Tunnel disconnects and the API disappears.

**Windows Settings → System → Power & battery → Screen and sleep**

When **plugged in**:

| Setting | Value |
|---------|-------|
| Screen | Your choice (e.g. 10 min) |
| Sleep | **Never** |
| Hibernate | **Off** |

Also check **Control Panel → Power Options → Change plan settings** for the active plan.

Optional (run as Administrator in PowerShell):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
```

Keep the laptop **plugged in** during production use.

---

## 3. Auto-Start After Reboot

Two options: **Task Scheduler** (built-in) or **NSSM** (Windows services).

### Option A — Task Scheduler (Recommended)

Run once as Administrator:

```powershell
cd "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\scripts"
.\install_autostart_tasks.ps1
```

This registers two tasks:

| Task | Runs at logon | Command |
|------|---------------|---------|
| `StoryForge-API` | Yes | `start_backend.bat` |
| `StoryForge-CloudflareTunnel` | Yes | `cloudflared tunnel run storyforge-api` |

Manage tasks:

```powershell
Get-ScheduledTask -TaskName "StoryForge-*"
Start-ScheduledTask -TaskName "StoryForge-API"
Stop-ScheduledTask -TaskName "StoryForge-API"
Unregister-ScheduledTask -TaskName "StoryForge-API" -Confirm:$false
```

### Option B — NSSM (Windows Services)

**Full guide:** [NSSM_SETUP.md](./NSSM_SETUP.md)

Quick start:

```powershell
winget install NSSM.NSSM
```

Run as **Administrator**:

```powershell
cd "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\scripts"
powershell -ExecutionPolicy Bypass -File install_nssm_services.ps1
```

Enter your Windows password when prompted (needed so cloudflared can read `%USERPROFILE%\.cloudflared` at boot).

Start services:

```powershell
nssm start StoryForgeAPI
Start-Sleep -Seconds 5
nssm start StoryForgeTunnel
```

Verify:

```powershell
Get-Service StoryForgeAPI, StoryForgeTunnel
curl http://127.0.0.1:8000/health
```

Uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File uninstall_nssm_services.ps1
```

---

## 4. Startup Order

1. Windows boots
2. **StoryForge-API** starts (uvicorn on `:8000`)
3. **StoryForge-CloudflareTunnel** starts (proxies to `127.0.0.1:8000`)

The tunnel task includes a 30-second delay so the API is ready first.

---

## 5. Verify After Reboot

```powershell
curl http://127.0.0.1:8000/health
curl https://api.yourdomain.com/health
```

Check logs:

- `storyforge-api\logs\backend.log`
- `storyforge-api\logs\uvicorn_*.log`
- `%USERPROFILE%\.cloudflared\` (tunnel logs if configured)

---

## 6. Related Docs

- [NSSM_SETUP.md](./NSSM_SETUP.md) — full NSSM service install guide
- [CLOUDFLARE_TUNNEL_SETUP.md](./CLOUDFLARE_TUNNEL_SETUP.md) — tunnel creation and DNS
- `.env.example` — production environment variables
