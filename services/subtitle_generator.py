"""
services/subtitle_generator.py
────────────────────────────────
Step 4 of the StoryForge pipeline.

Responsibilities
────────────────
• For each scene that has an audio_path on disk:
    - Call Groq Whisper (whisper-large-v3) with response_format="verbose_json"
      and timestamp_granularities=["word", "segment"].
    - Convert word-level timestamps → subtitle cues grouped into readable lines
      (≤ MAX_WORDS_PER_LINE words, ≤ MAX_LINE_CHARS chars).
    - If word-level data is absent, fall back to segment-level timestamps.
    - Write the per-scene SRT to:
        output/{job_id}/subtitles/scene_{n:03d}.srt

• After all scenes are processed, build a MASTER SRT file by:
    - Taking each scene's cues and shifting their timestamps by the
      cumulative audio offset of all preceding scenes
      (offset = sum of duration_hint of preceding scenes).
    - Re-numbering all cues sequentially from 1.
    - Writing the merged result to:
        output/{job_id}/subtitles/episode.srt

Return contract
───────────────
Success:
    {
        "success": True,
        "data": {
            "scenes":          [<updated Scene dicts — subtitle_path set>],
            "subtitle_paths":  ["<abs-path-1>", ...],    # per-scene SRTs
            "master_srt_path": "<abs-path>",             # episode.srt
            "failed_scenes":   [<scene_numbers>],
        }
    }
    Individual scene failures are non-fatal; master SRT is built from
    whichever scenes succeeded.

Failure (if every single scene fails):
    {
        "success": False,
        "error": "All scenes failed transcription."
    }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models.scene import Scene
from utils.groq_client import transcribe_audio
from utils.file_handler import (
    master_srt_path,
    scene_subtitle_path,
    write_text,
)

logger = logging.getLogger("storyforge.subtitle_generator")

# ─────────────────────────────────────────────────────────────────────────────
# Formatting constants
# ─────────────────────────────────────────────────────────────────────────────

# Word-grouping rules for subtitle cues
MAX_WORDS_PER_LINE: int = 8    # max words on a single subtitle line
MAX_LINE_CHARS: int = 42       # max characters per subtitle line (Netflix standard)
MAX_LINES_PER_CUE: int = 2     # max lines per SRT cue block

# Minimum cue duration — prevents zero-duration cues from confusing players
MIN_CUE_DURATION_SECS: float = 0.5

# Brief pause between Whisper API calls to avoid rate-limit spikes
INTER_CALL_DELAY_SECS: float = 0.8

# ─────────────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Cue:
    """
    One SRT subtitle cue: a time window and its display text.
    start/end are in seconds (float); text is already line-broken.
    """
    index: int           # 1-based cue number (within its SRT file)
    start: float         # cue start, seconds from file start
    end: float           # cue end, seconds from file start
    text: str            # display text (may contain \\n for multi-line)

    def to_srt_block(self) -> str:
        """Render this cue as a valid SRT block (no trailing newline)."""
        return (
            f"{self.index}\n"
            f"{_fmt(self.start)} --> {_fmt(self.end)}\n"
            f"{self.text}"
        )


@dataclass
class _SceneSubtitleResult:
    """Outcome of transcribing and formatting one scene's audio."""
    scene_number: int
    success: bool
    cues: list[_Cue] = field(default_factory=list)  # relative timestamps
    path: str | None = None                          # saved SRT path
    audio_duration: float = 0.0                      # duration_hint used for offset
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def generate_subtitles(
    job_id: str,
    scenes: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Transcribe each scene's MP3, produce per-scene SRTs, and merge into
    a master episode.srt with correct cumulative timestamps.

    Args:
        job_id:  Unique job identifier.
        scenes:  Serialised Scene dicts; must have audio_path + duration_hint.

    Returns:
        See module docstring for full return contract.
    """
    logger.info(
        "Subtitle generation START | job=%s | scenes=%d", job_id, len(scenes)
    )

    # ── Phase 1: Transcribe each scene ───────────────────────────────────────
    results: list[_SceneSubtitleResult] = []

    for idx, raw_scene in enumerate(scenes):
        scene = Scene(**raw_scene)

        # Inter-call delay (skip before first call)
        if idx > 0:
            await asyncio.sleep(INTER_CALL_DELAY_SECS)

        result = await _transcribe_scene(job_id, scene)
        results.append(result)

    # ── Phase 2: Write per-scene SRT files ───────────────────────────────────
    # (write_text is async; we do them sequentially to keep disk ops simple)
    subtitle_paths: list[str] = []
    failed_scenes: list[int] = []
    updated_scenes: list[dict] = []
    result_map: dict[int, _SceneSubtitleResult] = {
        r.scene_number: r for r in results
    }

    for raw_scene in scenes:
        scene = Scene(**raw_scene)
        res = result_map.get(scene.scene_number)

        if res and res.success and res.path:
            scene.subtitle_path = res.path
            subtitle_paths.append(res.path)
        else:
            failed_scenes.append(scene.scene_number)
            logger.warning(
                "Scene %03d subtitle FAILED: %s",
                scene.scene_number,
                res.error if res else "no result",
            )

        updated_scenes.append(scene.model_dump())

    # ── Phase 3: Build & write master episode.srt ─────────────────────────────
    master_path: str | None = None
    successful_results = [r for r in results if r.success]

    if successful_results:
        master_srt_content = _build_master_srt(successful_results, scenes)
        master_out = master_srt_path(job_id)
        try:
            await write_text(master_out, master_srt_content)
            master_path = str(master_out)
            logger.info(
                "Master SRT saved → %s (%d cues)",
                master_out.name,
                master_srt_content.count("\n\n") + 1,
            )
        except OSError as exc:
            logger.error("Failed to write master SRT: %s", exc)
    else:
        logger.warning("No successful scenes — master SRT not created.")

    succeeded = len(subtitle_paths)
    total = len(scenes)
    logger.info(
        "Subtitle generation DONE | job=%s | %d/%d succeeded | %d failed",
        job_id,
        succeeded,
        total,
        len(failed_scenes),
    )

    if succeeded == 0:
        return {
            "success": False,
            "error": "All scenes failed transcription — no subtitles generated.",
        }

    return {
        "success": True,
        "data": {
            "scenes":          updated_scenes,
            "subtitle_paths":  subtitle_paths,
            "master_srt_path": master_path,
            "failed_scenes":   failed_scenes,
        },
    }


def _parse_srt_to_cues(srt_content: str) -> list[_Cue]:
    import re
    cues = []
    blocks = srt_content.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            try:
                idx = int(lines[0].strip())
                time_match = re.match(
                    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})",
                    lines[1].strip()
                )
                if time_match:
                    g = time_match.groups()
                    start = int(g[0])*3600 + int(g[1])*60 + int(g[2]) + int(g[3])/1000.0
                    end = int(g[4])*3600 + int(g[5])*60 + int(g[6]) + int(g[7])/1000.0
                    text = "\n".join(lines[2:])
                    cues.append(_Cue(index=idx, start=start, end=end, text=text))
            except Exception:
                continue
    return cues


async def generate_subtitle_for_scene(
    job_id: str,
    scene: dict[str, Any],
) -> tuple[dict[str, Any], _SceneSubtitleResult | None]:
    """
    Transcribe a single scene's audio and return the updated scene dict.

    Returns:
        (updated_scene_dict, subtitle_result)
        subtitle_result is None when transcription failed entirely.
    """
    scene_obj = Scene(**scene)

    out_path = scene_subtitle_path(job_id, scene_obj.scene_number)
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("Scene %03d | Subtitle file already exists on disk, skipping transcription: %s", scene_obj.scene_number, out_path)
        scene_obj.subtitle_path = str(out_path)
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                content = f.read()
            cues = _parse_srt_to_cues(content)
            res = _SceneSubtitleResult(
                scene_number=scene_obj.scene_number,
                success=True,
                cues=cues,
                path=str(out_path),
                audio_duration=scene_obj.duration_hint or 0.0,
            )
            return scene_obj.model_dump(), res
        except Exception as e:
            logger.warning("Failed to parse existing SRT for scene %03d: %s. Regenerating.", scene_obj.scene_number, e)

    result = await _transcribe_scene(job_id, scene_obj)

    if result.success and result.path:
        scene_obj.subtitle_path = result.path
    else:
        logger.warning(
            "Scene %03d subtitle FAILED: %s",
            scene_obj.scene_number,
            result.error or "unknown",
        )

    return scene_obj.model_dump(), result if result.success else None


async def finalize_master_subtitles(
    job_id: str,
    scenes: list[dict[str, Any]],
    subtitle_results: list[_SceneSubtitleResult],
) -> str | None:
    """
    Build and write the master episode.srt from per-scene transcription results.

    Returns the absolute path to the master SRT, or None if no scenes succeeded.
    """
    successful = [r for r in subtitle_results if r.success]
    if not successful:
        logger.warning("No successful subtitle scenes — master SRT not created.")
        return None

    master_srt_content = _build_master_srt(successful, scenes)
    master_out = master_srt_path(job_id)
    try:
        await write_text(master_out, master_srt_content)
        logger.info(
            "Master SRT saved → %s (%d cues)",
            master_out.name,
            master_srt_content.count("\n\n") + 1,
        )
        return str(master_out)
    except OSError as exc:
        logger.error("Failed to write master SRT: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Scene-level transcription
# ─────────────────────────────────────────────────────────────────────────────


async def _transcribe_scene(
    job_id: str, scene: Scene
) -> _SceneSubtitleResult:
    """
    Transcribe one scene's MP3, convert to SRT cues, and save the file.

    Word-level timestamps (from Groq Whisper verbose_json + ["word","segment"])
    are the primary source.  If the response omits words (Groq sometimes does
    for very short audio), segment-level timestamps are used as a fallback.
    """
    # Guard: audio file must exist
    if not scene.audio_path or not Path(scene.audio_path).exists():
        return _SceneSubtitleResult(
            scene_number=scene.scene_number,
            success=False,
            error=(
                f"No audio file at '{scene.audio_path}'"
                if scene.audio_path
                else "audio_path not set"
            ),
        )

    # ── Whisper API call ──────────────────────────────────────────────────────
    logger.info(
        "Transcribing scene %03d | %s",
        scene.scene_number,
        Path(scene.audio_path).name,
    )

    try:
        raw = await transcribe_audio(
            audio_path=scene.audio_path,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    except FileNotFoundError as exc:
        return _SceneSubtitleResult(
            scene_number=scene.scene_number,
            success=False,
            error=str(exc),
        )
    except Exception as exc:
        logger.exception("Whisper call failed for scene %03d.", scene.scene_number)
        return _SceneSubtitleResult(
            scene_number=scene.scene_number,
            success=False,
            error=f"Whisper API error: {exc}",
        )

    # ── Build cues from word or segment data ──────────────────────────────────
    cues = _build_cues(raw)

    if not cues:
        # Last-resort: build a single cue from the full transcript text
        full_text = (raw.get("text") or "").strip()
        duration = scene.duration_hint or 10.0
        if full_text:
            cues = [_Cue(index=1, start=0.0, end=duration, text=full_text)]
        else:
            return _SceneSubtitleResult(
                scene_number=scene.scene_number,
                success=False,
                error="Whisper returned no text and no cues.",
            )

    # ── Write per-scene SRT ───────────────────────────────────────────────────
    srt_content = _cues_to_srt(cues)
    out_path = scene_subtitle_path(job_id, scene.scene_number)

    try:
        await write_text(out_path, srt_content)
    except OSError as exc:
        return _SceneSubtitleResult(
            scene_number=scene.scene_number,
            success=False,
            error=f"Disk write failed: {exc}",
        )

    logger.info(
        "Scene %03d SRT saved → %s (%d cues)",
        scene.scene_number,
        out_path.name,
        len(cues),
    )

    return _SceneSubtitleResult(
        scene_number=scene.scene_number,
        success=True,
        cues=cues,
        path=str(out_path),
        audio_duration=scene.duration_hint or _infer_duration(cues),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Cue construction — word-level (primary) and segment-level (fallback)
# ─────────────────────────────────────────────────────────────────────────────


def _build_cues(raw: dict) -> list[_Cue]:
    """
    Convert a Groq Whisper verbose_json response into a list of _Cue objects
    with relative timestamps (seconds from the start of this audio file).

    Strategy
    ────────
    1. If "words" key is present and non-empty → group words into display
       lines using the word-grouping algorithm.
    2. Else if "segments" key is present and non-empty → convert each
       segment directly to one cue.
    3. Else → return empty list (caller falls back to plain text).

    Groq word dict schema:
        { "word": str, "start": float, "end": float }

    Groq segment dict schema:
        { "id": int, "start": float, "end": float, "text": str, ... }
    """
    words: list[dict] = raw.get("words") or []
    segments: list[dict] = raw.get("segments") or []

    if words:
        logger.debug("Using word-level timestamps (%d words).", len(words))
        return _words_to_cues(words)

    if segments:
        logger.debug("Using segment-level timestamps (%d segments).", len(segments))
        return _segments_to_cues(segments)

    return []


def _words_to_cues(words: list[dict]) -> list[_Cue]:
    """
    Group individual word timestamps into subtitle cues.

    Grouping rules (applied in order — first rule that triggers starts a new cue):
      1. Adding the next word would exceed MAX_WORDS_PER_LINE words on the
         current line.
      2. Adding the next word would exceed MAX_LINE_CHARS chars on the
         current line.
      3. A second line is already full (MAX_LINES_PER_CUE lines reached).

    Each resulting cue spans from the start of its first word to the end of
    its last word.  If end - start < MIN_CUE_DURATION_SECS the end is
    extended to enforce minimum duration.

    Args:
        words: List of {"word": str, "start": float, "end": float} dicts.

    Returns:
        List of _Cue objects with 1-based sequential indices.
    """
    if not words:
        return []

    cues: list[_Cue] = []
    cue_index = 1

    # Each "group" is a list of word dicts that belong to one cue
    current_group: list[dict] = []
    current_lines: list[list[dict]] = [[]]  # lines within the current cue

    def _flush() -> None:
        nonlocal cue_index
        if not current_group:
            return
        start = current_group[0]["start"]
        end = current_group[-1]["end"]
        end = max(end, start + MIN_CUE_DURATION_SECS)

        # Build display text: join each line's words
        line_strings = [
            " ".join(w["word"].strip() for w in ln)
            for ln in current_lines
            if ln
        ]
        text = "\n".join(line_strings)

        cues.append(_Cue(index=cue_index, start=start, end=end, text=text))
        cue_index += 1

    for word_dict in words:
        word_text = word_dict.get("word", "").strip()
        if not word_text:
            continue

        current_line = current_lines[-1]
        line_text_so_far = " ".join(w["word"].strip() for w in current_line)
        projected_line = f"{line_text_so_far} {word_text}".strip()

        line_full = (
            len(current_line) >= MAX_WORDS_PER_LINE
            or len(projected_line) > MAX_LINE_CHARS
        )

        if line_full:
            if len(current_lines) < MAX_LINES_PER_CUE:
                # Start a new line within the same cue
                current_lines.append([word_dict])
                current_group.append(word_dict)
            else:
                # Cue is full — flush and start fresh
                _flush()
                current_group = [word_dict]
                current_lines = [[word_dict]]
        else:
            current_line.append(word_dict)
            current_group.append(word_dict)

    _flush()  # flush the last cue
    return cues


def _segments_to_cues(segments: list[dict]) -> list[_Cue]:
    """
    Convert Groq segment dicts directly to _Cue objects.

    Each segment maps to exactly one cue.  Long segment text is wrapped
    at MAX_LINE_CHARS to keep lines readable.

    Args:
        segments: List of Groq Whisper segment dicts.

    Returns:
        List of _Cue objects with 1-based sequential indices.
    """
    cues: list[_Cue] = []
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0))
        end = float(seg.get("end", 0))
        end = max(end, start + MIN_CUE_DURATION_SECS)
        raw_text = (seg.get("text") or "").strip()

        if not raw_text:
            continue

        # Wrap long lines
        text = _wrap_text(raw_text, MAX_LINE_CHARS, MAX_LINES_PER_CUE)
        cues.append(_Cue(index=i, start=start, end=end, text=text))

    return cues


# ─────────────────────────────────────────────────────────────────────────────
# Master SRT construction
# ─────────────────────────────────────────────────────────────────────────────


def _build_master_srt(
    results: list[_SceneSubtitleResult],
    scenes: list[dict[str, Any]],
) -> str:
    """
    Merge all scene cues into one master SRT with correct cumulative offsets.

    Algorithm
    ─────────
    • Maintain a running `time_offset` (seconds) that accumulates the
      duration of every preceding scene.
    • For each scene's cues, add `time_offset` to both start and end.
    • Re-number all cues globally from 1.
    • The offset for scene N =  Σ duration_hint[scene 1 … scene N-1].

    Duration source (in priority order):
      1. `result.audio_duration`  — populated from scene.duration_hint (set
         by voice_generator from MP3 byte-count estimate).
      2. Last cue's `end` timestamp from the transcription (inferred).
      3. Fallback: 10.0 seconds.

    Args:
        results: Successfully transcribed scenes (in scene_number order).
        scenes:  Original scene dicts (used to look up duration_hints).

    Returns:
        SRT string ready to write to disk.
    """
    # Build scene_number → duration_hint lookup from original scene dicts
    duration_map: dict[int, float] = {}
    for raw_scene in scenes:
        s = Scene(**raw_scene)
        duration_map[s.scene_number] = s.duration_hint or 10.0

    # Sort results by scene_number to ensure correct order
    ordered = sorted(results, key=lambda r: r.scene_number)

    all_blocks: list[str] = []
    global_cue_index = 1
    time_offset: float = 0.0

    for res in ordered:
        for cue in res.cues:
            shifted_start = cue.start + time_offset
            shifted_end = cue.end + time_offset
            shifted_end = max(shifted_end, shifted_start + MIN_CUE_DURATION_SECS)

            block = (
                f"{global_cue_index}\n"
                f"{_fmt(shifted_start)} --> {_fmt(shifted_end)}\n"
                f"{cue.text}"
            )
            all_blocks.append(block)
            global_cue_index += 1

        # Advance offset by this scene's actual audio duration
        scene_duration = duration_map.get(res.scene_number, 10.0)
        # If the transcription ran longer than the hint, use that instead
        if res.cues:
            transcribed_end = res.cues[-1].end
            scene_duration = max(scene_duration, transcribed_end)

        logger.debug(
            "Master SRT | scene %03d | offset=%.3fs + duration=%.3fs → next_offset=%.3fs",
            res.scene_number,
            time_offset,
            scene_duration,
            time_offset + scene_duration,
        )
        time_offset += scene_duration

    return "\n\n".join(all_blocks) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# SRT serialisation
# ─────────────────────────────────────────────────────────────────────────────


def _cues_to_srt(cues: list[_Cue]) -> str:
    """
    Serialise a list of _Cue objects to a complete SRT string.

    SRT format:
        <index>\\n
        <HH:MM:SS,mmm> --> <HH:MM:SS,mmm>\\n
        <text>\\n
        \\n

    Args:
        cues: List of _Cue objects (indices already set correctly).

    Returns:
        SRT-formatted string, ready to write to a .srt file.
    """
    if not cues:
        return ""
    blocks = [cue.to_srt_block() for cue in cues]
    return "\n\n".join(blocks) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Timestamp formatting
# ─────────────────────────────────────────────────────────────────────────────


def _fmt(seconds: float) -> str:
    """
    Format a float number of seconds as an SRT timestamp string.

    SRT format: HH:MM:SS,mmm  (comma separator between seconds and millis)

    Examples:
        0.0     → "00:00:00,000"
        65.5    → "00:01:05,500"
        3723.42 → "01:02:03,420"

    Args:
        seconds: Time value in seconds (clamped to >= 0).

    Returns:
        SRT timestamp string.
    """
    seconds = max(0.0, seconds)
    total_ms = round(seconds * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ─────────────────────────────────────────────────────────────────────────────
# Text utilities
# ─────────────────────────────────────────────────────────────────────────────


def _wrap_text(text: str, max_chars: int, max_lines: int) -> str:
    """
    Wrap *text* into at most *max_lines* lines of at most *max_chars* chars.

    Breaks on word boundaries.  If the text fits on a single line it is
    returned unchanged.

    Args:
        text:      Input string (may already contain newlines).
        max_chars: Maximum characters per line.
        max_lines: Maximum number of output lines.

    Returns:
        Wrapped string with "\\n" as the line separator.
    """
    # Normalise existing newlines to spaces for re-wrapping
    text = " ".join(text.split())

    if len(text) <= max_chars:
        return text

    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        if len(lines) >= max_lines:
            # Max lines reached — append remaining words to last line
            if current:
                lines[-1] = " ".join(current)
            break

        projected = current_len + (1 if current else 0) + len(word)
        if current and projected > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len = projected

    if current and len(lines) < max_lines:
        lines.append(" ".join(current))

    return "\n".join(lines)


def _infer_duration(cues: list[_Cue]) -> float:
    """
    Infer the total duration of a scene from the last cue's end timestamp.
    Used when duration_hint is not set.
    """
    if not cues:
        return 10.0
    return max(cues[-1].end, MIN_CUE_DURATION_SECS)
