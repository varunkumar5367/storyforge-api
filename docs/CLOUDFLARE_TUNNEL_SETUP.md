# Cloudflare Tunnel Setup — StoryForge AI Backend

This guide exposes your Windows laptop FastAPI backend (`localhost:8000`) to the internet via Cloudflare Tunnel, so the Vercel frontend can reach it over HTTPS.

## Architecture

```
Users → Vercel Frontend → Cloudflare Tunnel (HTTPS) → FastAPI :8000 → Supabase / Cloudinary
```

---

## Prerequisites

- Cloudflare account (free tier works)
- A domain added to Cloudflare (e.g. `yourdomain.com`)
- StoryForge backend running locally on port 8000
- `.env` configured with production values (see `.env.example`)

---

## Step 1 — Cloudflare Account & Domain

1. Sign up at [https://dash.cloudflare.com](https://dash.cloudflare.com)
2. Add your domain: **Websites → Add a site**
3. Update nameservers at your registrar to Cloudflare's NS records
4. Wait for status **Active**

---

## Step 2 — Install cloudflared (Windows)

Download and install `cloudflared`:

```powershell
winget install Cloudflare.cloudflared
```

Verify:

```powershell
cloudflared --version
```

---

## Step 3 — Authenticate cloudflared

```powershell
cloudflared tunnel login
```

This opens a browser — select your domain and authorize.

---

## Step 4 — Create the Tunnel

```powershell
cloudflared tunnel create storyforge-api
```

Note the **Tunnel ID** and credentials file path (usually `%USERPROFILE%\.cloudflared\<TUNNEL_ID>.json`).

List tunnels:

```powershell
cloudflared tunnel list
```

---

## Step 5 — Configure the Tunnel

Create `%USERPROFILE%\.cloudflared\config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: C:\Users\<YOU>\.cloudflared\<TUNNEL_ID>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Replace `<TUNNEL_ID>`, username, and hostname.

---

## Step 6 — DNS Route

```powershell
cloudflared tunnel route dns storyforge-api api.yourdomain.com
```

Or manually in Cloudflare Dashboard → DNS → CNAME:

| Type | Name | Target |
|------|------|--------|
| CNAME | api | `<TUNNEL_ID>.cfargotunnel.com` |

Proxy status: **Proxied** (orange cloud).

---

## Step 7 — Backend Environment

In `storyforge-api/.env`:

```env
ENV=production
FRONTEND_URL=https://your-app.vercel.app
BACKEND_PUBLIC_URL=https://api.yourdomain.com
DATABASE_URL=postgresql://...
GROQ_API_KEY=...
JWT_SECRET_KEY=<long-random-string>
CLOUDINARY_URL=cloudinary://...
PORT=8000
OUTPUT_DIR=./output
```

Start the backend:

```bat
start_backend.bat
```

---

## Step 8 — Run the Tunnel (Manual Test)

```powershell
cloudflared tunnel run storyforge-api
```

Verify:

```powershell
curl https://api.yourdomain.com/health
```

Expected: `{"status":"healthy", ...}`

---

## Step 9 — Install as Windows Service (Persistent)

Run PowerShell **as Administrator**:

```powershell
cloudflared service install
```

Ensure `config.yml` is in `%USERPROFILE%\.cloudflared\` before installing.

Start / manage service:

```powershell
sc start cloudflared
sc query cloudflared
```

To uninstall:

```powershell
cloudflared service uninstall
```

**Alternative:** Use NSSM to run both `start_backend.bat` and `cloudflared tunnel run` as services.

---

## Step 10 — Vercel Frontend Configuration

In Vercel project settings → Environment Variables:

| Variable | Value |
|----------|-------|
| `NEXT_PUBLIC_API_URL` | `https://api.yourdomain.com` |

Redeploy the frontend after setting this.

---

## Verification Checklist

```powershell
# 1. Local backend
curl http://127.0.0.1:8000/health

# 2. Public tunnel
curl https://api.yourdomain.com/health

# 3. Detailed diagnostics
curl https://api.yourdomain.com/health/diagnostics

# 4. CORS — from browser console on Vercel app
fetch('https://api.yourdomain.com/health', { credentials: 'include' })
```

---

## Expected Commands Summary

| Action | Command |
|--------|---------|
| Create tunnel | `cloudflared tunnel create storyforge-api` |
| Run tunnel | `cloudflared tunnel run storyforge-api` |
| Route DNS | `cloudflared tunnel route dns storyforge-api api.yourdomain.com` |
| Install service | `cloudflared service install` |
| Start backend | `start_backend.bat` |
| Check health | `curl https://api.yourdomain.com/health` |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| 502 Bad Gateway | Backend not running — start `start_backend.bat` |
| CORS errors | Set `FRONTEND_URL` to exact Vercel URL; set `ENV=production` |
| Relative download URLs | Set `BACKEND_PUBLIC_URL=https://api.yourdomain.com` |
| Tunnel won't start | Check `config.yml` tunnel ID and credentials path |
| FFmpeg errors | Verify `ffmpeg -version` works in the same shell as uvicorn |

---

## Security Notes

- Cloudflare Tunnel does not expose your home IP — traffic flows outbound only
- Set a strong `JWT_SECRET_KEY` (32+ random bytes)
- Keep `ENV=production` to disable localhost CORS origins
- Consider Cloudflare Access for admin routes if needed

---

## Windows Laptop Operations

See [WINDOWS_HOSTING.md](./WINDOWS_HOSTING.md) for:

- Disabling sleep/hibernate (required — tunnel drops when laptop sleeps)
- Auto-start via Task Scheduler or NSSM after reboot
- FFmpeg thread benchmarking
