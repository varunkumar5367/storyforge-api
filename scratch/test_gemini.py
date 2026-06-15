# scratch/test_gemini.py
import asyncio
import os
import sys
from pathlib import Path

# Add backend to python path
sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

async def main():
    import google.generativeai as genai
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        api_key = api_key.strip()
    print(f"API Key: '{api_key}'")
    
    genai.configure(api_key=api_key)
    # List models to see if key works
    try:
        models = genai.list_models()
        print("Models list successful!")
        for m in list(models)[:5]:
            print(m.name)
    except Exception as e:
        print("Error listing models:", e)

    # Try image generation
    try:
        model = genai.GenerativeModel("gemini-2.5-flash-preview-05-20")
        response = model.generate_content(
            "Generate a cinematic 1280x720 (16:9) image: A beautiful sunset over a fantasy city.",
            generation_config=genai.types.GenerationConfig(
                response_modalities=["IMAGE"],
            ),
        )
        print("Generation call returned!")
        print("Candidates count:", len(response.candidates) if response.candidates else 0)
    except Exception as e:
        print("Error generating image:", e)

if __name__ == "__main__":
    asyncio.run(main())
