# scratch/test_imagen.py
import os
import sys
from pathlib import Path

# Add backend to python path
sys.path.append(str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

def main():
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        api_key = api_key.strip()
    print(f"API Key: '{api_key}'")

    client = genai.Client(api_key=api_key)
    
    # Try generating an image using imagen-3.0-generate-002
    try:
        print("Generating with imagen-3.0-generate-002...")
        response = client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt='A serene Japanese garden with cherry blossoms, anime fantasy style, cinematic lighting',
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="16:9",
                output_mime_type='image/png'
            )
        )
        print("Success with imagen-3.0!")
        print("Images returned:", len(response.generated_images))
        img = response.generated_images[0].image
        img.save("scratch/test_imagen_3.png")
        print("Saved scratch/test_imagen_3.png")
    except Exception as e:
        print("Error with imagen-3.0:", e)

    # Try generating an image using imagen-4.0-generate-001
    try:
        print("Generating with imagen-4.0-generate-001...")
        response = client.models.generate_images(
            model='imagen-4.0-generate-001',
            prompt='A serene Japanese garden with cherry blossoms, anime fantasy style, cinematic lighting',
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="16:9",
                output_mime_type='image/png'
            )
        )
        print("Success with imagen-4.0!")
        print("Images returned:", len(response.generated_images))
        img = response.generated_images[0].image
        img.save("scratch/test_imagen_4.png")
        print("Saved scratch/test_imagen_4.png")
    except Exception as e:
        print("Error with imagen-4.0:", e)

if __name__ == "__main__":
    main()
