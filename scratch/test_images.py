# scratch/test_images.py
import asyncio
import logging
import sys
from pathlib import Path

# Add backend to python path
sys.path.append(str(Path(__file__).parent.parent))

from database import get_job
from services.image_generator import generate_images

logging.basicConfig(level=logging.INFO)

async def main():
    job_id = "ebf35e0b-919e-461b-aa5a-70eba5560795"
    job = await get_job(job_id)
    if not job:
        print("Job not found!")
        return
        
    import json
    scenes = json.loads(job["scenes"])
    char_mem = json.loads(job["character_memory"])
    
    # Pre-populate missing fields for older database schemas during testing
    if "characters" in char_mem:
        for char in char_mem["characters"]:
            char.setdefault("gender", "male")
            char.setdefault("hair", "short dark hair")
            char.setdefault("eyes", "brown eyes")
            char.setdefault("facial_features", "regular face")
            char.setdefault("body_type", "slender")
            char.setdefault("clothing", "casual clothes")
            char.setdefault("role", "protagonist")
            
    print(f"Triggering image generation for {len(scenes)} scenes...")
    result = await generate_images(job_id, scenes, char_mem)
    print("SUCCESS:", result["success"])
    print("FAILED SCENES:", result["data"].get("failed_scenes"))
    print("IMAGE PATHS:", result["data"].get("image_paths"))

if __name__ == "__main__":
    asyncio.run(main())
