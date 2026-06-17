"""
services/voice_generator.py
───────────────────────────
Step 3 of the StoryForge pipeline.

Responsibilities
────────────────
• Warm up VoiceForge before processing (Render.com free tier cold-starts):
    GET  {VOICEFORGE_URL}/api/health
    → 45 s timeout, up to 3 attempts with exponential back-off
    → If all attempts fail the whole service returns failure immediately,
      saving time that would otherwise be wasted on per-scene requests.

• For each scene, call VoiceForge TTS sequentially:
    POST {VOICEFORGE_URL}/api/tts
    Body : { "text": ..., "voice": ..., "speed": ..., "pitch": ... }
    Returns: audio/mpeg bytes

  Sequential (not parallel) to avoid hammering the free-tier Render instance.
  1.5 s inter-call delay between scenes.

• Save each response to:
    output/{job_id}/audio/scene_{n:03d}.mp3

• After saving, estimate the audio duration from the byte count using
  a rough 128 kbps bitrate assumption (accurate within ~5 %).
  `scene.duration_hint` is populated so the video composer knows each
  scene's length without running ffprobe at this stage.

Return contract
───────────────
Success:
    {
        "success": True,
        "data": {
            "scenes":         [<updated Scene dicts — audio_path + duration_hint set>],
            "audio_paths":    ["<abs-path-1>", ...],   # only successes
            "failed_scenes":  [<scene_numbers>],
            "total_duration": <float seconds>,          # sum of all duration_hints
        }
    }

Failure (wakeup failed — no scenes attempted):
    { "success": False, "error": "<message>" }
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from config import settings
from models.scene import Scene
from utils.file_handler import scene_audio_path, write_bytes

try:
    import edge_tts
except ImportError:
    edge_tts = None

logger = logging.getLogger("storyforge.voice_generator")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — all tuneable via environment or constructor kwargs
# ─────────────────────────────────────────────────────────────────────────────

VOICEFORGE_URL: str = settings.voiceforge_url.rstrip("/") if settings.voiceforge_url else ""

# Endpoints (empty when using local edge-tts only)
_HEALTH_ENDPOINT = f"{VOICEFORGE_URL}/api/health" if VOICEFORGE_URL else ""
_TTS_ENDPOINT = f"{VOICEFORGE_URL}/api/tts" if VOICEFORGE_URL else ""

# Wakeup / health-check settings
WAKEUP_TIMEOUT_SECS: float = 45.0        # single GET timeout (cold-start can take ~30 s)
WAKEUP_MAX_ATTEMPTS: int = 3             # total health-check attempts
WAKEUP_BACKOFF_BASE: float = 5.0         # wait = WAKEUP_BACKOFF_BASE ** attempt

# TTS call settings
TTS_TIMEOUT_SECS: float = 120.0          # individual TTS call timeout
TTS_INTER_CALL_DELAY_SECS: float = 1.5  # polite delay between scene calls

# Default voice parameters (overridable via generate_voices kwargs)
DEFAULT_VOICE = "en-US-JennyNeural"
DEFAULT_SPEED = 1.0
DEFAULT_PITCH = 0

# Duration estimation: 128 kbps MP3 → 128 000 bits/s → 16 000 bytes/s
_BYTES_PER_SECOND_MP3_128K: float = 16_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _VoiceResult:
    """Outcome of generating audio for a single scene."""

    scene_number: int
    success: bool
    path: str | None = None
    duration_secs: float = 0.0
    error: str | None = None
    bytes_received: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def generate_voices(
    job_id: str,
    scenes: list[dict[str, Any]],
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    pitch: int = DEFAULT_PITCH,
) -> dict[str, Any]:
    """
    Generate one MP3 per scene via VoiceForge TTS and save to disk.

    Args:
        job_id:  Unique job identifier (used to build output paths).
        scenes:  Serialised Scene dicts from previous pipeline steps.
        voice:   VoiceForge voice ID (e.g. "en-US-JennyNeural").
        speed:   Speech rate multiplier — 0.5 (slow) to 2.0 (fast).
        pitch:   Pitch shift in semitones, -10 to +10.

    Returns:
        On success::

            {
                "success": True,
                "data": {
                    "scenes":         [<updated Scene dicts>],
                    "audio_paths":    ["<abs_path>", ...],
                    "failed_scenes":  [<scene_numbers>],
                    "total_duration": <float seconds>,
                }
            }

        On wakeup failure::

            { "success": False, "error": "<message>" }
    """
    logger.info(
        "Voice generation START | job=%s | scenes=%d | voice=%s speed=%.1f pitch=%d",
        job_id,
        len(scenes),
        voice,
        speed,
        pitch,
    )

    # ── Phase 0: Wake up / health-check ───────────────────────────────────────
    wakeup_ok, wakeup_error = await _wakeup_voiceforge()
    if not wakeup_ok:
        logger.error("VoiceForge wakeup failed — aborting voice generation. %s", wakeup_error)
        return {"success": False, "error": f"VoiceForge unreachable: {wakeup_error}"}

    logger.info("VoiceForge is healthy — proceeding with TTS generation.")

    # ── Phase 1: Sequential TTS calls ─────────────────────────────────────────
    results: list[_VoiceResult] = []

    # Shared client for all TTS requests (connection reuse)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(TTS_TIMEOUT_SECS),
        follow_redirects=True,
    ) as client:
        for idx, raw_scene in enumerate(scenes):
            scene = Scene(**raw_scene)

            # Inter-call delay (skip before the very first request)
            if idx > 0:
                logger.debug(
                    "Waiting %.1fs before scene %03d …",
                    TTS_INTER_CALL_DELAY_SECS,
                    scene.scene_number,
                )
                await asyncio.sleep(TTS_INTER_CALL_DELAY_SECS)

            result = await _generate_scene_audio(
                client, job_id, scene, voice, speed, pitch
            )
            results.append(result)

    # ── Phase 2: Merge results back into scene dicts ───────────────────────────
    result_map: dict[int, _VoiceResult] = {r.scene_number: r for r in results}
    updated_scenes: list[dict] = []
    audio_paths: list[str] = []
    failed_scenes: list[int] = []
    total_duration: float = 0.0

    for raw_scene in scenes:
        scene = Scene(**raw_scene)
        res = result_map.get(scene.scene_number)

        if res and res.success and res.path:
            scene.audio_path = res.path
            scene.duration_hint = res.duration_secs
            audio_paths.append(res.path)
            total_duration += res.duration_secs
        else:
            error_detail = res.error if res else "unknown"
            failed_scenes.append(scene.scene_number)
            logger.warning(
                "Scene %03d voice FAILED: %s", scene.scene_number, error_detail
            )

        updated_scenes.append(scene.model_dump())

    succeeded = len(audio_paths)
    total = len(scenes)
    logger.info(
        "Voice generation DONE | job=%s | %d/%d succeeded | %d failed "
        "| total_duration=%.1fs",
        job_id,
        succeeded,
        total,
        len(failed_scenes),
        total_duration,
    )

    return {
        "success": True,
        "data": {
            "scenes":         updated_scenes,
            "audio_paths":    audio_paths,
            "failed_scenes":  failed_scenes,
            "total_duration": round(total_duration, 2),
        },
    }


async def warmup_voiceforge() -> tuple[bool, str | None]:
    """Public wrapper for VoiceForge health check / cold-start wakeup."""
    return await _wakeup_voiceforge()


async def generate_voice_for_scene(
    client: httpx.AsyncClient,
    job_id: str,
    scene: dict[str, Any],
    voice: str = DEFAULT_VOICE,
    speed: float = DEFAULT_SPEED,
    pitch: int = DEFAULT_PITCH,
) -> dict[str, Any]:
    """
    Generate TTS audio for a single scene and return the updated scene dict.

    The caller must ensure VoiceForge is warmed up (``warmup_voiceforge``)
    and provide a shared ``httpx.AsyncClient``.
    """
    scene_obj = Scene(**scene)
    result = await _generate_scene_audio(
        client, job_id, scene_obj, voice, speed, pitch
    )

    if result.success and result.path:
        scene_obj.audio_path = result.path
        scene_obj.duration_hint = result.duration_secs
    else:
        logger.warning(
            "Scene %03d voice FAILED: %s",
            scene_obj.scene_number,
            result.error or "unknown",
        )

    return scene_obj.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# Wakeup / health check
# ─────────────────────────────────────────────────────────────────────────────


async def _wakeup_voiceforge() -> tuple[bool, str | None]:
    """
    Ping VoiceForge's health endpoint and wait for a successful response.

    Render.com free-tier instances spin down after 15 minutes of inactivity
    and take 20–40 seconds to cold-start.  We handle this by:
      1. Sending a GET to /api/health with a generous 45 s timeout.
      2. Treating any 2xx response as "healthy".
      3. Retrying up to WAKEUP_MAX_ATTEMPTS times with exponential back-off
         if the request fails or returns a non-2xx status.

    Back-off schedule:
      attempt 0 → immediate
      attempt 1 → wait  5 s
      attempt 2 → wait 25 s

    Returns:
        (True, None)          — server is healthy, proceed
        (False, error_string) — all attempts exhausted
    """
    if edge_tts is not None:
        logger.info("Local edge-tts is available. Bypassing VoiceForge API warmup.")
        return True, None

    last_error = "unknown error"

    for attempt in range(WAKEUP_MAX_ATTEMPTS):
        if attempt > 0:
            wait = WAKEUP_BACKOFF_BASE ** attempt
            logger.info(
                "VoiceForge wakeup retry %d/%d in %.0fs …",
                attempt,
                WAKEUP_MAX_ATTEMPTS - 1,
                wait,
            )
            await asyncio.sleep(wait)

        logger.info(
            "VoiceForge health check | attempt %d/%d | url=%s",
            attempt + 1,
            WAKEUP_MAX_ATTEMPTS,
            _HEALTH_ENDPOINT,
        )

        ok, error = await _health_check_once()
        if ok:
            logger.info(
                "VoiceForge responded healthy on attempt %d.", attempt + 1
            )
            return True, None

        last_error = error
        logger.warning(
            "VoiceForge health check attempt %d failed: %s", attempt + 1, error
        )

    return False, (
        f"VoiceForge did not become healthy after {WAKEUP_MAX_ATTEMPTS} attempts. "
        f"Last error: {last_error}"
    )


async def _health_check_once() -> tuple[bool, str | None]:
    """
    Perform a single GET /api/health request.

    Returns:
        (True, None)          on HTTP 2xx
        (False, error_string) on timeout, connection error, or non-2xx
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(WAKEUP_TIMEOUT_SECS)
        ) as client:
            response = await client.get(_HEALTH_ENDPOINT)
    except httpx.TimeoutException:
        return False, f"Health check timed out after {WAKEUP_TIMEOUT_SECS:.0f}s"
    except httpx.ConnectError as exc:
        return False, f"Connection refused: {exc}"
    except httpx.RequestError as exc:
        return False, f"Request error: {exc}"

    if response.is_success:
        logger.debug(
            "Health check OK | status=%d | body=%.80s",
            response.status_code,
            response.text,
        )
        return True, None

    return False, f"HTTP {response.status_code}: {response.text[:120]}"


# ─────────────────────────────────────────────────────────────────────────────
# Scene-level TTS
# ─────────────────────────────────────────────────────────────────────────────


async def _generate_scene_audio(
    client: httpx.AsyncClient,
    job_id: str,
    scene: Scene,
    voice: str,
    speed: float,
    pitch: int,
) -> _VoiceResult:
    """
    Call VoiceForge TTS for a single scene, validate the response, and
    save the MP3 to disk.

    The scene's `text` field (synced to `narration`) is the source for TTS.
    If the text is unusually long (> 800 chars) it is split into chunks,
    each converted separately, and the resulting bytes are concatenated.
    This avoids server-side timeouts on lengthy narrations.

    Args:
        client:  Shared httpx.AsyncClient (already configured with timeout).
        job_id:  Job identifier for path construction.
        scene:   Deserialised Scene object.
        voice:   VoiceForge voice ID.
        speed:   Speech rate.
        pitch:   Pitch shift.

    Returns:
        _VoiceResult with success, path, duration_secs, and error details.
    """
    narration = scene.text.strip()  # `text` is the canonical narration field
    if not narration:
        return _VoiceResult(
            scene_number=scene.scene_number,
            success=False,
            error="Scene narration text is empty — nothing to synthesise.",
        )

    logger.info(
        "TTS | scene %03d | chars=%d | voice=%s speed=%.1f pitch=%d",
        scene.scene_number,
        len(narration),
        voice,
        speed,
        pitch,
    )

    # Split very long narrations into chunks to avoid TTS timeouts
    chunks = _split_text(narration, max_chars=800)
    if len(chunks) > 1:
        logger.debug(
            "Scene %03d narration split into %d chunks.", scene.scene_number, len(chunks)
        )

    # Synthesise each chunk and collect bytes
    all_audio_bytes = bytearray()
    for chunk_idx, chunk in enumerate(chunks):
        chunk_bytes, error = await _call_tts(client, chunk, voice, speed, pitch)
        if error:
            return _VoiceResult(
                scene_number=scene.scene_number,
                success=False,
                error=(
                    f"TTS failed on chunk {chunk_idx + 1}/{len(chunks)}: {error}"
                ),
            )
        all_audio_bytes.extend(chunk_bytes)
        logger.debug(
            "Scene %03d chunk %d/%d → %d bytes",
            scene.scene_number,
            chunk_idx + 1,
            len(chunks),
            len(chunk_bytes),
        )

    total_bytes = len(all_audio_bytes)
    if total_bytes == 0:
        return _VoiceResult(
            scene_number=scene.scene_number,
            success=False,
            error="TTS returned zero audio bytes.",
        )

    # Save to disk
    out_path = scene_audio_path(job_id, scene.scene_number)
    try:
        await write_bytes(out_path, bytes(all_audio_bytes))
    except OSError as exc:
        return _VoiceResult(
            scene_number=scene.scene_number,
            success=False,
            error=f"Disk write failed: {exc}",
        )

    # Estimate duration from byte count (128 kbps MP3 assumption)
    duration_secs = _estimate_duration(total_bytes)

    logger.info(
        "Scene %03d audio saved → %s | %d KB | ~%.1fs",
        scene.scene_number,
        out_path.name,
        total_bytes // 1024,
        duration_secs,
    )

    return _VoiceResult(
        scene_number=scene.scene_number,
        success=True,
        path=str(out_path),
        duration_secs=duration_secs,
        bytes_received=total_bytes,
    )


async def _call_tts(
    client: httpx.AsyncClient,
    text: str,
    voice: str,
    speed: float,
    pitch: int,
) -> tuple[bytes, str | None]:
    """
    Make a POST to VoiceForge /api/tts with built-in retries and back-off to handle
    transient connection errors, timeouts, rate limits, or server cold starts.

    Args:
        client: Shared async HTTP client.
        text:   Text to synthesise (≤ 800 chars recommended).
        voice:  Voice identifier.
        speed:  Speech rate multiplier.
        pitch:  Pitch shift in semitones.

    Returns:
        (audio_bytes, None)          on success
        (b"",         error_string)  on any failure
    """
    if edge_tts is not None:
        try:
            logger.info("Generating TTS locally using edge-tts | voice=%s | speed=%.2f | pitch=%d", voice, speed, pitch)
            # Map speed multiplier to percentage format (e.g. +10%, -5%)
            rate_pct = int((speed - 1.0) * 100)
            rate_str = f"{rate_pct:+d}%" if rate_pct != 0 else "+0%"
            # Map pitch in semitones to Hz offsets (e.g. +5Hz, -3Hz)
            pitch_str = f"{pitch:+d}Hz" if pitch != 0 else "+0Hz"
            
            communicate = edge_tts.Communicate(text, voice, rate=rate_str, pitch=pitch_str)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            
            if audio_data:
                return audio_data, None
            logger.warning("Local edge-tts returned empty audio, falling back to VoiceForge API.")
        except Exception as e:
            logger.warning("Local edge-tts failed, falling back to VoiceForge API: %s", e)

    payload = {
        "text":  text,
        "voice": voice,
        "speed": speed,
        "pitch": pitch,
    }

    logger.debug("POST %s | chars=%d", _TTS_ENDPOINT, len(text))
    max_retries = 4
    last_error = "unknown error"

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait_time = 3.0 * (2.0 ** (attempt - 1))
                logger.info(
                    "TTS retry %d/%d in %.1fs for text chunk (chars=%d)...",
                    attempt,
                    max_retries - 1,
                    wait_time,
                    len(text),
                )
                await asyncio.sleep(wait_time)

            response = await client.post(_TTS_ENDPOINT, data=payload)
            
            # ── Status check ──────────────────────────────────────────────────────────
            if response.status_code == 422:
                # Unprocessable entity — likely a payload schema error, do not retry
                return b"", f"VoiceForge rejected payload (HTTP 422): {response.text[:200]}"
                
            if response.status_code in (429, 502, 503, 504):
                last_error = f"VoiceForge temporary issue (HTTP {response.status_code}): {response.text[:100]}"
                logger.warning(last_error)
                continue

            response.raise_for_status()

            # ── Content-type check ────────────────────────────────────────────────────
            content_type = response.headers.get("content-type", "").lower()
            valid_ct = "audio" in content_type or "octet-stream" in content_type
            if not valid_ct:
                last_error = (
                    f"Unexpected content-type '{content_type}'. "
                    f"Body preview: {response.text[:150]}"
                )
                logger.warning(last_error)
                continue

            # ── Body check ────────────────────────────────────────────────────────────
            if not response.content:
                last_error = "VoiceForge returned an empty audio body"
                logger.warning(last_error)
                continue

            logger.debug(
                "TTS response OK | %d bytes | content-type=%s",
                len(response.content),
                content_type,
            )
            return response.content, None

        except httpx.TimeoutException:
            last_error = f"TTS request timed out after {TTS_TIMEOUT_SECS:.0f}s"
            logger.warning(last_error)
        except httpx.ConnectError as exc:
            last_error = f"Connection error: {exc}"
            logger.warning(last_error)
        except httpx.RequestError as exc:
            last_error = f"HTTP request error: {exc}"
            logger.warning(last_error)
        except httpx.HTTPStatusError as exc:
            body_preview = exc.response.text[:200] if exc.response.text else "<empty>"
            last_error = f"HTTP {exc.response.status_code}: {body_preview}"
            logger.warning(last_error)

    return b"", f"TTS generation failed after {max_retries} attempts. Last error: {last_error}"


# ─────────────────────────────────────────────────────────────────────────────
# Text chunking
# ─────────────────────────────────────────────────────────────────────────────


def _split_text(text: str, max_chars: int = 800) -> list[str]:
    """
    Split *text* into sentence-boundary-aware chunks ≤ *max_chars* each.

    Algorithm
    ─────────
    1. Split on sentence-ending punctuation (. ! ?) followed by whitespace.
    2. Greedily accumulate sentences until the next sentence would exceed
       *max_chars* — then start a new chunk.
    3. If a single sentence is longer than *max_chars*, split it on the
       nearest word boundary before the limit.

    Args:
        text:      The full narration string.
        max_chars: Maximum character count per chunk.

    Returns:
        List of non-empty text strings, each ≤ *max_chars* characters
        (except degenerate single-word cases).
    """
    if len(text) <= max_chars:
        return [text]

    import re

    # Split into individual sentences (keep the delimiter with the sentence)
    raw_sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in raw_sentences if s.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)

        # A single sentence longer than max_chars needs hard-splitting
        if sentence_len > max_chars:
            # Flush current accumulation first
            if current:
                chunks.append(" ".join(current))
                current, current_len = [], 0
            # Hard-split the overlong sentence on word boundary
            chunks.extend(_hard_split(sentence, max_chars))
            continue

        # Would adding this sentence exceed the limit?
        projected = current_len + (1 if current else 0) + sentence_len
        if current and projected > max_chars:
            chunks.append(" ".join(current))
            current, current_len = [sentence], sentence_len
        else:
            current.append(sentence)
            current_len += (1 if len(current) > 1 else 0) + sentence_len

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if c]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """
    Split a single long string into ≤ max_chars chunks at word boundaries.
    Used as a fallback when a single sentence exceeds *max_chars*.
    """
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        word_len = len(word)
        projected = current_len + (1 if current else 0) + word_len
        if current and projected > max_chars:
            chunks.append(" ".join(current))
            current, current_len = [word], word_len
        else:
            current.append(word)
            current_len += (1 if len(current) > 1 else 0) + word_len

    if current:
        chunks.append(" ".join(current))

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Duration estimation
# ─────────────────────────────────────────────────────────────────────────────


def _estimate_duration(byte_count: int) -> float:
    """
    Estimate the playback duration of an MP3 file from its byte count.

    Assumption: VoiceForge encodes at ~128 kbps (standard TTS quality).
        duration = bytes / (bitrate_kbps * 1000 / 8)
                 = bytes / 16_000

    This is accurate within ~5 % for constant-bitrate streams.
    For variable-bitrate output the estimate may drift by up to ~15 %.

    Args:
        byte_count: Size of the MP3 file in bytes.

    Returns:
        Estimated duration in seconds (minimum 0.1 s).
    """
    if byte_count <= 0:
        return 0.0
    duration = byte_count / _BYTES_PER_SECOND_MP3_128K
    return max(0.1, round(duration, 2))
