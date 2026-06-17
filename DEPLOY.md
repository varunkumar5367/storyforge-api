# StoryForge API — Quick Start

**Full deployment guide:** [docs/COMPLETE_DEPLOYMENT_GUIDE.md](docs/COMPLETE_DEPLOYMENT_GUIDE.md)

## Local dev (5 min)

```powershell
cd storyforge-api
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
scripts\generate_jwt_secret.bat          # paste into .env
.venv\Scripts\pip install -r requirements.txt
.\start_backend.bat
```

## Production (laptop + Cloudflare Tunnel)

See [docs/COMPLETE_DEPLOYMENT_GUIDE.md](docs/COMPLETE_DEPLOYMENT_GUIDE.md) for the full step-by-step checklist.

Quick verify:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_deployment.ps1 -PublicUrl "https://api.yourdomain.com"
```

## Scripts

| Script | Purpose |
|--------|---------|
| `start_backend.bat` | Start uvicorn locally |
| `scripts/setup_env.ps1` | Add missing keys to `.env` |
| `scripts/generate_jwt_secret.bat` | Generate JWT secret |
| `scripts/verify_deployment.ps1` | Pre-flight checks |
| `scripts/install_nssm_services.ps1` | Auto-start at boot (Admin) |
| `scripts/install_autostart_tasks.ps1` | Task Scheduler alternative |

## Docs

- [COMPLETE_DEPLOYMENT_GUIDE.md](docs/COMPLETE_DEPLOYMENT_GUIDE.md) — **start here**
- [CLOUDFLARE_TUNNEL_SETUP.md](docs/CLOUDFLARE_TUNNEL_SETUP.md)
- [NSSM_SETUP.md](docs/NSSM_SETUP.md)
- [WINDOWS_HOSTING.md](docs/WINDOWS_HOSTING.md)
