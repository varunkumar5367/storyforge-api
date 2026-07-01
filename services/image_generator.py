"""
services/image_generator.py
───────────────────────────
Step 2 of the StoryForge pipeline.

Responsibilities
────────────────
• For every scene, build a rich image prompt from four layers:
    1. scene.image_prompt  — LLM-written visual description of the scene
    2. scene.text          — narration snippet (mood anchor)
    3. scene.setting       — physical environment/atmosphere
    4. character_memory    — per-character visual descriptions for only the
                             characters present in THIS scene
  All four layers are combined with the fixed art-style token string:
    "anime fantasy illustration, cinematic lighting, high detail, 16:9"

• Derive a per-character seed from a deterministic hash of the character
  name so the same character always maps to the same seed, keeping faces
  consistent across scenes.  When multiple characters share a scene the
  seeds are XOR-combined into a single stable integer.

• Generate 1280×720 images via a three-provider fallback chain:
    1. Gemini (primary)       — google-generativeai SDK
    2. Hugging Face (fallback)— FLUX.1-schnell inference API
    3. Pollinations (last)    — requires POLLINATIONS_API_KEY

• Save each image to:
    output/{job_id}/images/scene_{n:03d}.png

• Return the standard pipeline result dict:
    {
        "success": True,
        "data": {
            "scenes":       [<updated scene dicts with image_path set>],
            "image_paths":  ["<abs-path-1>", ...],        # only successes
            "failed_scenes": [<scene_numbers that failed>],
        }
    }
  image generation failures are non-fatal — the pipeline continues.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings
from models.character import Character, CharacterMemory
from models.scene import Scene
from utils.file_handler import scene_image_path, write_bytes

logger = logging.getLogger("image_generator")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"
HF_MODEL_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "black-forest-labs/FLUX.1-schnell"
)
POLLINATIONS_BASE = "https://gen.pollinations.ai/image"

IMAGE_WIDTH = 1280
IMAGE_HEIGHT = 720

ART_STYLE = (
    "anime fantasy illustration, cinematic lighting, "
    "high detail, 16:9, masterpiece, sharp focus"
)

NEGATIVE_TOKENS = (
    "low quality, blurry, watermark, text, logo, "
    "duplicate faces, deformed, extra limbs, ugly"
)

REQUEST_TIMEOUT = 90
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_BASE = 3.0
MAX_PROMPT_CHARS = 1500

# Gemini free tier: 2 images/min → 32 s between calls
GEMINI_MIN_INTERVAL_SECS = 32.0

_gemini_last_call_at: float = 0.0
_gemini_lock = asyncio.Lock()



# ─────────────────────────────────────────────────────────────────────────────
# Internal data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _ImageResult:
    """Holds the outcome of a single scene's image generation."""

    scene_number: int
    success: bool
    path: str | None = None
    error: str | None = None
    prompt_used: str = ""
    seed_used: int = 0
    provider: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def generate_image_for_scene(
    job_id: str,
    scene: dict[str, Any],
    character_memory: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    art_style_suffix: str = ART_STYLE,
    image_model: str | None = None,
) -> dict[str, Any]:
    """
    Generate an image for a single scene and return the updated scene dict.

    Used by the per-scene streaming orchestrator.  Failures are non-fatal:
    ``image_path`` and ``image_provider`` are set to None on failure.
    """
    char_mem = CharacterMemory(**character_memory)
    scene_obj = Scene(**scene)

    out_path = scene_image_path(job_id, scene_obj.scene_number)
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("Scene %03d | Image file already exists on disk, skipping generation: %s", scene_obj.scene_number, out_path)
        scene_obj.image_path = str(out_path)
        scene_obj.image_provider = "cached"
        return scene_obj.model_dump()

    if client is None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            follow_redirects=True,
        ) as owned_client:
            result = await _generate_scene_image(
                owned_client, job_id, scene_obj, char_mem,
                width=width, height=height, art_style_suffix=art_style_suffix,
                image_model=image_model,
            )
    else:
        result = await _generate_scene_image(
            client, job_id, scene_obj, char_mem,
            width=width, height=height, art_style_suffix=art_style_suffix,
            image_model=image_model,
        )

    if result.success and result.path:
        scene_obj.image_path = result.path
        scene_obj.image_provider = result.provider
    else:
        scene_obj.image_path = None
        scene_obj.image_provider = None

    return scene_obj.model_dump()


async def generate_images(
    job_id: str,
    scenes: list[dict[str, Any]],
    character_memory: dict[str, Any],
) -> dict[str, Any]:
    """
    Generate one 1280×720 PNG per scene and write them to disk.

    Args:
        job_id:           Unique job identifier (used for directory naming).
        scenes:           Serialised Scene dicts from the story_analyzer step.
        character_memory: Serialised CharacterMemory dict.

    Returns:
        {
            "success": True,
            "data": {
                "scenes":        [<updated Scene dicts — image_path populated>],
                "image_paths":   ["<abs_path>", ...],
                "failed_scenes": [<scene_numbers>],
            }
        }
        Always returns success=True; individual scene failures are non-fatal.
    """
    logger.info(
        "Image generation START | job=%s | scenes=%d", job_id, len(scenes)
    )

    char_mem = CharacterMemory(**character_memory)
    results: list[_ImageResult] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        follow_redirects=True,
    ) as client:
        for raw_scene in scenes:
            scene = Scene(**raw_scene)
            result = await _generate_scene_image(client, job_id, scene, char_mem)
            results.append(result)

    result_map: dict[int, _ImageResult] = {r.scene_number: r for r in results}
    updated_scenes: list[dict] = []
    image_paths: list[str] = []
    failed_scenes: list[int] = []

    for raw_scene in scenes:
        scene = Scene(**raw_scene)
        res = result_map.get(scene.scene_number)

        if res and res.success and res.path:
            scene.image_path = res.path
            scene.image_provider = res.provider
            image_paths.append(res.path)
        else:
            error_detail = res.error if res else "unknown"
            scene.image_path = None
            scene.image_provider = None
            failed_scenes.append(scene.scene_number)
            logger.warning(
                "Scene %03d image FAILED: %s", scene.scene_number, error_detail
            )

        updated_scenes.append(scene.model_dump())

    succeeded = len(image_paths)
    total = len(scenes)
    logger.info(
        "Image generation DONE | job=%s | %d/%d succeeded | %d failed",
        job_id,
        succeeded,
        total,
        len(failed_scenes),
    )

    return {
        "success": True,
        "data": {
            "scenes":        updated_scenes,
            "image_paths":   image_paths,
            "failed_scenes": failed_scenes,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scene-level logic
# ─────────────────────────────────────────────────────────────────────────────


async def _generate_scene_image(
    client: httpx.AsyncClient,
    job_id: str,
    scene: Scene,
    char_mem: CharacterMemory,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    art_style_suffix: str = ART_STYLE,
    image_model: str | None = None,
) -> _ImageResult:
    """Build the prompt, download via provider chain, and save to disk."""
    prompt = _build_scene_prompt(scene, char_mem, art_style_suffix=art_style_suffix)
    seed = _scene_seed(scene, char_mem)

    logger.info(
        "Scene %03d | seed=%d | prompt_chars=%d",
        scene.scene_number,
        seed,
        len(prompt),
    )
    logger.debug("Scene %03d full prompt:\n%s", scene.scene_number, prompt)

    image_bytes, provider, download_error = await _download_with_retry(
        client, prompt, seed, scene.scene_number, width=width, height=height, image_model=image_model
    )

    if download_error:
        logger.error(
            "Scene %03d | all providers failed: %s",
            scene.scene_number,
            download_error,
        )
        return _ImageResult(
            scene_number=scene.scene_number,
            success=False,
            error=download_error,
            prompt_used=prompt,
            seed_used=seed,
        )

    out_path = scene_image_path(job_id, scene.scene_number)
    try:
        await write_bytes(out_path, image_bytes)
    except OSError as exc:
        return _ImageResult(
            scene_number=scene.scene_number,
            success=False,
            error=f"Disk write failed: {exc}",
            prompt_used=prompt,
            seed_used=seed,
            provider=provider,
        )

    logger.info(
        "Scene %03d | provider=%s | saved → %s",
        scene.scene_number,
        provider,
        out_path.name,
    )
    return _ImageResult(
        scene_number=scene.scene_number,
        success=True,
        path=str(out_path),
        prompt_used=prompt,
        seed_used=seed,
        provider=provider,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────


def _build_scene_prompt(scene: Scene, char_mem: CharacterMemory, *, art_style_suffix: str = ART_STYLE) -> str:
    """Assemble a layered image-generation prompt for one scene."""
    parts: list[str] = []

    if scene.image_prompt:
        parts.append(scene.image_prompt.strip())

    char_block = _build_character_block(scene.characters_present, char_mem)
    if char_block:
        parts.append(char_block)

    parts.append(art_style_suffix)

    positive = ", ".join(filter(None, parts))
    full_prompt = f"{positive} | {NEGATIVE_TOKENS}"

    if len(full_prompt) > MAX_PROMPT_CHARS:
        logger.debug(
            "Scene %03d prompt truncated from %d → %d chars",
            scene.scene_number,
            len(full_prompt),
            MAX_PROMPT_CHARS,
        )
        full_prompt = full_prompt[:MAX_PROMPT_CHARS]

    return full_prompt


def _build_character_block(
    characters_present: list[str],
    char_mem: CharacterMemory,
) -> str:
    """Build a compact inline description of characters visible in the scene."""
    if not characters_present:
        return ""

    descriptions: list[str] = []
    for name in characters_present:
        char = char_mem.get(name)
        if char:
            descriptions.append(_character_inline(char))

    if not descriptions:
        return ""

    joined = "; ".join(descriptions)
    return f"Characters visible: {joined}"


def _character_inline(char: Character) -> str:
    """Return an anonymised, appearance-only description of a character."""
    return char.to_image_description()


# ─────────────────────────────────────────────────────────────────────────────
# Seed derivation
# ─────────────────────────────────────────────────────────────────────────────


def _character_seed(name: str) -> int:
    """Derive a deterministic integer seed from a character's name."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    raw = int(digest[:10], 16)
    return (raw % 999_999) + 1


def _scene_seed(scene: Scene, char_mem: CharacterMemory) -> int:
    """Derive a single seed for a scene from the characters present in it."""
    known = [
        char_mem.get(name)
        for name in scene.characters_present
        if char_mem.get(name) is not None
    ]

    if not known:
        fallback_text = f"scene_{scene.scene_number}_{scene.title}"
        return _character_seed(fallback_text)

    if len(known) == 1:
        return _character_seed(known[0].name)

    combined = 0
    for char in known:
        combined ^= _character_seed(char.name)

    return max(1, combined % 999_999)


# ─────────────────────────────────────────────────────────────────────────────
# Download with retry — provider fallback chain
# ─────────────────────────────────────────────────────────────────────────────


async def _download_with_retry(
    client: httpx.AsyncClient,
    prompt: str,
    seed: int,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    image_model: str | None = None,
) -> tuple[bytes, str | None, str | None]:
    """
    Try providers in priority order with exponential back-off between attempts.

    Returns:
        (image_bytes, provider_name, None) on success
        (b"", None, error_string) after all retries exhausted
    """
    last_error = "unknown error"

    for attempt in range(RETRY_ATTEMPTS):
        if attempt > 0:
            wait = RETRY_BACKOFF_BASE ** attempt
            logger.info(
                "Scene %03d | retry %d/%d in %.0fs …",
                scene_number,
                attempt,
                RETRY_ATTEMPTS - 1,
                wait,
            )
            await asyncio.sleep(wait)

        image_bytes, provider, error = await _try_all_providers(
            client, prompt, seed, scene_number, width=width, height=height, image_model=image_model
        )

        if error is None and image_bytes:
            return image_bytes, provider, None

        last_error = error or "unknown error"
        logger.warning(
            "Scene %03d | attempt %d failed: %s",
            scene_number,
            attempt + 1,
            last_error,
        )

    return b"", None, (
        f"All {RETRY_ATTEMPTS} download attempts failed. Last error: {last_error}"
    )


async def _download_local_diffusers(
    prompt: str,
    seed: int,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    image_model: str | None = None,
) -> tuple[bytes, str | None]:
    """Generate an image locally using PyTorch diffusers."""
    import gc
    import torch
    import io

    def _generate():
        from diffusers.pipelines.stable_diffusion_xl.pipeline_stable_diffusion_xl import StableDiffusionXLPipeline
        from diffusers.pipelines.pipeline_utils import DiffusionPipeline
        from diffusers.schedulers.scheduling_euler_discrete import EulerDiscreteScheduler
        import os
        
        # Check if FORCE_CPU override is set (from GUI RAM/VRAM toggle)
        force_cpu = os.environ.get("FORCE_CPU", "0").strip().lower() in ("1", "true", "yes")
        
        # Determine device
        device = "cpu" if force_cpu else ("cuda" if torch.cuda.is_available() else "cpu")
        torch_dtype = torch.float16 if device == "cuda" else torch.float32
        logger.info("Diffusers device selected: %s (FORCE_CPU=%s)", device, force_cpu)
        
        model_id = image_model or settings.local_image_model
        pipe = None
        
        try:
            logger.info("Loading local diffusers model: %s on %s", model_id, device)
            
            if model_id == "ByteDance/SDXL-Lightning-4step":
                # Check if we already have a pre-merged model folder saved locally on the E: drive
                import os
                hf_home = os.environ.get("HF_HOME", ".")
                merged_local_path = os.path.join(os.path.dirname(hf_home) if hf_home != "." else ".", "sdxl_lightning_4step_merged")
                
                if os.path.exists(merged_local_path) and os.path.exists(os.path.join(merged_local_path, "model_index.json")):
                    logger.info("Loading pre-merged SDXL-Lightning pipeline from local disk: %s", merged_local_path)
                    try:
                        pipe = StableDiffusionXLPipeline.from_pretrained(
                            merged_local_path,
                            torch_dtype=torch_dtype,
                            use_safetensors=True,
                        )
                    except Exception as e:
                        logger.warning("Failed loading pre-merged model from %s: %s. Retrying dynamic merge...", merged_local_path, e)
                        merged_local_path = None
                else:
                    merged_local_path = None

                if merged_local_path is None:
                    # Perform dynamic merge of base SDXL and Lightning UNet
                    from huggingface_hub import hf_hub_download
                    from safetensors.torch import load_file
                    
                    base = "stabilityai/stable-diffusion-xl-base-1.0"
                    repo = "ByteDance/SDXL-Lightning"
                    ckpt = "sdxl_lightning_4step_unet.safetensors"
                    
                    logger.info("Loading base SDXL pipeline: %s", base)
                    try:
                        pipe = StableDiffusionXLPipeline.from_pretrained(
                            base,
                            torch_dtype=torch_dtype,
                            variant="fp16" if device == "cuda" else None,
                        )
                    except (ValueError, OSError):
                        logger.warning("Failed loading SDXL with variant='fp16', retrying without variant")
                        pipe = StableDiffusionXLPipeline.from_pretrained(
                            base,
                            torch_dtype=torch_dtype,
                        )
                    
                    logger.info("Overwriting UNet weights with lightning checkpoint: %s", ckpt)
                    pipe.unet.load_state_dict(load_file(hf_hub_download(repo, ckpt), device="cpu"))
                    
                    pipe.scheduler = EulerDiscreteScheduler.from_config(
                        pipe.scheduler.config,
                        timestep_spacing="trailing"
                    )
                    
                    # Auto-save the merged pipeline to disk for future fast startups
                    try:
                        save_path = os.path.join(os.path.dirname(hf_home) if hf_home != "." else ".", "sdxl_lightning_4step_merged")
                        logger.info("Auto-saving merged SDXL-Lightning pipeline to local disk: %s", save_path)
                        os.makedirs(save_path, exist_ok=True)
                        pipe.save_pretrained(save_path)
                        logger.info("Merged model saved successfully!")
                    except Exception as e:
                        logger.warning("Failed to auto-save merged model: %s", e)
            else:
                # Load generic model directly
                try:
                    pipe = DiffusionPipeline.from_pretrained(
                        model_id,
                        torch_dtype=torch_dtype,
                        variant="fp16" if device == "cuda" else None,
                    )
                except (ValueError, OSError):
                    logger.warning("Failed loading model %s with variant='fp16', retrying without variant", model_id)
                    pipe = DiffusionPipeline.from_pretrained(
                        model_id,
                        torch_dtype=torch_dtype,
                    )
                
            if "flux" in model_id.lower() and device == "cuda":
                logger.info("Enabling model CPU offload for local FLUX generation to prevent VRAM OOM")
                pipe.enable_model_cpu_offload()
            else:
                pipe = pipe.to(device)
            
            # Enforce safety checker bypass (optional, to save memory/speed)
            if hasattr(pipe, "safety_checker") and pipe.safety_checker is not None:
                pipe.safety_checker = None
                
            # Set up seed/generator
            gen_device = "cpu" if ("flux" in model_id.lower() and device == "cuda") else device
            generator = torch.Generator(device=gen_device).manual_seed(seed)
            
            # Run image generation
            # Note: for SDXL-Lightning/LCM/Schnell we use 4 steps. For others we default to 25.
            num_inference_steps = 4 if "lightning" in model_id.lower() or "lcm" in model_id.lower() or "turbo" in model_id.lower() or "schnell" in model_id.lower() else 25
            guidance_scale = 0.0 if "lightning" in model_id.lower() or "turbo" in model_id.lower() or "schnell" in model_id.lower() else (3.5 if "flux" in model_id.lower() else 7.5)
            
            logger.info("Generating local image | steps=%d | guidance=%.1f", num_inference_steps, guidance_scale)
            
            output = pipe(
                prompt=prompt,
                width=width,
                height=height,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
                generator=generator,
            )
            
            image = output.images[0]
            
            # Convert PIL image to bytes
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            return buf.getvalue()
            
        finally:
            if pipe is not None:
                del pipe
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                
    try:
        img_bytes = await asyncio.to_thread(_generate)
        if img_bytes:
            return img_bytes, None
        return b"", "Local diffusers returned no image data"
    except Exception as exc:
        logger.exception("Local diffusers image generation failed")
        return b"", f"Local diffusers error: {exc}"


async def _try_all_providers(
    client: httpx.AsyncClient,
    prompt: str,
    seed: int,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    image_model: str | None = None,
) -> tuple[bytes, str | None, str | None]:
    """
    Provider chain: Local Diffusers → Gemini → Hugging Face Paid → Pollinations → Stable Horde → HF Free → PIL Placeholder.
 
    Returns (bytes, provider, error) where error is None on success.
    """
    # 0. Local Diffusers
    if settings.use_local_images:
        local_bytes, local_err = await _download_local_diffusers(
            prompt, seed, scene_number, width=width, height=height, image_model=image_model
        )
        if local_err is None and local_bytes:
            return local_bytes, "local_diffusers", None
        logger.warning("Scene %03d | provider=local_diffusers failed: %s", scene_number, local_err)

    # 1. Gemini
    gemini_bytes, gemini_err = await _download_gemini(prompt, scene_number, width=width, height=height)
    if gemini_err is None and gemini_bytes:
        return gemini_bytes, "gemini", None
    logger.warning("Scene %03d | provider=gemini failed: %s", scene_number, gemini_err)

    # 2. Hugging Face Paid
    hf_bytes, hf_err = await _download_huggingface(client, prompt, scene_number, width=width, height=height)
    if hf_err is None and hf_bytes:
        return hf_bytes, "huggingface", None
    logger.warning("Scene %03d | provider=huggingface failed: %s", scene_number, hf_err)

    # 3. Pollinations
    poll_bytes, poll_err = await _download_pollinations(client, prompt, seed, scene_number, width=width, height=height)
    if poll_err is None and poll_bytes:
        return poll_bytes, "pollinations", None
    logger.warning("Scene %03d | provider=pollinations failed: %s", scene_number, poll_err)

    # 4. Stable Horde
    logger.warning("Scene %03d | Trying Stable Horde...", scene_number)
    sh_bytes, sh_err = await _download_stable_horde(prompt, scene_number, width=width, height=height)
    if sh_err is None and sh_bytes:
        return sh_bytes, "stablehorde", None
    logger.warning("Scene %03d | provider=stablehorde failed: %s", scene_number, sh_err)

    # 5. Hugging Face Free
    logger.warning("Scene %03d | Trying Hugging Face free inference...", scene_number)
    hff_bytes, hff_err = await _download_hf_free(prompt, scene_number, width=width, height=height)
    if hff_err is None and hff_bytes:
        return hff_bytes, "hf_free", None
    logger.warning("Scene %03d | provider=hf_free failed: %s", scene_number, hff_err)

    # 6. PIL Placeholder (never fails)
    logger.warning("Scene %03d | All image APIs failed — generating PIL placeholder.", scene_number)
    placeholder_bytes = _make_placeholder_image(scene_number, prompt, width=width, height=height)
    return placeholder_bytes, "placeholder", None


# ─────────────────────────────────────────────────────────────────────────────
# Provider 1 — Gemini
# ─────────────────────────────────────────────────────────────────────────────


async def _gemini_rate_limit_wait() -> None:
    """Enforce 32 s minimum interval between Gemini API calls."""
    global _gemini_last_call_at

    async with _gemini_lock:
        if _gemini_last_call_at > 0:
            elapsed = time.monotonic() - _gemini_last_call_at
            if elapsed < GEMINI_MIN_INTERVAL_SECS:
                wait = GEMINI_MIN_INTERVAL_SECS - elapsed
                logger.debug("Gemini rate limit — sleeping %.1fs", wait)
                await asyncio.sleep(wait)

        _gemini_last_call_at = time.monotonic()



async def _download_gemini(
    prompt: str,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> tuple[bytes, str | None]:
    """Generate an image via the Google Generative AI SDK."""
    api_key = settings.gemini_api_key
    if not api_key:
        return b"", "GEMINI_API_KEY not set"

    try:
        import google.generativeai as genai  # noqa: F401
    except ImportError:
        logger.warning(
            "google-generativeai not installed — skipping Gemini provider"
        )
        return b"", "google-generativeai package not installed"

    await _gemini_rate_limit_wait()

    try:
        image_bytes = await asyncio.to_thread(
            _gemini_generate_sync, api_key, prompt, scene_number, width=width, height=height
        )
        if image_bytes:
            return _resize_to_target(image_bytes, width=width, height=height), None
        return b"", "Gemini returned no image data"
    except ImportError:
        logger.warning(
            "google-generativeai not installed — skipping Gemini provider"
        )
        return b"", "google-generativeai package not installed"
    except Exception as exc:
        err = str(exc)
        if "429" in err or "quota" in err.lower() or "rate" in err.lower():
            return b"", f"Gemini rate limit/quota: {exc}"
        return b"", f"Gemini error: {exc}"


def _gemini_generate_sync(
    api_key: str,
    prompt: str,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> bytes:
    """Synchronous Gemini image generation (runs in a thread pool)."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)

    generation_prompt = (
        f"Generate a {width}x{height} image: {prompt}"
    )

    try:
        response = model.generate_content(
            generation_prompt,
            generation_config=genai.types.GenerationConfig(
                response_modalities=["IMAGE"],
            ),
        )
    except (TypeError, AttributeError, ValueError):
        response = model.generate_content(generation_prompt)

    if not response.candidates:
        raise RuntimeError("Gemini returned no candidates")

    for candidate in response.candidates:
        content = getattr(candidate, "content", None)
        if not content:
            continue
        for part in content.parts:
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                data = inline.data
                if isinstance(data, str):
                    import base64
                    return base64.b64decode(data)
                return bytes(data)

    raise RuntimeError(f"Gemini response contained no image for scene {scene_number}")


# ─────────────────────────────────────────────────────────────────────────────
# Provider 2 — Hugging Face
# ─────────────────────────────────────────────────────────────────────────────


async def _download_huggingface(
    client: httpx.AsyncClient,
    prompt: str,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> tuple[bytes, str | None]:
    """POST to Hugging Face FLUX.1-schnell inference API."""
    api_key = settings.huggingface_api_key
    if not api_key:
        return b"", "HUGGINGFACE_API_KEY not set"

    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "inputs": prompt,
        "parameters": {"width": width, "height": height},
    }

    try:
        response = await client.post(HF_MODEL_URL, headers=headers, json=payload)
    except httpx.TimeoutException:
        return b"", f"Request timed out after {REQUEST_TIMEOUT}s"
    except httpx.ConnectError as exc:
        return b"", f"Connection error: {exc}"
    except httpx.RequestError as exc:
        return b"", f"HTTP request error: {exc}"

    if response.status_code == 429:
        return b"", "Rate limited (HTTP 429)"
    if response.status_code == 503:
        return b"", "Hugging Face model loading (HTTP 503)"

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200] if exc.response.text else ""
        return b"", f"HTTP {exc.response.status_code}: {body}"

    content_type = response.headers.get("content-type", "").lower()
    if "image" not in content_type and "octet-stream" not in content_type:
        preview = response.text[:200] if response.text else "<binary>"
        return b"", f"Unexpected content-type '{content_type}': {preview}"

    if not response.content:
        return b"", "Hugging Face returned an empty body"

    logger.debug(
        "Scene %03d | Hugging Face | %d bytes",
        scene_number,
        len(response.content),
    )
    img_bytes = _resize_to_target(response.content, width=width, height=height)
    return img_bytes, None


# ─────────────────────────────────────────────────────────────────────────────
# Provider 3 — Pollinations
# ─────────────────────────────────────────────────────────────────────────────


_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _pollinations_headers(attempt_idx: int = 0) -> dict[str, str]:
    ua = _UA_POOL[attempt_idx % len(_UA_POOL)]
    is_firefox = "Firefox" in ua
    return {
        "User-Agent": ua,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Referer": "https://pollinations.ai/",
        "Origin": "https://pollinations.ai",
        **({} if is_firefox else {
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        }),
    }


async def _download_pollinations(
    client: httpx.AsyncClient,
    prompt: str,
    seed: int,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> tuple[bytes, str | None]:
    """GET image from Pollinations.ai (uses API key if available, otherwise rotates public endpoints)."""
    api_key = settings.pollinations_api_key

    if api_key:
        encoded_prompt = urllib.parse.quote(prompt)
        url = f"{POLLINATIONS_BASE}/{encoded_prompt}"
        params = {
            "seed": seed,
            "width": width,
            "height": height,
            "model": "flux",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code == 200 and response.content:
                img_bytes = _resize_to_target(response.content, width=width, height=height)
                return img_bytes, None
            logger.warning("Scene %03d | Authenticated Pollinations returned status %d", scene_number, response.status_code)
        except Exception as exc:
            logger.warning("Scene %03d | Authenticated Pollinations error: %s", scene_number, exc)

    logger.info("Scene %03d | Trying public Pollinations candidates...", scene_number)

    base = "https://image.pollinations.ai/prompt"
    encoded_prompt = urllib.parse.quote(prompt)
    encoded_short = urllib.parse.quote(prompt[:250])

    rand_seed = random.randint(1, 999_999)
    dims = f"width={width}&height={height}&seed={rand_seed}"

    url_candidates = [
        # Tier 1: full resolution, model variants
        (f"{base}/{encoded_prompt}?{dims}&model=flux",         0),
        (f"{base}/{encoded_prompt}?{dims}&model=flux-schnell", 1),
        (f"{base}/{encoded_prompt}?{dims}&model=turbo",        2),
        (f"{base}/{encoded_prompt}?{dims}",                    0),
        # Tier 1b: no dimensions — avoids resolution-gating
        (f"{base}/{encoded_prompt}?model=flux",                1),
        (f"{base}/{encoded_prompt}?model=turbo",               2),
        (f"{base}/{encoded_prompt}",                           0),
        # Tier 2: short prompt
        (f"{base}/{encoded_short}?{dims}&model=flux",          1),
        (f"{base}/{encoded_short}?model=flux",                 2),
        (f"{base}/{encoded_short}",                            0),
    ]

    last_error = "no candidates tried"
    for idx, (url, ua_idx) in enumerate(url_candidates):
        hdrs = _pollinations_headers(ua_idx)
        try:
            response = await client.get(url, headers=hdrs)
            if response.status_code in (402, 403):
                last_error = f"HTTP {response.status_code} on cand {idx + 1}"
                await asyncio.sleep(random.uniform(0.3, 1.0))
                continue
            if response.status_code == 429:
                last_error = "rate-limited (429)"
                await asyncio.sleep(2.0)
                continue
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            if "image" not in content_type and "octet-stream" not in content_type:
                last_error = f"bad content-type '{content_type}'"
                continue
            if not response.content:
                last_error = "empty body"
                continue

            img_bytes = _resize_to_target(response.content, width=width, height=height)
            return img_bytes, None
        except Exception as exc:
            last_error = str(exc)
            continue

    return b"", f"All Pollinations candidates failed. Last error: {last_error}"


# ─────────────────────────────────────────────────────────────────────────────
# Provider 4 — Stable Horde
# ─────────────────────────────────────────────────────────────────────────────


async def _download_stable_horde(
    prompt: str,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> tuple[bytes, str | None]:
    """Generate an image via Stable Horde (stablehorde.net).
    Uses anonymous key '0000000000', with 1024x576 dimensions.
    """
    horde_base = "https://stablehorde.net/api/v2"
    submit_headers = {
        "apikey": "0000000000",
        "Content-Type": "application/json",
        "Client-Agent": "StoryForge:1.0:storyforge",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        try:
            submit_resp = await client.post(
                f"{horde_base}/generate/async",
                headers=submit_headers,
                json={
                    "prompt": prompt[:500],
                    "params": {
                        "width": width,
                        "height": height,
                        "steps": 20,
                        "sampler_name": "k_euler",
                        "cfg_scale": 7.5,
                        "n": 1,
                    },
                    "models": ["stable_diffusion"],
                    "nsfw": False,
                    "censor_nsfw": True,
                },
            )
        except Exception as exc:
            return b"", f"Stable Horde submit failed: {exc}"

        if submit_resp.status_code != 202:
            body = submit_resp.text[:200]
            return b"", f"Stable Horde submit HTTP {submit_resp.status_code}: {body}"

        job_id = submit_resp.json().get("id", "")
        if not job_id:
            return b"", "Stable Horde: no job ID returned"

        logger.info("Scene %03d | Stable Horde job submitted: %s", scene_number, job_id)

        # Poll status for max 5 minutes (30 polls * 10 seconds)
        for poll_n in range(30):
            await asyncio.sleep(10)
            try:
                check = await client.get(f"{horde_base}/generate/check/{job_id}")
                check_data = check.json()
            except Exception as exc:
                logger.warning("Scene %03d | Stable Horde poll %d error: %s", scene_number, poll_n + 1, exc)
                continue

            if not check_data.get("done"):
                queued = check_data.get("waiting", "?")
                logger.debug("Scene %03d | Stable Horde waiting=%s (poll %d)", scene_number, queued, poll_n + 1)
                continue

            # Fetch result
            try:
                status = await client.get(f"{horde_base}/generate/status/{job_id}")
                status_data = status.json()
            except Exception as exc:
                return b"", f"Stable Horde status fetch failed: {exc}"

            generations = status_data.get("generations", [])
            if not generations:
                return b"", "Stable Horde: no generations in response"

            img_url = generations[0].get("img", "")
            if not img_url:
                return b"", "Stable Horde: empty image field"

            try:
                # The image is returned as a URL. Download it.
                img_resp = await client.get(img_url)
                img_resp.raise_for_status()
                img_bytes = _resize_to_target(img_resp.content, width=width, height=height)
                return img_bytes, None
            except Exception as exc:
                return b"", f"Stable Horde image download/resize failed: {exc}"

    return b"", "Stable Horde: timed out after 5 minutes"


# ─────────────────────────────────────────────────────────────────────────────
# Provider 5 — Hugging Face Free Serverless
# ─────────────────────────────────────────────────────────────────────────────


_HF_FREE_MODELS = [
    "stabilityai/stable-diffusion-xl-base-1.0",
    "runwayml/stable-diffusion-v1-5",
    "CompVis/stable-diffusion-v1-4",
]


async def _download_hf_free(
    prompt: str,
    scene_number: int,
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
) -> tuple[bytes, str | None]:
    """Try HuggingFace's free serverless inference API."""
    api_key = settings.huggingface_api_key
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True) as client:
        for model in _HF_FREE_MODELS:
            url = f"https://router.huggingface.co/hf-inference/models/{model}"
            try:
                response = await client.post(url, headers=headers, json={"inputs": prompt[:400]})
                if response.status_code == 503:
                    continue
                if response.status_code not in (200, 201):
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "image" not in content_type and "octet-stream" not in content_type:
                    continue
                if not response.content:
                    continue
                img_bytes = _resize_to_target(response.content, width=width, height=height)
                return img_bytes, None
            except Exception as exc:
                logger.warning("Scene %03d | HF-free %s error: %s", scene_number, model, exc)
                continue
    return b"", "HuggingFace free inference: all models failed"


# ─────────────────────────────────────────────────────────────────────────────
# Helper — PIL Placeholder
# ─────────────────────────────────────────────────────────────────────────────


def _make_placeholder_image(scene_number: int, prompt: str, *, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Generate a dark cinematic placeholder PNG using Pillow when all image APIs fail."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = width, height
    img = Image.new("RGB", (w, h), color=(10, 8, 20))
    draw = ImageDraw.Draw(img)

    for y in range(h):
        t = y / h
        r = int(18 + t * 5)
        g = int(12 + t * 4)
        b = int(35 + t * 10)
        draw.line([(0, y), (w, y)], fill=(r, g, b))

    accent = (100, 60, 180)
    for length in (60, 55):
        draw.line([(0, 0), (length, 0)], fill=accent, width=2)
        draw.line([(0, 0), (0, length)], fill=accent, width=2)
        draw.line([(w, 0), (w - length, 0)], fill=accent, width=2)
        draw.line([(w, 0), (w, length)], fill=accent, width=2)
        draw.line([(0, h), (length, h)], fill=accent, width=2)
        draw.line([(0, h), (0, h - length)], fill=accent, width=2)
        draw.line([(w, h), (w - length, h)], fill=accent, width=2)
        draw.line([(w, h), (w, h - length)], fill=accent, width=2)

    try:
        font_large = ImageFont.truetype("arial.ttf", 48)
        font_small = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    label = f"SCENE {scene_number:03d}"
    draw.text((w // 2, h // 2 - 50), label, fill=(180, 140, 255), font=font_large, anchor="mm")

    short_prompt = prompt[:120] + "…" if len(prompt) > 120 else prompt
    draw.text((w // 2, h // 2 + 30), short_prompt, fill=(140, 120, 180), font=font_small, anchor="mm")

    draw.text((w // 2, h - 28), "[placeholder — image API unavailable]", fill=(70, 55, 100), font=font_small, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Image utilities
# ─────────────────────────────────────────────────────────────────────────────


def _resize_to_target(img_bytes: bytes, *, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT) -> bytes:
    """Resize any image to the target dimensions via Pillow."""
    from PIL import Image

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
