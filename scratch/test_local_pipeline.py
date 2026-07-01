"""
scratch/test_local_pipeline.py — Verification script for the local GPU/CPU AI model pipeline.
Tests LLM (Ollama), Image Generator (Diffusers), and Subtitle Generator (faster-whisper) sequentially.
"""

import asyncio
import os
import sys
import logging
from pathlib import Path
import torch

# Set up paths so we can import from parent directory
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config import settings
from utils.groq_client import llm_chat, transcribe_audio
from services.image_generator import _download_local_diffusers

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_local_pipeline")

async def test_llm():
    logger.info("=== Testing Local LLM (Ollama) ===")
    system_prompt = "You are a creative writer. Output a single short sentence."
    user_prompt = "Tell me a story about a little robot in a big world."
    
    try:
        response = await llm_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
            max_tokens=50,
        )
        logger.info("LLM Response: %s", response)
        logger.info("Local LLM test: SUCCESS\n")
        return True
    except Exception as e:
        logger.error("Local LLM test: FAILED | Error: %s\n", e)
        return False

async def test_image_generation(use_tiny_model=True):
    logger.info("=== Testing Local Image Gen (Diffusers) ===")
    
    original_model = settings.local_image_model
    if use_tiny_model:
        # Use a 2MB tiny test model to verify pipeline and CUDA execution quickly without massive downloads
        test_model = "hf-internal-testing/tiny-stable-diffusion-torch"
        logger.info("Using tiny model for fast verification: %s", test_model)
        settings.local_image_model = test_model
    else:
        logger.info("Using configured model: %s", original_model)
        
    prompt = "a cute little robot standing in a green forest, 16:9, anime style"
    seed = 42
    
    try:
        img_bytes, err = await _download_local_diffusers(
            prompt=prompt,
            seed=seed,
            scene_number=1,
            width=512 if use_tiny_model else 1280,
            height=512 if use_tiny_model else 720,
        )
        
        if err:
            logger.error("Local Image Gen: FAILED | Error: %s\n", err)
            return False
            
        logger.info("Generated image bytes size: %d bytes", len(img_bytes))
        
        # Save output image
        out_dir = Path(__file__).resolve().parent / "test_outputs"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "test_image.png"
        out_path.write_bytes(img_bytes)
        logger.info("Saved test image to: %s", out_path)
        logger.info("Local Image Gen test: SUCCESS\n")
        return True
    except Exception as e:
        logger.error("Local Image Gen test: FAILED | Error: %s\n", e)
        return False
    finally:
        settings.local_image_model = original_model

async def test_whisper():
    logger.info("=== Testing Local Whisper (faster-whisper) ===")
    
    # We need a small audio file to test. Let's create a dummy silent wav or look for a file.
    # We can write a 1-second silent WAV file using wave module.
    import wave
    import struct
    
    out_dir = Path(__file__).resolve().parent / "test_outputs"
    out_dir.mkdir(exist_ok=True)
    audio_path = out_dir / "test_silent.wav"
    
    logger.info("Creating a 1-second silent test WAV file at: %s", audio_path)
    with wave.open(str(audio_path), "wb") as wav_file:
        wav_file.setparams((1, 2, 16000, 16000, "NONE", "not compressed"))
        # 16000 samples of silence (16-bit 0)
        for _ in range(16000):
            wav_file.writeframes(struct.pack("h", 0))
            
    try:
        # Note: faster-whisper default is small. To run fast in test we can temporarily override settings
        original_whisper_model = settings.local_whisper_model
        # Use 'tiny' for fast test download/run (only 75 MB)
        settings.local_whisper_model = "tiny"
        logger.info("Using 'tiny' Whisper model for fast verification (75 MB)")
        
        result = await transcribe_audio(
            audio_path=audio_path,
            language="en"
        )
        
        logger.info("Transcription Result Keys: %s", list(result.keys()))
        logger.info("Transcription Text: '%s'", result.get("text"))
        logger.info("Segments Count: %d", len(result.get("segments", [])))
        logger.info("Words Count: %d", len(result.get("words", [])))
        logger.info("Local Whisper test: SUCCESS\n")
        return True
    except Exception as e:
        logger.error("Local Whisper test: FAILED | Error: %s\n", e)
        return False
    finally:
        settings.local_whisper_model = original_whisper_model

async def main():
    logger.info("CUDA Device Name: %s", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "None")
    
    llm_ok = await test_llm()
    img_ok = await test_image_generation(use_tiny_model=True)
    whisper_ok = await test_whisper()
    
    logger.info("=== Final Results ===")
    logger.info("Local LLM (Ollama): %s", "PASSED" if llm_ok else "FAILED")
    logger.info("Local Image Gen (Diffusers): %s", "PASSED" if img_ok else "FAILED")
    logger.info("Local Whisper (faster-whisper): %s", "PASSED" if whisper_ok else "FAILED")
    
    if llm_ok and img_ok and whisper_ok:
        logger.info("All local GPU checks PASSED! Codebase is ready for local production.")
        sys.exit(0)
    else:
        logger.error("Some checks FAILED. Please review the errors above.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
