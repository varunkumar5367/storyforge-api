"""
services/video_composer.py
──────────────────────────
Step 5 of the StoryForge pipeline.

Produces a single episode.mp4 at 1280×720, H.264 + AAC, through four
sequential FFmpeg passes:

  Pass 1 — Ken Burns clip per scene
  ───────────────────────────────────
  For each scene (image + audio):
    • Loop the PNG as a static video source.
    • Apply zoompan filter for a slow Ken Burns zoom-in.
    • Trim to the exact audio length (probed via ffprobe).
    • Encode as H.264 / AAC into a temp clip.

    ffmpeg -y -loop 1 -framerate 25 -i scene.png \\
           -i scene.mp3 \\
           -filter_complex "[0:v]scale=1280:720,
               zoompan=z='min(zoom+0.0008,1.4)':
                        x='iw/2-(iw/zoom/2)':
                        y='ih/2-(ih/zoom/2)':
                        d=<frames>:s=1280x720:fps=25[v]" \\
           -map [v] -map 1:a \\
           -t <duration> \\
           -c:v libx264 -preset fast -crf 23 \\
           -c:a aac -b:a 192k \\
           -pix_fmt yuv420p \\
           _clip_000.mp4

  Pass 2 — xfade crossfade concatenation
  ──────────────────────────────────────
  Chain all clips with xfade (video) + acrossfade (audio) transitions.

  For N=2 clips:
    ffmpeg -y -i _clip_000.mp4 -i _clip_001.mp4 \\
           -filter_complex "
             [0:v][1:v]xfade=transition=fade:duration=0.5:offset=<d0-0.5>[vout];
             [0:a][1:a]acrossfade=d=0.5[aout]" \\
           -map [vout] -map [aout] \\
           -c:v libx264 -preset fast -crf 23 \\
           -c:a aac -b:a 192k -pix_fmt yuv420p \\
           _concat.mp4

  For N>2 clips the xfade/acrossfade chain is extended iteratively.

  Pass 3 — Subtitle burn-in
  ─────────────────────────
  Burn episode.srt subtitles using libass via the subtitles filter:

    ffmpeg -y -i _concat.mp4 \\
           -vf "subtitles='<episode.srt>':
                force_style='FontName=Arial,FontSize=22,
                             Bold=1,
                             PrimaryColour=&H00FFFFFF,
                             OutlineColour=&H00000000,
                             Outline=2,Shadow=1,
                             Alignment=2,MarginV=30'" \\
           -c:v libx264 -preset fast -crf 23 \\
           -c:a copy \\
           episode.mp4

  If no episode.srt is found, _concat.mp4 is renamed to episode.mp4 directly.

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
        "ffmpeg_stderr": "<last N chars of FFmpeg stderr>",   # always present on FFmpeg errors
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
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
# Constants
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_FPS: int = 25
OUTPUT_WIDTH: int = 1280
OUTPUT_HEIGHT: int = 720

# H.264 encoding settings
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "fast"
VIDEO_CRF = 23              # 18=near-lossless, 28=smaller file; 23 is a good default

# AAC audio encoding
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"

# Ken Burns effect parameters
KB_ZOOM_RATE: float = 0.0008   # zoom increment per frame (smaller = gentler)
KB_MAX_ZOOM: float = 1.40      # maximum zoom factor (1.0 = no zoom)

# xfade / acrossfade transition duration (seconds)
XFADE_DURATION: float = 0.5

# ffprobe fallback duration when probing fails
FALLBACK_DURATION_SECS: float = 10.0

# How many chars of FFmpeg stderr to include in error responses
STDERR_TAIL_CHARS: int = 3000

# ─────────────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _ClipInfo:
    """Metadata about one successfully-built scene clip."""
    scene_number: int
    path: Path
    duration: float        # probed audio duration (seconds)


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
    Assemble episode.mp4 from the scene images, audio files, and subtitles.

    The four-pass pipeline is:
      1. Ken Burns clip per scene (zoompan + audio mux).
      2. xfade crossfade concatenation of all clips.
      3. Subtitle burn-in from episode.srt (if present).
      4. Cleanup of all temp files.

    Args:
        job_id:  Unique job identifier.
        scenes:  Serialised Scene dicts; image_path + audio_path must be set.

    Returns:
        See module docstring.
    """
    logger.info("Video composition START | job=%s | scenes=%d", job_id, len(scenes))

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

    # Determine master SRT path (built by subtitle_generator)
    srt_path = master_srt_path(job_id)
    has_subtitles = srt_path.exists()
    if not has_subtitles:
        logger.warning(
            "Master SRT not found at '%s' — video will have no subtitles.", srt_path
        )

    # Temp paths live alongside the final output
    clip_paths: list[Path] = []
    temp_files: list[Path] = list(placeholder_files)  # tracked for cleanup

    try:
        # ── Pass 1: Ken Burns clips ───────────────────────────────────────────
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
            clip_paths.append(clip_path)

        # ── Pass 2 & 3: Concatenation and subtitle burn-in ───────────────────
        logger.info(
            "Pass 2 & 3 | Concatenating %d clips and burning subtitles ...",
            len(clip_infos)
        )
        await _concat_with_xfade(
            clips=clip_infos,
            out_path=out_path,
            burn_subtitles_path=srt_path if has_subtitles else None
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
            "error": f"FFmpeg failed (exit code {exc.returncode}). See ffmpeg_stderr for details.",
            "ffmpeg_stderr": exc.stderr[-STDERR_TAIL_CHARS:],
            "ffmpeg_cmd": " ".join(exc.cmd),
        }

    except Exception as exc:
        logger.exception("Unexpected error during video composition [job=%s].", job_id)
        return {
            "success": False,
            "error": f"Unexpected error: {exc}",
            "ffmpeg_stderr": "",
        }

    finally:
        # Always clean up temp files (best-effort)
        _cleanup(temp_files, out_path)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1 — Ken Burns clip
# ─────────────────────────────────────────────────────────────────────────────


async def _build_ken_burns_clip(
    scene: Scene,
    duration: float,
    out_path: Path,
) -> None:
    """
    Build one scene clip with a slow Ken Burns zoom-in effect.

    FFmpeg command (annotated):

        ffmpeg -y                           # overwrite output
               -loop 1                     # loop the PNG as a video source
               -framerate 25               # input framerate for the looped image
               -i scene_001.png            # still image input
               -i scene_001.mp3            # audio input
               -filter_complex "
                 [0:v]scale=1280:720,      # ensure correct canvas size first
                 zoompan=
                   z='min(zoom+0.0008,1.4)':   # zoom expression: grows each frame
                   x='iw/2-(iw/zoom/2)':       # pan x: keep subject centred
                   y='ih/2-(ih/zoom/2)':       # pan y: keep subject centred
                   d=<frames>:                  # total frames = duration × fps
                   s=1280x720:                  # output size
                   fps=25[v]"                   # output framerate
               -map [v]                    # map processed video stream
               -map 1:a                   # map audio stream from MP3
               -t <duration>              # hard trim to exact audio length
               -c:v libx264               # H.264 video codec
               -preset fast               # encoding speed/quality trade-off
               -crf 23                    # quality (lower = better)
               -c:a aac                   # AAC audio codec
               -b:a 192k                  # audio bitrate
               -pix_fmt yuv420p           # required for broad player compat
               _clip_000.mp4

    The zoompan `z` expression increments the zoom multiplier by KB_ZOOM_RATE
    each frame, clamped at KB_MAX_ZOOM so the image never over-zooms.
    """
    frames = max(1, int(duration * OUTPUT_FPS))

    camera_instr = getattr(scene, "camera", "slow_zoom_in") or "slow_zoom_in"
    camera_instr = camera_instr.lower().strip()

    # Determine zoom & pan expressions based on the camera instruction
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
        # ── inputs ──────────────────────────────────────────────────
        "ffmpeg", "-y",
        "-loop", "1",               # loop the still image
        "-framerate", str(OUTPUT_FPS),  # input framerate for the looped PNG
        "-i", str(scene.image_path),     # [0] video: still image (or placeholder)
        "-i", str(scene.audio_path),     # [1] audio: MP3 narration
        # ── filter graph ────────────────────────────────────────────
        "-filter_complex", filter_complex,
        # ── stream mapping ──────────────────────────────────────────
        "-map", "[v]",              # processed video
        "-map", "1:a",              # raw audio from MP3
        # ── duration ────────────────────────────────────────────────
        "-t", f"{duration:.3f}",    # hard trim: ensures clip = audio length
        # ── video encoding ──────────────────────────────────────────
        "-c:v", VIDEO_CODEC,        # libx264
        "-preset", VIDEO_PRESET,    # fast
        "-crf", str(VIDEO_CRF),     # 23
        # ── audio encoding ──────────────────────────────────────────
        "-c:a", AUDIO_CODEC,        # aac
        "-b:a", AUDIO_BITRATE,      # 192k
        # ── pixel format ────────────────────────────────────────────
        "-pix_fmt", "yuv420p",      # required for QuickTime / most players
        str(out_path),
    ]

    logger.debug(
        # Exact FFmpeg command:
        # ffmpeg -y -loop 1 -framerate 25 -i <img> -i <aud>
        #   -filter_complex "[0:v]scale=1280:720,zoompan=z='min(zoom+0.0008,1.4)':
        #                    x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=<N>:s=1280x720:fps=25[v]"
        #   -map [v] -map 1:a -t <dur>
        #   -c:v libx264 -preset fast -crf 23 -c:a aac -b:a 192k -pix_fmt yuv420p <out>
        "Ken Burns cmd: %s",
        _redacted_cmd(cmd),
    )

    await _run_ffmpeg(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 — xfade concatenation
# ─────────────────────────────────────────────────────────────────────────────


async def _concat_with_xfade_direct(
    clips: list[_ClipInfo],
    out_path: Path,
    burn_subtitles_path: Path | None = None,
) -> None:
    """
    Concatenate N scene clips directly with xfade (video) + acrossfade (audio)
    transitions. Optional subtitles burn-in can be combined into this pass.
    """
    n = len(clips)
    assert n >= 2, "Need at least 2 clips for xfade."

    # Build -i flags
    inputs: list[str] = []
    for ci in clips:
        inputs += ["-i", str(ci.path)]

    # Build filter graph
    v_filters: list[str] = []
    a_filters: list[str] = []

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
            # ffmpeg xfade: fade transition at the computed offset
            f"{v_in_a}{v_in_b}"
            f"xfade=transition=fade:"
            f"duration={XFADE_DURATION:.3f}:"
            f"offset={cumulative_offset:.3f}"
            f"{v_out}"
        )
        a_filters.append(
            # ffmpeg acrossfade: audio crossfade matching xfade duration
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

    cmd = [
        # ── inputs ──────────────────────────────────────────────────
        "ffmpeg", "-y",
        *inputs,                    # -i _clip_000.mp4 -i _clip_001.mp4 ...
        # ── filter graph ────────────────────────────────────────────
        "-filter_complex_script", str(filter_script_path),
        # Example for 2 clips:
        #   [0:v][1:v]xfade=transition=fade:duration=0.5:offset=<d0-0.5>[vout];
        #   [0:a][1:a]acrossfade=d=0.5[aout]
        # ── stream mapping ──────────────────────────────────────────
        "-map", "[vout]",
        "-map", "[aout]",
        # ── encoding ────────────────────────────────────────────────
        "-c:v", VIDEO_CODEC,        # libx264
        "-preset", VIDEO_PRESET,    # fast
        "-crf", str(VIDEO_CRF),     # 23
        "-c:a", AUDIO_CODEC,        # aac
        "-b:a", AUDIO_BITRATE,      # 192k
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]

    logger.debug(
        # Exact FFmpeg command (2-clip example):
        # ffmpeg -y -i _clip_000.mp4 -i _clip_001.mp4
        #   -filter_complex_script filter_complex_concat.txt
        #   -map [vout] -map [aout]
        #   -c:v libx264 -preset fast -crf 23 -c:a aac -b:a 192k -pix_fmt yuv420p _concat.mp4
        "xfade concat cmd: %s",
        _redacted_cmd(cmd),
    )

    try:
        await _run_ffmpeg(cmd)
    finally:
        if filter_script_path.exists():
            try:
                filter_script_path.unlink()
            except OSError as e:
                logger.warning("Could not delete temporary filter complex script '%s': %s", filter_script_path, e)


async def _concat_with_xfade(
    clips: list[_ClipInfo],
    out_path: Path,
    burn_subtitles_path: Path | None = None,
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
        await _concat_with_xfade_direct(clips, out_path, burn_subtitles_path)
        return

    batch_size = 5
    grouped_clips: list[_ClipInfo] = []
    temp_dir = out_path.parent

    for idx, i in enumerate(range(0, n, batch_size)):
        chunk = clips[i : i + batch_size]
        group_path = temp_dir / f"_group_{idx:03d}_{out_path.stem}.mp4"
        group_duration = sum(c.duration for c in chunk) - XFADE_DURATION * (len(chunk) - 1)

        logger.info(
            "Batching intermediate group %d (%d clips, duration=%.2fs) ...",
            idx, len(chunk), group_duration
        )

        await _concat_with_xfade(chunk, group_path, burn_subtitles_path=None)

        grouped_clips.append(_ClipInfo(
            scene_number=idx,
            path=group_path,
            duration=group_duration
        ))

    await _concat_with_xfade(grouped_clips, out_path, burn_subtitles_path)

    # Clean up intermediate group files
    for gc in grouped_clips:
        try:
            if gc.path.exists():
                gc.path.unlink()
        except OSError as e:
            logger.warning("Could not delete intermediate group file '%s': %s", gc.path, e)


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3 — subtitle burn-in
# ─────────────────────────────────────────────────────────────────────────────


async def _burn_subtitles(
    video_path: Path,
    srt_path: Path,
    out_path: Path,
) -> None:
    """
    Burn episode.srt into the video using FFmpeg's libass subtitles filter.

    Style tokens applied via force_style:
        FontName=Arial        — clean, widely available sans-serif font
        FontSize=22           — readable on 720p without obscuring too much
        Bold=1                — bold weight for contrast
        PrimaryColour=&H00FFFFFF — white text (AABBGGRR: alpha=00 fully opaque)
        OutlineColour=&H00000000 — black outline
        Outline=2             — 2-pixel outline thickness
        Shadow=1              — subtle drop shadow for depth
        Alignment=2           — bottom-centre (ASS alignment code 2)
        MarginV=30            — 30 px from bottom edge

    FFmpeg command:

        ffmpeg -y
               -i _concat.mp4
               -vf "subtitles='<episode.srt>':
                    force_style='FontName=Arial,FontSize=22,Bold=1,
                                 PrimaryColour=&H00FFFFFF,
                                 OutlineColour=&H00000000,
                                 Outline=2,Shadow=1,
                                 Alignment=2,MarginV=30'"
               -c:v libx264 -preset fast -crf 23
               -c:a copy
               episode.mp4

    Path escaping:
        The subtitles filter requires the SRT path to use forward slashes
        on all platforms, and Windows drive-letter colons to be escaped
        as '\\:' (e.g. C\\:/path/to/file.srt).
    """
    # Escape path for FFmpeg subtitles filter (cross-platform)
    srt_escaped = _escape_srt_path(srt_path)

    subtitle_style = (
        "FontName=Arial,"
        "FontSize=22,"
        "Bold=1,"
        "PrimaryColour=&H00FFFFFF,"    # white text, fully opaque
        "OutlineColour=&H00000000,"    # black outline
        "Outline=2,"                   # 2-pixel outline
        "Shadow=1,"                    # 1-pixel drop shadow
        "Alignment=2,"                 # bottom-centre
        "MarginV=30"                   # 30 px margin from bottom
    )
    vf = f"subtitles='{srt_escaped}':force_style='{subtitle_style}'"

    cmd = [
        # ── input ───────────────────────────────────────────────────
        "ffmpeg", "-y",
        "-i", str(video_path),      # concatenated video (no subtitles yet)
        # ── subtitle burn-in filter ──────────────────────────────────
        "-vf", vf,
        # subtitles='<srt>':force_style='FontName=Arial,FontSize=22,Bold=1,
        #            PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,
        #            Outline=2,Shadow=1,Alignment=2,MarginV=30'
        # ── video encoding (re-encode required for filter) ───────────
        "-c:v", VIDEO_CODEC,        # libx264
        "-preset", VIDEO_PRESET,    # fast
        "-crf", str(VIDEO_CRF),     # 23
        # ── audio (pass-through — already AAC from pass 2) ───────────
        "-c:a", "copy",             # copy audio stream, no re-encode
        str(out_path),
    ]

    logger.debug(
        # Exact FFmpeg command:
        # ffmpeg -y -i _concat.mp4
        #   -vf "subtitles='<srt>':force_style='...'"
        #   -c:v libx264 -preset fast -crf 23 -c:a copy episode.mp4
        "Subtitle burn cmd: %s",
        _redacted_cmd(cmd),
    )

    await _run_ffmpeg(cmd)


# ─────────────────────────────────────────────────────────────────────────────
# ffprobe — audio duration
# ─────────────────────────────────────────────────────────────────────────────


async def _probe_audio_duration(audio_path: str | None) -> float:
    """
    Probe the exact duration of an MP3 file using ffprobe.

    Command:
        ffprobe -v quiet -print_format json -show_streams <audio>

    Parses the first stream's "duration" field.
    Falls back to FALLBACK_DURATION_SECS on any error so the pipeline
    can continue even if a single probe fails.

    Args:
        audio_path: Path to the MP3 file (or None).

    Returns:
        Duration in seconds as a float.
    """
    if not audio_path or not Path(audio_path).exists():
        logger.warning(
            "Audio file missing or None ('%s') — using fallback duration %.1fs.",
            audio_path,
            FALLBACK_DURATION_SECS,
        )
        return FALLBACK_DURATION_SECS

    cmd = [
        # ffprobe -v quiet -print_format json -show_streams <audio>
        "ffprobe",
        "-v", "quiet",              # suppress banner/info output
        "-print_format", "json",    # output as JSON
        "-show_streams",            # include stream metadata (has "duration" field)
        str(audio_path),
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        if result.returncode != 0:
            err = result.stderr.decode(errors="replace")[-500:]
            logger.warning("ffprobe rc=%d for '%s': %s", result.returncode, audio_path, err)
            return FALLBACK_DURATION_SECS

        stdout = result.stdout

        info = json.loads(stdout.decode())
        streams = info.get("streams", [])

        if not streams:
            logger.warning("ffprobe: no streams found in '%s'.", audio_path)
            return FALLBACK_DURATION_SECS

        # Prefer the audio stream's duration; fall back to the first stream
        for stream in streams:
            if stream.get("codec_type") == "audio" and "duration" in stream:
                dur = float(stream["duration"])
                logger.debug("ffprobe: '%s' → %.3fs", Path(audio_path).name, dur)
                return max(0.1, dur)

        # No audio stream found — try "duration" on any stream
        dur = float(streams[0].get("duration", FALLBACK_DURATION_SECS))
        return max(0.1, dur)

    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        logger.warning("ffprobe parse error for '%s': %s", audio_path, exc)
        return FALLBACK_DURATION_SECS


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg subprocess runner
# ─────────────────────────────────────────────────────────────────────────────


async def _run_ffmpeg(cmd: list[str]) -> None:
    """
    Execute an FFmpeg command asynchronously via asyncio subprocess.

    Both stdout and stderr are captured.  On non-zero exit code an
    FFmpegError is raised containing the full stderr output so callers
    can include it in their error response.

    FFmpeg writes progress to stderr even on success, so we always
    collect it but only treat it as an error if returncode != 0.

    Args:
        cmd: Full FFmpeg command list (first element must be "ffmpeg").

    Raises:
        FFmpegError: If FFmpeg exits with a non-zero return code.
    """
    # Inject "-threads", "2" right after "ffmpeg" if not already present
    if cmd and cmd[0] == "ffmpeg" and "-threads" not in cmd:
        cmd = [cmd[0]] + ["-threads", "2"] + cmd[1:]

    try:
        out_path = Path(cmd[-1])
        stderr_log_path = out_path.parent / f"ffmpeg_{out_path.stem}_stderr.log"
    except Exception:
        import tempfile
        stderr_log_path = Path(tempfile.gettempdir()) / "ffmpeg_temp_stderr.log"

    logger.debug("Running: %s (logging stderr to %s)", " ".join(cmd[:8]) + " ...", stderr_log_path.name)

    try:
        with open(stderr_log_path, "w", encoding="utf-8", errors="replace") as f_err:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=f_err,
            )
        
        stderr_text = ""
        if stderr_log_path.exists():
            try:
                # Read last STDERR_TAIL_CHARS bytes
                with open(stderr_log_path, "r", encoding="utf-8", errors="replace") as f_read:
                    f_read.seek(0, 2)
                    size = f_read.tell()
                    seek_pos = max(0, size - STDERR_TAIL_CHARS)
                    f_read.seek(seek_pos)
                    stderr_text = f_read.read()
            except Exception as e:
                logger.warning("Could not read stderr log: %s", e)

        if result.returncode != 0:
            # Log the tail of stderr at ERROR level for immediate visibility
            logger.error(
                "FFmpeg FAILED | rc=%d | cmd=%s\nstderr (tail):\n%s",
                result.returncode,
                " ".join(cmd[:6]) + " ...",
                stderr_text[-1500:],
            )
            raise FFmpegError(cmd, result.returncode, stderr_text)

        # On success, log a short summary at DEBUG level
        logger.debug(
            "FFmpeg OK | rc=0 | output=%s",
            cmd[-1],  # last arg is always the output file
        )

    finally:
        # Clean up the stderr log file on success (or always, if we want to save space)
        if stderr_log_path.exists():
            try:
                stderr_log_path.unlink()
            except OSError as e:
                logger.warning("Could not delete temporary FFmpeg stderr log file: %s", e)



# ─────────────────────────────────────────────────────────────────────────────
# Validation and utilities
# ─────────────────────────────────────────────────────────────────────────────


def _filter_valid_scenes(
    scenes: list[dict[str, Any]],
    job_id: str,
    work_dir: Path,
    temp_files: list[Path],
) -> list[Scene]:
    """
    Return scenes that have narration audio on disk.

    Image files are optional — when a scene has audio but no image, a dark
    cinematic placeholder PNG is generated so FFmpeg can still build the clip.
    """
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
    """Return the first path that exists on disk (stored value or canonical)."""
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
    """Write a dark 1280×720 placeholder PNG for scenes without generated art."""
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
    """
    Produce an FFmpeg subtitles-filter-safe path string.

    Rules:
      • Use forward slashes on all platforms.
      • On Windows, escape the colon after the drive letter:
          C:/path/... → C\\:/path/...
        (FFmpeg's subtitles filter parses ':' as an option separator unless
        it is escaped.)

    Args:
        path: Absolute path to the SRT file.

    Returns:
        Escaped path string suitable for embedding in a -vf subtitles=... value.
    """
    # Normalise to forward slashes
    s = str(path).replace("\\", "/")

    # On Windows, escape the drive-letter colon: "C:/" → "C\\:/"
    if platform.system() == "Windows" and len(s) >= 2 and s[1] == ":":
        s = s[0] + "\\:" + s[2:]

    return s


def _cleanup(temp_files: list[Path], protect: Path) -> None:
    """
    Remove all temp files, skipping *protect* (the final output).
    Errors are logged but not re-raised.
    """
    seen: set[Path] = set()
    for f in temp_files:
        if f in seen or f == protect:
            continue
        seen.add(f)
        try:
            if f.exists():
                f.unlink()
                logger.debug("Cleaned up temp file: %s", f.name)
        except OSError as exc:
            logger.warning("Could not delete temp file '%s': %s", f, exc)


def _redacted_cmd(cmd: list[str]) -> str:
    """Return a human-readable command string, truncating long filter_complex values."""
    parts = []
    skip_next = False
    for token in cmd:
        if skip_next:
            parts.append("<filter_complex>")
            skip_next = False
        elif token == "-filter_complex":
            parts.append(token)
            skip_next = True
        else:
            parts.append(token)
    return " ".join(parts)
