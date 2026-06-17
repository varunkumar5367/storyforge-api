# StoryForge AI — Complete Deployment Guide

**Goal:** Run the FastAPI backend on your Windows laptop, expose it via Cloudflare Tunnel, and connect your Vercel frontend.

```
Users → Vercel Frontend → Cloudflare Tunnel (HTTPS) → FastAPI :8000 → Supabase + Cloudinary
```

**Time needed:** ~45–60 minutes (first time)

---

## Overview — What YOU do vs what's already done

| Already done in code | You do manually |
|---------------------|-----------------|
| Config system (`BACKEND_PUBLIC_URL`, CORS, JWT) | Fill in `.env` secrets |
| Health + monitoring endpoints | Create Cloudflare account + tunnel |
| `start_backend.bat` | Point DNS to tunnel |
| NSSM / Task Scheduler scripts | Set Vercel env var |
| FFmpeg thread tuning | Disable laptop sleep |
| Frontend `getAssetUrl()` handles tunnel URLs | Keep laptop plugged in |

---

# PHASE 1 — Prepare Your Laptop (15 min)

## Step 1.1 — Install FFmpeg

FFmpeg is required for video composition.

1. Download from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/) (ffmpeg-release-essentials.zip)
2. Extract to `C:\ffmpeg`
3. Add `C:\ffmpeg\bin` to your **PATH**:
   - Press `Win + S` → search **"Environment Variables"**
   - Click **Edit the system environment variables**
   - **Environment Variables** → under **User variables** → select **Path** → **Edit** → **New**
   - Add: `C:\ffmpeg\bin`
   - Click OK on all dialogs
4. **Close and reopen** PowerShell, then verify:

```powershell
ffmpeg -version
ffprobe -version
```

You should see version info for both.

---

## Step 1.2 — Install cloudflared

Open PowerShell:

```powershell
winget install Cloudflare.cloudflared
```

Close and reopen PowerShell, then verify:

```powershell
cloudflared --version
```

---

## Step 1.3 — Install NSSM (auto-start at boot)

```powershell
winget install NSSM.NSSM
```

Verify:

```powershell
nssm version
```

---

## Step 1.4 — Disable Sleep (critical)

If the laptop sleeps, the API and tunnel go offline.

1. **Settings** → **System** → **Power & battery** → **Screen and sleep**
2. When **plugged in**:
   - **Sleep:** Never
   - **Hibernate:** Off (if shown)
3. Keep the laptop **plugged in** when hosting production traffic

Optional (Administrator PowerShell):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
```

---

# PHASE 2 — Configure the Backend (10 min)

## Step 2.1 — Open the project folder

```powershell
cd "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api"
```

## Step 2.2 — Python virtual environment

If `.venv` does not exist:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

If it already exists, just update packages:

```powershell
.venv\Scripts\pip install -r requirements.txt
```

## Step 2.3 — Update your `.env` file

Run the helper (adds any missing keys without erasing your existing values):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
```

Open `.env` in a text editor and fill in **every** value below.

### Required values

```env
# AI
GROQ_API_KEY=gsk_your_key_here

# Auth — generate a long random string (see Step 2.4)
JWT_SECRET_KEY=

# Database — Supabase connection string (Step 2.5)
DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres

# URLs — fill AFTER Cloudflare setup (Phase 3), or use placeholders now
FRONTEND_URL=https://your-app.vercel.app
BACKEND_PUBLIC_URL=https://api.yourdomain.com

# Production mode
ENV=production

# Storage
OUTPUT_DIR=./output
PORT=8000
FFMPEG_THREADS=4
```

### Optional but recommended

```env
CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name
GEMINI_API_KEY=
HUGGINGFACE_API_KEY=
POLLINATIONS_API_KEY=
VOICEFORGE_URL=
```

> **Note:** `VOICEFORGE_URL` is optional. With `edge-tts` installed, TTS runs locally on your laptop.

---

## Step 2.4 — Generate JWT_SECRET_KEY

In PowerShell:

```powershell
.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the output into `.env`:

```env
JWT_SECRET_KEY=paste_the_generated_string_here
```

---

## Step 2.5 — Get Supabase DATABASE_URL

1. Go to [https://supabase.com/dashboard](https://supabase.com/dashboard)
2. Open your project
3. **Settings** → **Database**
4. Under **Connection string**, choose **URI** mode
5. Select **Transaction pooler** (port **6543**) — recommended for FastAPI
6. Copy the string and replace `[YOUR-PASSWORD]` with your database password
7. Paste into `.env` as `DATABASE_URL=...`

Example shape:

```
postgresql://postgres.abcdefgh:MyPassword@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
```

---

## Step 2.6 — Test the backend locally

```powershell
.\start_backend.bat
```

Leave that window open. In a **new** PowerShell window:

```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/diagnostics
```

Expected: JSON with `"status": "healthy"` and `"ffmpeg_available": true`.

Press `Ctrl+C` in the backend window to stop when done testing.

---

# PHASE 3 — Cloudflare Tunnel (15 min)

You need a **domain on Cloudflare** (e.g. `yourdomain.com`). Free Cloudflare plan is fine.

## Step 3.1 — Log in to Cloudflare

1. Create account at [https://dash.cloudflare.com](https://dash.cloudflare.com)
2. Add your domain if not already added
3. Ensure domain status is **Active**

## Step 3.2 — Authenticate cloudflared

```powershell
cloudflared tunnel login
```

- A browser opens
- Select your domain
- Click **Authorize**

## Step 3.3 — Create the tunnel

```powershell
cloudflared tunnel create storyforge-api
```

Write down:
- **Tunnel ID** (shown in output)
- Credentials file: `C:\Users\varun\.cloudflared\<TUNNEL_ID>.json`

List tunnels to confirm:

```powershell
cloudflared tunnel list
```

## Step 3.4 — Create config file

Create file: `C:\Users\varun\.cloudflared\config.yml`

Replace `<TUNNEL_ID>` and `api.yourdomain.com` with your values:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: C:\Users\varun\.cloudflared\<TUNNEL_ID>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Example:

```yaml
tunnel: a1b2c3d4-e5f6-7890-abcd-ef1234567890
credentials-file: C:\Users\varun\.cloudflared\a1b2c3d4-e5f6-7890-abcd-ef1234567890.json

ingress:
  - hostname: api.mywebsite.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

## Step 3.5 — Create DNS record

```powershell
cloudflared tunnel route dns storyforge-api api.yourdomain.com
```

This creates a CNAME in Cloudflare DNS automatically.

Verify in Cloudflare Dashboard → **DNS** → you should see:

| Type | Name | Content | Proxy |
|------|------|---------|-------|
| CNAME | api | `<tunnel-id>.cfargotunnel.com` | Proxied (orange) |

## Step 3.6 — Update `.env` with your public URLs

Edit `storyforge-api\.env`:

```env
BACKEND_PUBLIC_URL=https://api.yourdomain.com
FRONTEND_URL=https://your-app.vercel.app
ENV=production
```

Save the file.

## Step 3.7 — Test tunnel manually

**Terminal 1** — start backend:

```powershell
cd storyforge-api
.\start_backend.bat
```

**Terminal 2** — start tunnel:

```powershell
cloudflared tunnel run storyforge-api
```

**Terminal 3** — verify:

```powershell
curl https://api.yourdomain.com/health
```

Expected: same healthy JSON as localhost.

Stop both terminals with `Ctrl+C` when satisfied.

---

# PHASE 4 — Auto-Start with NSSM (10 min)

So everything survives reboot without you logging in manually.

## Step 4.1 — Install services

Open **PowerShell as Administrator**:

```powershell
cd "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\scripts"
powershell -ExecutionPolicy Bypass -File install_nssm_services.ps1
```

When prompted, enter your **Windows login password**. NSSM needs this to run services as your user (so cloudflared can read tunnel credentials).

## Step 4.2 — Start services

Still in Administrator PowerShell:

```powershell
nssm start StoryForgeAPI
Start-Sleep -Seconds 5
nssm start StoryForgeTunnel
```

## Step 4.3 — Verify services

```powershell
Get-Service StoryForgeAPI, StoryForgeTunnel
curl http://127.0.0.1:8000/health
curl https://api.yourdomain.com/health
```

Both should return healthy JSON.

Check logs if anything fails:

```powershell
Get-Content "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\logs\service_api_err.log" -Tail 20
Get-Content "C:\Users\varun\OneDrive\Desktop\personal projects\yt\storyforge-api\logs\service_tunnel_err.log" -Tail 20
```

## Step 4.4 — Test reboot (optional but recommended)

1. Restart your laptop
2. **Do not** open any terminal manually
3. Wait 1–2 minutes after boot
4. Run: `curl https://api.yourdomain.com/health`

If healthy, auto-start works.

---

# PHASE 5 — Connect Vercel Frontend (5 min)

## Step 5.1 — Set environment variable in Vercel

1. Go to [https://vercel.com/dashboard](https://vercel.com/dashboard)
2. Open your **StoryForge frontend** project
3. **Settings** → **Environment Variables**
4. Add:

| Name | Value | Environments |
|------|-------|--------------|
| `NEXT_PUBLIC_API_URL` | `https://api.yourdomain.com` | Production, Preview, Development |

5. Click **Save**

## Step 5.2 — Redeploy frontend

1. **Deployments** tab
2. Click **⋯** on the latest deployment → **Redeploy**
3. Wait for build to finish

## Step 5.3 — Local frontend dev (optional)

```powershell
cd storyforge-frontend
copy .env.example .env.local
```

Edit `.env.local`:

```env
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
```

```powershell
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000)

---

# PHASE 6 — End-to-End Test (5 min)

1. Open your Vercel app URL in a browser
2. **Register** or **Login**
3. Upload a small `.txt` story (under 1500 words for free tier)
4. Watch job progress on the status page
5. When complete, verify:
   - Video plays
   - Thumbnail loads
   - Download links work

Run automated checks:

```powershell
cd storyforge-api
powershell -ExecutionPolicy Bypass -File scripts\verify_deployment.ps1 -PublicUrl "https://api.yourdomain.com"
```

---

# Quick Reference

## Daily commands

```powershell
nssm restart StoryForgeAPI          # after .env or code change
Get-Service StoryForgeAPI, StoryForgeTunnel
curl https://api.yourdomain.com/health
```

## Log files

| File | What it is |
|------|------------|
| `storyforge-api\storyforge.log` | Application log |
| `storyforge-api\logs\service_api.log` | NSSM uvicorn stdout |
| `storyforge-api\logs\service_api_err.log` | NSSM uvicorn errors |
| `storyforge-api\logs\service_tunnel.log` | Cloudflare tunnel log |

## Troubleshooting

| Problem | What to do |
|---------|------------|
| `502` on public URL | `nssm start StoryForgeAPI` — backend not running |
| CORS error in browser | Check `FRONTEND_URL` in `.env` matches exact Vercel URL |
| Download URLs broken | Set `BACKEND_PUBLIC_URL` in `.env`, restart API |
| Tunnel won't start | Run `cloudflared tunnel run storyforge-api` manually to see error |
| Login fails after migration | Users need to re-login; ensure `JWT_SECRET_KEY` is set |
| Video pipeline OOM | Lower `FFMPEG_THREADS` to 2 in `.env` |
| API offline after sleep | Disable sleep; keep laptop plugged in |

---

# Checklist — Print and tick off

```
[ ] FFmpeg + ffprobe on PATH
[ ] cloudflared installed
[ ] NSSM installed
[ ] Sleep disabled (plugged in)
[ ] .env fully filled (GROQ, JWT, DATABASE, URLs)
[ ] curl http://127.0.0.1:8000/health → healthy
[ ] Cloudflare tunnel created
[ ] config.yml written
[ ] DNS CNAME for api.yourdomain.com
[ ] curl https://api.yourdomain.com/health → healthy
[ ] NSSM services installed and running
[ ] Vercel NEXT_PUBLIC_API_URL set
[ ] Frontend redeployed
[ ] End-to-end story upload test passed
[ ] Reboot test passed (optional)
```

---

# Related docs

- [NSSM_SETUP.md](./NSSM_SETUP.md) — NSSM details
- [CLOUDFLARE_TUNNEL_SETUP.md](./CLOUDFLARE_TUNNEL_SETUP.md) — tunnel reference
- [WINDOWS_HOSTING.md](./WINDOWS_HOSTING.md) — sleep, FFmpeg benchmark
