"""
utils/monitoring.py — Production diagnostics: disk, RAM, FFmpeg, startup checks.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger("storyforge.monitoring")


def _bytes_to_mb(n: float) -> float:
    return round(n / (1024 * 1024), 2)


def get_disk_usage(path: str | Path | None = None) -> dict[str, Any]:
    """Return disk usage for the output directory mount."""
    target = Path(path or settings.output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    return {
        "path": str(target),
        "total_mb": _bytes_to_mb(usage.total),
        "used_mb": _bytes_to_mb(usage.used),
        "free_mb": _bytes_to_mb(usage.free),
        "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
    }


def get_memory_usage() -> dict[str, Any]:
    """Return current process RSS and system memory stats (cross-platform)."""
    result: dict[str, Any] = {
        "platform": platform.system(),
        "process_rss_mb": None,
        "system_total_mb": None,
        "system_available_mb": None,
        "system_used_percent": None,
    }

    try:
        import psutil

        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        result["process_rss_mb"] = _bytes_to_mb(mem.rss)

        vm = psutil.virtual_memory()
        result["system_total_mb"] = _bytes_to_mb(vm.total)
        result["system_available_mb"] = _bytes_to_mb(vm.available)
        result["system_used_percent"] = vm.percent
    except ImportError:
        if platform.system() != "Windows":
            try:
                import resource

                rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                # Linux reports KB; macOS reports bytes
                if platform.system() == "Darwin":
                    result["process_rss_mb"] = _bytes_to_mb(rss_kb)
                else:
                    result["process_rss_mb"] = round(rss_kb / 1024, 2)
            except Exception:
                pass

    return result


def check_ffmpeg() -> dict[str, Any]:
    """Verify ffmpeg and ffprobe are available and log version info."""
    diag: dict[str, Any] = {
        "ffmpeg_path": shutil.which("ffmpeg"),
        "ffprobe_path": shutil.which("ffprobe"),
        "ffmpeg_version": None,
        "ffprobe_version": None,
        "available": False,
    }

    for key, binary in (("ffmpeg_version", "ffmpeg"), ("ffprobe_version", "ffprobe")):
        path = shutil.which(binary)
        if not path:
            continue
        try:
            proc = subprocess.run(
                [path, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                diag[key] = proc.stdout.splitlines()[0][:120]
        except Exception as exc:
            logger.warning("%s version check failed: %s", binary, exc)

    diag["available"] = bool(diag["ffmpeg_path"] and diag["ffprobe_path"])
    return diag


def get_output_dir_stats() -> dict[str, Any]:
    """Summarise the output directory size and job count."""
    output = Path(settings.output_dir)
    if not output.exists():
        return {"path": str(output), "exists": False, "job_dirs": 0, "size_mb": 0}

    job_dirs = [d for d in output.iterdir() if d.is_dir()]
    total_bytes = sum(
        f.stat().st_size for d in job_dirs for f in d.rglob("*") if f.is_file()
    )
    return {
        "path": str(output.resolve()),
        "exists": True,
        "job_dirs": len(job_dirs),
        "size_mb": _bytes_to_mb(total_bytes),
    }


def run_startup_diagnostics() -> dict[str, Any]:
    """Run all startup checks and log warnings for missing dependencies."""
    ffmpeg = check_ffmpeg()
    disk = get_disk_usage()
    memory = get_memory_usage()
    output_stats = get_output_dir_stats()

    if not ffmpeg["available"]:
        logger.error(
            "FFmpeg/ffprobe not found on PATH. Video composition will fail. "
            "Install FFmpeg and ensure it is on PATH."
        )
    else:
        logger.info(
            "FFmpeg OK | ffmpeg=%s | ffprobe=%s",
            ffmpeg.get("ffmpeg_version", "?"),
            ffmpeg.get("ffprobe_version", "?"),
        )

    if disk["free_mb"] < 1024:
        logger.warning(
            "Low disk space on output volume: %.0f MB free at %s",
            disk["free_mb"],
            disk["path"],
        )

    if not settings.effective_jwt_secret:
        logger.warning(
            "JWT_SECRET_KEY is not set — using insecure development default. "
            "Set JWT_SECRET_KEY in production."
        )

    if settings.is_production and not settings.backend_public_url:
        logger.warning(
            "ENV=production but BACKEND_PUBLIC_URL is not set. "
            "Download URLs will be relative paths only."
        )

    if settings.is_production and not settings.frontend_url:
        logger.warning(
            "ENV=production but FRONTEND_URL is not set. "
            "CORS will block the Vercel frontend."
        )

    logger.info(
        "Startup diagnostics | python=%s | platform=%s | env=%s | "
        "backend=%s | frontend=%s | disk_free=%.0fMB | output_jobs=%d",
        sys.version.split()[0],
        platform.system(),
        settings.env,
        settings.backend_public_url or "(relative)",
        settings.frontend_url or "(unset)",
        disk["free_mb"],
        output_stats["job_dirs"],
    )

    return {
        "ffmpeg": ffmpeg,
        "disk": disk,
        "memory": memory,
        "output": output_stats,
        "config": {
            "env": settings.env,
            "backend_public_url": settings.backend_public_url or None,
            "frontend_url": settings.frontend_url or None,
            "database": "postgresql" if settings.database_url.startswith("postgres") else "sqlite",
            "cloudinary_configured": bool(settings.cloudinary_url),
        },
    }


def get_health_payload() -> dict[str, Any]:
    """Lightweight health payload for /health endpoint."""
    ffmpeg = check_ffmpeg()
    disk = get_disk_usage()
    memory = get_memory_usage()

    status = "healthy" if ffmpeg["available"] else "degraded"
    return {
        "status": status,
        "service": "StoryForge AI",
        "ffmpeg_available": ffmpeg["available"],
        "disk_free_mb": disk["free_mb"],
        "memory": memory,
    }
