"""
services/video_composer.py
──────────────────────────
Step 5 of the StoryForge pipeline.

OPTIMIZED FOR RENDER.COM FREE TIER (512MB RAM limit).

Produces a single episode.mp4 at 1280×720, H.264 + AAC, through sequential
FFmpeg passes with aggressive memory management:

  Key optimizations:
  • FFMPEG_THREADS = 2  → caps CPU/RAM per subprocess
  • Recursive 5-clip batching  → at most 5 decoders in memory at once
  • Subtitle burn-in merged into final xfade pass  → saves one full re-encode
  • filter_complex written to a script file  → avoids command-line length limits
  • Temp file tracking + cleanup in finally block

  Pass 1 — Ken Burns clip per scene (sequential, one at a time)
  ───────────────────────────────────────────────────────────────
  For each scene: build Ken Burns MP4 with audio, save to _clip_NNN.mp4

  Pass 2 — Recursive batch xfade concatenation
  ──────────────────────────────────────────────
  _concat_with_xfade() groups clips into batches of ≤5, concatenates each
  batch with xfade+acrossfade, then recursively merges batch outputs.
  Subtitles are appended to the final xfade filter graph (no extra pass).

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

# H.264 encoding
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "fast"
VIDEO_CRF = 23
FFMPEG_THREADS = "2"
VIDEO_BUFSIZE = "512k"

# AAC audio
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

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
    Assemble episode.mp4 from scenes using sequential, memory-efficient processing
    and recursive batching to avoid OOM crashes on free-tier environments.

    Pipeline:
      1. Per-scene Ken Burns clip (sequentially built, saved as intermediate mp4)
      2. Recursive batch xfade concatenation of all clips, merging subtitles in final pass
      3. Cleanup of all temp files

    Args:
        job_id:  Unique job identifier.
        scenes:  Serialised Scene dicts; image_path + audio_path must be set.

    Returns:
        See module docstring.
    """
    logger.info("Video composition START | job=%s | scenes=%d | mode=recursive-batch-xfade", job_id, len(scenes))

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
        # ── Pass 1: Ken Burns clips (sequentially built) ──────────────────────
        clip_infos: list[_ClipInfo] = []

        for i, scene in enumerate(valid_scenes):
            clip_path = final_dir / f"_clip_{i:03d}.mp4"
            temp_files.append(clip_path)

            duration = await _probe_audio_duration(scene.audio_path)

            logger.info(
                "Pass 1 | scene %03d | duration=%.2fs | image=%s",
                scene.scene_number,
                duration,
                Path(scene.image_path).name,
            )

            await _build_ken_burns_clip(scene, duration, clip_path)
            clip_infos.append(_ClipInfo(
                scene_number=scene.scene_number,
                path=clip_path,
                duration=duration,
            ))

        # ── Pass 2 & 3: Concatenation and subtitle burn-in ───────────────────
        logger.info(
            "Pass 2 & 3 | Concatenating %d clips with recursive batching and burning subtitles ...",
            len(clip_infos)
        )
        await _concat_with_xfade(
            clips=clip_infos,
            out_path=out_path,
            burn_subtitles_path=srt_path if has_subtitles else None,
            temp_files=temp_files,
        )

        # ── Measure output duration ───────────────────────────────────────────
        total_duration = sum(ci.duration for ci in clip_infos)
        # Subtract the overlap consumed by xfade transitions
        if len(clip_infos) > 1:
            total_duration -= XFADE_DURATION * (len(clip_infos) - 1)

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
                "scene_count":   len(clip_infos),
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
            "error": f"Video composition failed: FFmpeg failed (exit code {exc.returncode}). See ffmpeg_stderr for details.",
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
        "-threads", FFMPEG_THREADS,
        # ── Memory-conscious decoding ──────────────────────────────
        # Limit input buffer
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
        # ── Encoding ───────────────────────────────────────────────
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
# Pass 2 — Direct Concatenation (up to 5 clips)
# ─────────────────────────────────────────────────────────────────────────────


async def _concat_with_xfade_direct(
    clips: list[_ClipInfo],
    out_path: Path,
    burn_subtitles_path: Path | None = None,
    temp_files: list[Path] | None = None,
) -> None:
    """
    Concatenate N scene clips directly with xfade (video) + acrossfade (audio)
    transitions. Optional subtitles burn-in can be combined into this pass.
    """
    n = len(clips)
    assert n >= 2, "Need at least 2 clips for xfade."

    inputs = []
    for c in clips:
        inputs.extend(["-i", str(c.path)])

    v_filters = []
    a_filters = []

    cumulative_offset: float = 0.0  # tracks the xfade offset for each transition

    v_final_label = "[vtemp]" if (burn_subtitles_path and burn_subtitles_path.exists()) else "[vout]"

    for i in range(n - 1):
        # Offset = cumulative clip durations up to clip[i] minus overlaps used so far
        cumulative_offset += clips[i].duration - XFADE_DURATION

        if i == 0:
            v_in_a = "[0:v]"
            v_in_b = "[1:v]"
            a_in_a = "[0:a]"
            a_in_b = "[1:a]"
        else:
            v_in_a = f"[v{i - 1}{i}]"   # output of previous xfade
            v_in_b = f"[{i + 1}:v]"
            a_in_a = f"[a{i - 1}{i}]"
            a_in_b = f"[{i + 1}:a]"

        # Label for this xfade output
        if i == n - 2:
            # Last transition — use final label names
            v_out = v_final_label
            a_out = "[aout]"
        else:
            v_out = f"[v{i}{i + 1}]"
            a_out = f"[a{i}{i + 1}]"

        v_filters.append(
            f"{v_in_a}{v_in_b}"
            f"xfade=transition=fade:"
            f"duration={XFADE_DURATION:.3f}:"
            f"offset={cumulative_offset:.3f}"
            f"{v_out}"
        )
        a_filters.append(
            f"{a_in_a}{a_in_b}"
            f"acrossfade=d={XFADE_DURATION:.3f}"
            f"{a_out}"
        )

    filter_complex = ";".join(v_filters + a_filters)

    if burn_subtitles_path and burn_subtitles_path.exists():
        srt_escaped = _escape_srt_path(burn_subtitles_path)
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
        filter_complex += f";[vtemp]subtitles='{srt_escaped}':force_style='{subtitle_style}'[vout]"

    # Write filter graph to a temporary script file to avoid Windows command line length limit
    filter_script_path = out_path.parent / f"filter_complex_{out_path.stem}.txt"
    filter_script_path.write_text(filter_complex, encoding="utf-8")
    if temp_files is not None:
        temp_files.append(filter_script_path)

    cmd = [
        "ffmpeg", "-y",
        "-threads", FFMPEG_THREADS,
        *inputs,                    # -i _clip_000.mp4 -i _clip_001.mp4 ...
        # ── filter graph ────────────────────────────────────────────
        "-filter_complex_script", str(filter_script_path),
        # ── stream mapping ──────────────────────────────────────────
        "-map", "[vout]",
        "-map", "[aout]",
        # ── encoding ────────────────────────────────────────────────
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-bufsize", VIDEO_BUFSIZE,
        "-maxrate", "3000k",
        "-c:a", AUDIO_CODEC,
        "-b:a", AUDIO_BITRATE,
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]

    logger.debug("Direct concat cmd: %s", _redacted_cmd(cmd))

    try:
        await _run_ffmpeg(cmd, timeout_secs=FFMPEG_TIMEOUT_SECS)
    finally:
        if filter_script_path.exists():
            try:
                filter_script_path.unlink()
            except OSError as e:
                logger.warning("Could not delete temporary filter complex script '%s': %s", filter_script_path, e)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2b — Recursive batch concatenation wrapper
# ─────────────────────────────────────────────────────────────────────────────


async def _concat_with_xfade(
    clips: list[_ClipInfo],
    out_path: Path,
    burn_subtitles_path: Path | None = None,
    temp_files: list[Path] | None = None,
) -> None:
    """
    Concatenate a list of clips recursively in batches of at most 5 to avoid OOM
    due to too many concurrent video decoders.
    """
    n = len(clips)
    if n == 0:
        raise ValueError("Cannot concatenate 0 clips.")

    if n == 1:
        if burn_subtitles_path and burn_subtitles_path.exists():
            await _burn_subtitles(clips[0].path, burn_subtitles_path, out_path)
        else:
            shutil.copy2(clips[0].path, out_path)
        return

    if n <= 5:
        await _concat_with_xfade_direct(clips, out_path, burn_subtitles_path, temp_files)
        return

    batch_size = 5
    grouped_clips: list[_ClipInfo] = []
    temp_dir = out_path.parent

    for idx, i in enumerate(range(0, n, batch_size)):
        chunk = clips[i : i + batch_size]
        group_path = temp_dir / f"_group_{idx:03d}_{out_path.name}"
        if temp_files is not None:
            temp_files.append(group_path)

        group_duration = sum(c.duration for c in chunk) - XFADE_DURATION * (len(chunk) - 1)

        logger.info(
            "Batching intermediate group %d (%d clips, duration=%.2fs) ...",
            idx, len(chunk), group_duration
        )

        await _concat_with_xfade(chunk, group_path, burn_subtitles_path=None, temp_files=temp_files)

        grouped_clips.append(_ClipInfo(
            scene_number=idx,
            path=group_path,
            duration=group_duration
        ))

    await _concat_with_xfade(grouped_clips, out_path, burn_subtitles_path, temp_files=temp_files)

    # Clean up intermediate group files
    for gc in grouped_clips:
        try:
            if gc.path.exists():
                gc.path.unlink()
        except OSError as e:
            logger.warning("Could not delete intermediate group file '%s': %s", gc.path, e)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3 — Subtitle burn-in (fallback for n=1)
# ─────────────────────────────────────────────────────────────────────────────


async def _burn_subtitles(
    video_path: Path,
    srt_path: Path,
    out_path: Path,
) -> None:
    """
    Burn episode.srt into the video using FFmpeg's libass subtitles filter.
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
        "-threads", FFMPEG_THREADS,

        "-i", str(video_path),
        "-vf", vf,
        "-c:v", VIDEO_CODEC,
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),
        "-bufsize", VIDEO_BUFSIZE,
        "-maxrate", "3000k",
        "-c:a", "copy",
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