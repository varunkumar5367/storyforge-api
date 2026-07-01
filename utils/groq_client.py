"""
utils/groq_client.py — Singleton async Groq client wrapper.

Provides helpers for:
  - Chat completions (LLM text generation)
  - Whisper audio transcription
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("storyforge.groq_client")

# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------
_client: AsyncGroq | None = None
_rate_limited_models: dict[str, float] = {}



def get_groq_client() -> AsyncGroq:
    """Return a lazily-initialised singleton AsyncGroq client."""
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in the environment.")
        _client = AsyncGroq(api_key=api_key)
        logger.info("Groq async client initialised.")
    return _client


# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------
async def llm_chat(
    system_prompt: str,
    user_prompt: str,
    model: str = "llama-3.3-70b-versatile",
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    """
    Send a chat completion request to Groq and return the raw text response.
    Has built-in fallback logic for rate limit (429) or overloaded (503) errors.

    Args:
        system_prompt: Instruction/role context for the model.
        user_prompt:   The actual user query / task.
        model:         Groq model ID (default: llama-3.3-70b-versatile).
        temperature:   Sampling temperature (0.0–1.0).
        max_tokens:    Maximum tokens in the response.

    Returns:
        The assistant's message content as a plain string.
    """
    import asyncio
    import time
    import httpx
    from config import settings

    if settings.use_local_llm:
        logger.info(
            "Calling local Ollama LLM: model=%s | prompt_len=%d",
            settings.ollama_model,
            len(user_prompt),
        )
        try:
            async with httpx.AsyncClient(timeout=180.0) as local_client:
                resp = await local_client.post(
                    f"{settings.ollama_url.rstrip('/')}/chat/completions",
                    json={
                        "model": settings.ollama_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": False,
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"] or ""
                logger.debug("Local Ollama response | model=%s | chars=%d", settings.ollama_model, len(text))
                return text
        except Exception as exc:
            logger.error("Local Ollama LLM call failed: %s", str(exc))
            raise exc

    # Build list of models to try, starting with the requested model
    fallbacks = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    models_to_try = [model]
    for fb in fallbacks:
        if fb not in models_to_try:
            models_to_try.append(fb)

    # Filter out models that are currently marked as rate-limited
    now = time.monotonic()
    available_models = [m for m in models_to_try if _rate_limited_models.get(m, 0.0) < now]
    if not available_models:
        # If all are rate-limited, fall back to the last one anyway
        available_models = [models_to_try[-1]]

    client = get_groq_client()
    last_exc = None
    max_retries = 3

    for model_name in available_models:
        for attempt in range(max_retries):
            try:
                logger.info(
                    "Calling LLM: model=%s | prompt_len=%d | attempt=%d/%d",
                    model_name,
                    len(user_prompt),
                    attempt + 1,
                    max_retries,
                )
                completion = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                text = completion.choices[0].message.content or ""
                logger.debug("LLM response | model=%s | tokens_used=%s | chars=%d",
                             model_name, completion.usage, len(text))
                return text
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                is_rate_limit = "429" in exc_str or "rate_limit" in exc_str or "limit reached" in exc_str.lower()
                is_overloaded = "503" in exc_str or "overloaded" in exc_str.lower()
                is_decommissioned = "decommissioned" in exc_str.lower() or "not found" in exc_str.lower() or "does not exist" in exc_str.lower() or "not supported" in exc_str.lower()

                if is_rate_limit or is_overloaded:
                    # Mark model as rate-limited for 10 minutes (600 seconds)
                    _rate_limited_models[model_name] = time.monotonic() + 600.0
                    wait_time = 2.0 ** (attempt + 1)
                    logger.warning(
                        "Model %s failed with rate limit/overload (%s). Sleeping %.1fs before retry...",
                        model_name,
                        exc_str,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                elif is_decommissioned:
                    logger.warning(
                        "Model %s is decommissioned/unavailable (%s). Trying next fallback model immediately.",
                        model_name,
                        exc_str,
                    )
                    break
                else:
                    logger.error("LLM call failed with non-rate-limit/non-availability error: %s", exc_str)
                    raise exc
        logger.warning("Exhausted retries for model %s. Trying next fallback model...", model_name)

    logger.error("All LLM fallback models exhausted. Last error: %s", last_exc)
    raise last_exc



# ---------------------------------------------------------------------------
# Whisper transcription helper
# ---------------------------------------------------------------------------
async def transcribe_audio(
    audio_path: str | Path,
    model: str = "whisper-large-v3",
    response_format: str = "verbose_json",
    language: str = "en",
    timestamp_granularities: list[str] | None = None,
) -> dict:
    """
    Transcribe an audio file using Groq Whisper and return the raw response dict.
    Includes built-in rate-limit retries with exponential back-off to handle the 3 RPM limit.

    Args:
        audio_path:               Path to the MP3/WAV/etc. file.
        model:                    Whisper model ID.
        response_format:          "verbose_json" returns timestamped segments/words.
        language:                 ISO-639-1 language code.
        timestamp_granularities:  Granularity list, e.g. ["word", "segment"].
                                  Defaults to both so callers can use either.

    Returns:
        The full Groq transcription response as a dict.
    """
    import asyncio
    import gc
    import torch
    from config import settings

    if timestamp_granularities is None:
        timestamp_granularities = ["word", "segment"]

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if settings.use_local_whisper:
        logger.info(
            "Transcribing '%s' locally using faster-whisper: model=%s",
            audio_path.name,
            settings.local_whisper_model,
        )
        
        def _transcribe():
            from faster_whisper import WhisperModel
            import os
            force_cpu = os.environ.get("FORCE_CPU", "0").strip().lower() in ("1", "true", "yes")
            device = "cpu" if force_cpu else ("cuda" if torch.cuda.is_available() else "cpu")
            compute_type = "float16" if device == "cuda" else "int8"
            logger.info("Whisper device selected: %s (FORCE_CPU=%s)", device, force_cpu)
            
            try:
                whisper_model = WhisperModel(
                    settings.local_whisper_model,
                    device=device,
                    compute_type=compute_type,
                )
                segments_gen, info = whisper_model.transcribe(
                    str(audio_path),
                    language=language,
                    word_timestamps=True,
                )
                segments_list = list(segments_gen)
                
                formatted_segments = []
                all_words = []
                full_text = ""
                for seg in segments_list:
                    full_text += seg.text + " "
                    seg_dict = {
                        "id": seg.id,
                        "start": seg.start,
                        "end": seg.end,
                        "text": seg.text,
                        "words": []
                    }
                    if seg.words:
                        for w in seg.words:
                            word_dict = {
                                "word": w.word,
                                "start": w.start,
                                "end": w.end
                            }
                            seg_dict["words"].append(word_dict)
                            all_words.append(word_dict)
                    formatted_segments.append(seg_dict)
                
                return {
                    "text": full_text.strip(),
                    "segments": formatted_segments,
                    "words": all_words,
                }
            finally:
                if 'whisper_model' in locals():
                    del whisper_model
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    
        result_dict = await asyncio.to_thread(_transcribe)
        return result_dict

    client = get_groq_client()
    max_retries = 4
    last_exc = None

    for attempt in range(max_retries):
        try:
            logger.info(
                "Transcribing '%s' | model=%s | attempt=%d/%d",
                audio_path.name,
                model,
                attempt + 1,
                max_retries,
            )
            with audio_path.open("rb") as f:
                transcription = await client.audio.transcriptions.create(
                    file=(audio_path.name, f, "audio/mpeg"),
                    model=model,
                    response_format=response_format,
                    language=language,
                    timestamp_granularities=timestamp_granularities,
                )
            
            return transcription.model_dump() if hasattr(transcription, "model_dump") else dict(transcription)
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            is_rate_limit = "429" in exc_str or "rate_limit" in exc_str or "limit reached" in exc_str.lower()
            is_overloaded = "503" in exc_str or "overloaded" in exc_str.lower()

            if is_rate_limit or is_overloaded:
                # 3 RPM limit is strict, so we sleep longer: 5s, 10s, 20s, 40s
                wait_time = 5.0 * (2.0 ** attempt)
                logger.warning(
                    "Whisper transcription rate limited/overloaded (%s). Sleeping %.1fs before retry...",
                    exc_str,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error("Whisper transcription failed with error: %s", exc_str)
                raise exc

    logger.error("All Whisper transcription retries exhausted. Last error: %s", last_exc)
    raise last_exc
