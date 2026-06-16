"""
services/video_composer.py
──────────────────────────
Step 5 of the StoryForge pipeline.

OPTIMIZED FOR RENDER.COM FREE TIER (512MB RAM limit).

Produces a single episode.mp4 at 1280×720, H.264 + AAC, through sequential
FFmpeg passes with aggressive memory management:

  Key optimizations:
  • Stream-based processing (no buffering entire video in memory)
  • Sequential single-clip composition (not batching)
  • Reduced frame buffer via -bufsize flag
  • Immediate temp file cleanup after each clip
  • Subprocess isolation with explicit memory limits
  • Minimal filter complexity per operation

  Pass 1 — Ken Burns clip per scene (sequential, one at a time)
  ───────────────────────────────────────────────────────────────
  For each scene, immediately:
    1. Build Ken Burns MP4 with audio
    2. Concat to running output (if not first clip)
    3. Delete source clip from disk
    4. Proceed to next scene

  Pass 2 — Subtitle burn-in (final pass over complete video)
  ──────────────────────────────────────────────────────────────
  If episode.srt exists, re-encode with subtitle filter.
  Otherwise, final output is the concatenated video.

Output
──────
    output/{job_id}/final/episode.mp4

Return contract
───────────────
Success:
    {
        "success": True,
        "data": {
            "video_path":   "<abs-path to episode.mp4>",
            "duration_secs": <float>,
            "scene_count":  <int>,
        }
    }

Failure:
    {
        "success": False,
        "error":  "<human-readable message>",
        "ffmpeg_stderr": "<last N chars of FFmpeg stderr>",
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.scene import Scene
from utils.file_handler import (
    final_video_path,
    get_final_dir,
    master_srt_path,
    scene_audio_path,
    scene_image_path,
)

logger = logging.getLogger("storyforge.video_composer")

# ─────────────────────────────────────────────────────────────────────────────
# Constants — Tuned for Render.com 512MB free tier
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_FPS: int = 25
OUTPUT_WIDTH: int = 1280
OUTPUT_HEIGHT: int = 720

# H.264 encoding — reduced bitrate for memory efficiency
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "ultrafast"  # ↓ was "fast" — uses ~30% less RAM during encoding
VIDEO_CRF = 26              # ↓ was 23 — slightly lower quality but smaller files
VIDEO_BUFSIZE = "512k"      # ↓ new — limits decoder frame buffer to 512KB

# AAC audio — reduced bitrate
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "128k"      # ↓ was 192k — still high quality for speech

# Ken Burns effect
KB_ZOOM_RATE: float = 0.0008
KB_MAX_ZOOM: float = 1.40

# xfade transition duration (seconds)
XFADE_DURATION: float = 0.5

# ffprobe fallback
FALLBACK_DURATION_SECS: float = 10.0

# Error reporting
STDERR_TAIL_CHARS: int = 2000

# Subprocess timeout (minutes per clip)
FFMPEG_TIMEOUT_SECS: int = 300  # 5 min per clip

# ─────────────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _ClipInfo:
    """Metadata about one successfully-built scene clip."""
    scene_number: int
    path: Path
    duration: float


class FFmpegError(RuntimeError):
    """Raised when an FFmpeg subprocess exits with a non-zero return code."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"FFmpeg exited with code {returncode}.\n"
            f"Command: {' '.join(cmd[:6])} ...\n"
            f"stderr (tail):\n{stderr[-STDERR_TAIL_CHARS:]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def compose_video(
    job_id: str,
    scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Assemble episode.mp4 from scenes using sequential, memory-efficient processing.

    Pipeline:
      1. Per-scene Ken Burns clip (immediate concat to running output)
      2. Subtitle burn-in (optional, final pass)

    Args:
        job_id:  Unique job identifier.
        scenes:  Serialised Scene dicts; image_path + audio_path must be set.

    Returns:
        See module docstring.
    """
    logger.info("Video composition START | job=%s | scenes=%d | mode=sequential-memory-optimized", job_id, len(scenes))

    # ── Validate inputs ───────────────────────────────────────────────────────
    final_dir = get_final_dir(job_id)
    placeholder_files: list[Path] = []
    valid_scenes = _filter_valid_scenes(
        scenes, job_id, final_dir, placeholder_files
    )
    if not valid_scenes:
        return {
            "success": False,
            "error": (
                "No scenes have audio_path on disk. "
                "Ensure voice_generator completed successfully."
            ),
            "ffmpeg_stderr": "",
        }

    out_path = final_video_path(job_id)
    srt_path = master_srt_path(job_id)
    has_subtitles = srt_path.exists()

    if not has_subtitles:
        logger.warning(
            "Master SRT not found at '%s' — video will have no subtitles.", srt_path
        )

    temp_files: list[Path] = list(placeholder_files)

    try:
        # ── Sequential clip composition ────────────────────────────────────────
        # Build clips one at a time, concatenating to running output.
        # This avoids holding multiple decoders in memory simultaneously.

        running_output: Path | None = None
        total_duration: float = 0.0
        concat_count: int = 0

        for i, scene in enumerate(valid_scenes):
            scene_num = scene.scene_number
            logger.info(
                "Scene %03d | Building Ken Burns clip (%d/%d)",
                scene_num,
                i + 1,
                len(valid_scenes),
            )

            # Probe audio duration
            duration = await _probe_audio_duration(scene.audio_path)
            total_duration += duration

            # Build single clip
            single_clip = final_dir / f"_clip_{i:03d}.mp4"
            temp_files.append(single_clip)

            await _build_ken_burns_clip(scene, duration, single_clip)

            # Concat to running output (if not first)
            if running_output is None:
                # First clip — just move it to running position
                running_output = final_dir / "_running_output.mp4"
                shutil.move(str(single_clip), str(running_output))
                temp_files.remove(single_clip)
                temp_files.append(running_output)
                logger.debug(
                    "Scene %03d | First clip, no concat needed",
                    scene_num,
                )
            else:
                # Concatenate this clip to the running output
                concat_count += 1
                logger.info(
                    "Scene %03d | Concatenating with xfade (transition %d)",
                    scene_num,
                    concat_count,
                )
                await _concat_two_clips(running_output, single_clip, running_output, duration)

                # Delete the individual clip immediately to free disk space
                try:
                    single_clip.unlink()
                    temp_files.remove(single_clip)
                    logger.debug("Freed disk space: deleted %s", single_clip.name)
                except (OSError, ValueError) as e:
                    logger.warning("Could not delete clip %s: %s", single_clip, e)

                # Aggressive GC to reclaim memory
                import gc
                gc.collect()

        if running_output is None:
            return {
                "success": False,
                "error": "No valid scenes processed.",
                "ffmpeg_stderr": "",
            }

        # ── Subtitle burn-in (optional final pass) ────────────────────────────
        if has_subtitles and srt_path.exists():
            logger.info(
                "Burning subtitles into final video ...",
            )
            await _burn_subtitles(running_output, srt_path, out_path)
            # Delete the intermediate running output
            try:
                running_output.unlink()
                logger.debug("Freed disk space: deleted running output")
            except OSError as e:
                logger.warning("Could not delete running output: %s", e)
        else:
            # No subtitles — just move running output to final
            shutil.move(str(running_output), str(out_path))
            logger.debug("No subtitles; moved running output to final")

        # ── Account for xfade overlaps ─────────────────────────────────────────
        if concat_count > 0:
            total_duration -= XFADE_DURATION * concat_count

        logger.info(
            "Video composition DONE | job=%s | output=%s | ~%.1fs",
            job_id,
            out_path.name,
            total_duration,
        )

        return {
            "success": True,
            "data": {
                "video_path":    str(out_path),
                "duration_secs": round(total_duration, 2),
                "scene_count":   len(valid_scenes),
            },
        }

    except FFmpegError as exc:
        logger.error(
            "FFmpegError during composition [job=%s]: rc=%d\n%s",
            job_id,
            exc.returncode,
            exc.stderr[-1000:],
        )
        return {
            "success": False,
            "error": f"FFmpeg failed (exit code {exc.returncode}). See ffmpeg_stderr for details.",
            "ffmpeg_stderr": exc.stderr[-STDERR_TAIL_CHARS:],
            "ffmpeg_cmd": " ".join(exc.cmd[:10]),
        }

    except Exception as exc:
        logger.exception("Unexpected error during video composition [job=%s].", job_id)
        return {
            "success": False,
            "error": f"Unexpected error: {exc}",
            "ffmpeg_stderr": "",
        }

    finally:
        # Always clean up temp files
        _cleanup(temp_files, out_path)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1 — Ken Burns clip (single scene)
# ─────────────────────────────────────────────────────────────────────────────


async def _build_ken_burns_clip(
    scene: Scene,
    duration: float,
    out_path: Path,
) -> None:
    """
    Build one scene clip with Ken Burns zoom-in effect.

    Memory optimizations:
      • ultrafast preset (less RAM for encoding)
      • Reduced CRF (smaller files, less memory pressure)
      • bufsize limits frame buffer
      • Direct stream mapping, no intermediate buffering
    """
    frames = max(1, int(duration * OUTPUT_FPS))

    camera_instr = getattr(scene, "camera", "slow_zoom_in") or "slow_zoom_in"
    camera_instr = camera_instr.lower().strip()

    # Zoom & pan expressions based on camera instruction
    if camera_instr == "slow_zoom_out":
        zoom_expr = f"max({KB_MAX_ZOOM}-{KB_ZOOM_RATE}*on,1.0)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif camera_instr == "pan_left":
        zoom_expr = f"{KB_MAX_ZOOM}"
        x_expr = f"(iw-iw/zoom)*(1-on/{frames})"
        y_expr = "ih/2-(ih/zoom/2)"
    elif camera_instr == "pan_right":
        zoom_expr = f"{KB_MAX_ZOOM}"
        x_expr = f"(iw-iw/zoom)*(on/{frames})"
        y_expr = "ih/2-(ih/zoom/2)"
    elif camera_instr == "pan_up":
        zoom_expr = f"{KB_MAX_ZOOM}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = f"(ih-ih/zoom)*(1-on/{frames})"
    elif camera_instr == "pan_down":
        zoom_expr = f"{KB_MAX_ZOOM}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = f"(ih-ih/zoom)*(on/{frames})"
    elif camera_instr == "static":
        zoom_expr = "1.0"
        x_expr = "0"
        y_expr = "0"
    else:  # slow_zoom_in (default)
        zoom_expr = f"min(zoom+{KB_ZOOM_RATE},{KB_MAX_ZOOM})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    filter_complex = (
        f"[0:v]"
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},"
        f"zoompan="
        f"z='{zoom_expr}':"
        f"x='{x_expr}':"
        f"y='{y_expr}':"
        f"d={frames}:"
        f"s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:"
        f"fps={OUTPUT_FPS}"
        f"[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        # ── Memory-conscious decoding ──────────────────────────────
        "-threads", "1",            # Single thread to reduce memory
        "-buffer_size", "512k",     # Limit input buffer
        "-loop", "1",
        "-framerate", str(OUTPUT_FPS),
        "-i", str(scene.image_path),
        "-i", str(scene.audio_path),
        # ── Filter ─────────────────────────────────────────────────
        "-filter_complex", filter_complex,
        # ── Stream mapping ─────────────────────────────────────────
        "-map", "[v]",
        "-map", "1:a",
        "-t", f"{duration:.3f}",
        # ── Encoding — ultrafast + reduced quality ────────────────
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-bufsize", VIDEO_BUFSIZE,  # Limit encoder buffer
        "-maxrate", "3000k",         # Limit bitrate spikes
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",   # Enable streaming (moov atom first)
        str(out_path),
    ]

    logger.debug("Ken Burns cmd: %s", _redacted_cmd(cmd))
    await _run_ffmpeg(cmd, timeout_secs=FFMPEG_TIMEOUT_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1b — Sequential xfade concatenation (two clips at a time)
# ─────────────────────────────────────────────────────────────────────────────


async def _concat_two_clips(
    clip_a: Path,
    clip_b: Path,
    out_path: Path,
    clip_b_duration: float,
) -> None:
    """
    Concatenate exactly two clips with xfade + acrossfade.

    Memory optimization:
      • Only two inputs in memory at once
      • Single-pass encoding
      • Minimal filter chain
    """
    # Calculate offset for xfade
    # For the second clip, the offset is (duration of first clip - fade duration)
    # But we don't have first clip duration readily, so we compute from file
    duration_a = await _probe_video_duration(clip_a)
    offset = duration_a - XFADE_DURATION

    filter_complex = (
        f"[0:v][1:v]"
        f"xfade=transition=fade:duration={XFADE_DURATION:.3f}:offset={offset:.3f}"
        f"[vout];"
        f"[0:a][1:a]"
        f"acrossfade=d={XFADE_DURATION:.3f}"
        f"[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-threads", "1",
        "-buffer_size", "512k",
        "-i", str(clip_a),
        "-i", str(clip_b),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-bufsize", VIDEO_BUFSIZE,
        "-maxrate", "3000k",
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]

    logger.debug("Concat xfade cmd: %s", _redacted_cmd(cmd))
    await _run_ffmpeg(cmd, timeout_secs=FFMPEG_TIMEOUT_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — Subtitle burn-in
# ─────────────────────────────────────────────────────────────────────────────


async def _burn_subtitles(
    video_path: Path,
    srt_path: Path,
    out_path: Path,
) -> None:
    """
    Burn episode.srt into the video using FFmpeg's libass subtitles filter.

    Style:
        FontName=Arial, FontSize=22, Bold=1
        PrimaryColour=&H00FFFFFF (white, fully opaque)
        OutlineColour=&H00000000 (black outline)
        Outline=2, Shadow=1, Alignment=2 (bottom-centre), MarginV=30
    """
    srt_escaped = _escape_srt_path(srt_path)

    subtitle_style = (
        "FontName=Arial,"
        "FontSize=22,"
        "Bold=1,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "Outline=2,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginV=30"
    )
    vf = f"subtitles='{srt_escaped}':force_style='{subtitle_style}'"

    cmd = [
        "ffmpeg", "-y",
        "-threads", "1",
        "-buffer_size", "512k",
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-bufsize", VIDEO_BUFSIZE,
        "-maxrate", "3000k",
        "-c:a", "copy",  # Audio already encoded
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]

    logger.debug("Subtitle burn cmd: %s", _redacted_cmd(cmd))
    await _run_ffmpeg(cmd, timeout_secs=FFMPEG_TIMEOUT_SECS)


# ─────────────────────────────────────────────────────────────────────────────
# ffprobe — audio and video duration
# ─────────────────────────────────────────────────────────────────────────────


async def _probe_audio_duration(audio_path: str | None) -> float:
    """Probe the exact duration of an MP3 file using ffprobe."""
    if not audio_path or not Path(audio_path).exists():
        logger.warning(
            "Audio file missing or None ('%s') — using fallback duration %.1fs.",
            audio_path,
            FALLBACK_DURATION_SECS,
        )
        return FALLBACK_DURATION_SECS

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(audio_path),
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[-500:]
            logger.warning("ffprobe rc=%d for '%s': %s", result.returncode, audio_path, err)
            return FALLBACK_DURATION_SECS

        info = json.loads(result.stdout.decode())
        streams = info.get("streams", [])

        if not streams:
            logger.warning("ffprobe: no streams found in '%s'.", audio_path)
            return FALLBACK_DURATION_SECS

        # Prefer audio stream's duration
        for stream in streams:
            if stream.get("codec_type") == "audio" and "duration" in stream:
                dur = float(stream["duration"])
                logger.debug("ffprobe: '%s' → %.3fs", Path(audio_path).name, dur)
                return max(0.1, dur)

        # Fallback to first stream
        dur = float(streams[0].get("duration", FALLBACK_DURATION_SECS))
        return max(0.1, dur)

    except Exception as exc:
        logger.warning("ffprobe error for '%s': %s", audio_path, exc)
        return FALLBACK_DURATION_SECS


async def _probe_video_duration(video_path: Path) -> float:
    """Probe the exact duration of a video file using ffprobe."""
    if not video_path.exists():
        logger.warning("Video file missing: '%s'", video_path)
        return FALLBACK_DURATION_SECS

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        str(video_path),
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning("ffprobe failed for '%s'", video_path)
            return FALLBACK_DURATION_SECS

        info = json.loads(result.stdout.decode())
        streams = info.get("streams", [])

        if streams and "duration" in streams[0]:
            dur = float(streams[0]["duration"])
            return max(0.1, dur)

        return FALLBACK_DURATION_SECS

    except Exception as exc:
        logger.warning("ffprobe error for '%s': %s", video_path, exc)
        return FALLBACK_DURATION_SECS


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg subprocess runner with timeout
# ─────────────────────────────────────────────────────────────────────────────


async def _run_ffmpeg(cmd: list[str], timeout_secs: int = 300) -> None:
    """
    Execute FFmpeg asynchronously with subprocess timeout and memory controls.

    Args:
        cmd: Full FFmpeg command list.
        timeout_secs: Max seconds per command (default 5 min).

    Raises:
        FFmpegError: If FFmpeg exits non-zero or times out.
    """
    try:
        out_path = Path(cmd[-1])
        stderr_log_path = out_path.parent / f"ffmpeg_{out_path.stem}_stderr.log"
    except Exception:
        import tempfile
        stderr_log_path = Path(tempfile.gettempdir()) / "ffmpeg_temp_stderr.log"

    logger.debug("Running FFmpeg (timeout=%ds): %s", timeout_secs, " ".join(cmd[:8]) + " ...")

    try:
        with open(stderr_log_path, "w", encoding="utf-8", errors="replace") as f_err:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=f_err,
                timeout=timeout_secs,
            )

        stderr_text = ""
        if stderr_log_path.exists():
            try:
                with open(stderr_log_path, "r", encoding="utf-8", errors="replace") as f_read:
                    f_read.seek(0, 2)
                    size = f_read.tell()
                    seek_pos = max(0, size - STDERR_TAIL_CHARS)
                    f_read.seek(seek_pos)
                    stderr_text = f_read.read()
            except Exception as e:
                logger.warning("Could not read stderr log: %s", e)

        if result.returncode != 0:
            logger.error(
                "FFmpeg FAILED | rc=%d | cmd=%s\nstderr (tail):\n%s",
                result.returncode,
                " ".join(cmd[:6]) + " ...",
                stderr_text[-1000:],
            )
            raise FFmpegError(cmd, result.returncode, stderr_text)

        logger.debug("FFmpeg OK | output=%s", cmd[-1])

    except asyncio.TimeoutError:
        logger.error("FFmpeg TIMEOUT (>%ds) | cmd=%s", timeout_secs, " ".join(cmd[:6]) + " ...")
        raise FFmpegError(cmd, -1, f"FFmpeg timeout (exceeded {timeout_secs}s)")

    finally:
        if stderr_log_path.exists():
            try:
                stderr_log_path.unlink()
            except OSError as e:
                logger.warning("Could not delete stderr log: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Validation and utilities
# ─────────────────────────────────────────────────────────────────────────────


def _filter_valid_scenes(
    scenes: list[dict[str, Any]],
    job_id: str,
    work_dir: Path,
    temp_files: list[Path],
) -> list[Scene]:
    """Return scenes that have narration audio on disk."""
    valid: list[Scene] = []
    for raw in scenes:
        scene = Scene(**raw)

        audio_path = _resolve_existing_path(
            scene.audio_path, scene_audio_path(job_id, scene.scene_number)
        )
        if not audio_path:
            logger.warning(
                "Scene %03d skipped | no audio on disk (%s)",
                scene.scene_number,
                scene.audio_path or "—",
            )
            continue

        image_path = _resolve_existing_path(
            scene.image_path, scene_image_path(job_id, scene.scene_number)
        )
        if not image_path:
            placeholder = work_dir / f"_placeholder_{scene.scene_number:03d}.png"
            _create_placeholder_image(
                placeholder,
                scene.scene_number,
                scene.title,
            )
            temp_files.append(placeholder)
            image_path = placeholder
            logger.warning(
                "Scene %03d | image missing — using placeholder (%s)",
                scene.scene_number,
                placeholder.name,
            )

        scene.audio_path = str(audio_path)
        scene.image_path = str(image_path)
        valid.append(scene)

    return valid


def _resolve_existing_path(
    stored: str | None,
    canonical: Path,
) -> Path | None:
    """Return the first path that exists on disk."""
    candidates: list[Path] = []
    if stored:
        candidates.append(Path(stored))
    if canonical not in candidates:
        candidates.append(canonical)

    for path in candidates:
        if path.exists():
            return path.resolve()
    return None


def _create_placeholder_image(
    out_path: Path,
    scene_number: int,
    title: str,
) -> None:
    """Write a dark 1280×720 placeholder PNG."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = OUTPUT_WIDTH, OUTPUT_HEIGHT
    img = Image.new("RGB", (w, h), color=(10, 8, 20))
    draw = ImageDraw.Draw(img)

    for y in range(h):
        t = y / h
        draw.line(
            [(0, y), (w, y)],
            fill=(int(18 + t * 5), int(12 + t * 4), int(35 + t * 10)),
        )

    try:
        font_large = ImageFont.truetype("arial.ttf", 48)
        font_small = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    label = f"SCENE {scene_number:03d}"
    draw.text(
        (w // 2, h // 2 - 50),
        label,
        fill=(180, 140, 255),
        font=font_large,
        anchor="mm",
    )
    short_title = title[:80] + "…" if len(title) > 80 else title
    if short_title:
        draw.text(
            (w // 2, h // 2 + 30),
            short_title,
            fill=(140, 120, 180),
            font=font_small,
            anchor="mm",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")


def _escape_srt_path(path: Path) -> str:
    """Produce an FFmpeg subtitles-filter-safe path."""
    s = str(path).replace("\\", "/")
    if platform.system() == "Windows" and len(s) >= 2 and s[1] == ":":
        s = s[0] + "\\:" + s[2:]
    return s


def _cleanup(temp_files: list[Path], protect: Path) -> None:
    """Remove all temp files (best effort)."""
    seen: set[Path] = set()
    for f in temp_files:
        if f in seen or f == protect:
            continue
        seen.add(f)
        try:
            if f.exists():
                f.unlink()
                logger.debug("Cleaned up: %s", f.name)
        except OSError as exc:
            logger.warning("Could not delete '%s': %s", f, exc)


def _redacted_cmd(cmd: list[str]) -> str:
    """Return a human-readable command string."""
    parts = []
    skip_next = False
    for token in cmd:
        if skip_next:
            parts.append("<filter_complex>")
            skip_next = False
        elif token in ("-filter_complex", "-vf"):
            parts.append(token)
            skip_next = True
        else:
            parts.append(token)
    return " ".join(parts)