# scratch/test_api_endpoints.py
import asyncio
import httpx
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config import settings

async def test_pollinations():
    print("--- TESTING POLLINATIONS ---")
    base = "https://image.pollinations.ai/prompt"
    prompt = "a beautiful anime fantasy illustration of a quiet street at sunset, cinematic lighting, 16:9"
    
    # Try different models and parameters
    scenarios = [
        ("No model, no key", f"{base}/{prompt}?width=1024&height=576", {}),
        ("Flux model, no key", f"{base}/{prompt}?width=1024&height=576&model=flux", {}),
        ("Sana model, no key", f"{base}/{prompt}?width=1024&height=576&model=sana", {}),
        ("Turbo model, no key", f"{base}/{prompt}?width=1024&height=576&model=turbo", {}),
    ]
    
    # Add key scenarios if key exists in config
    if settings.pollinations_api_key:
        print(f"Pollinations key found: {settings.pollinations_api_key[:10]}...")
        scenarios.append(("Flux model with key", f"{base}/{prompt}?width=1024&height=576&model=flux", {"Authorization": f"Bearer {settings.pollinations_api_key}"}))
    else:
        print("No Pollinations key found in config.")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        for name, url, headers in scenarios:
            print(f"\nRunning scenario: {name}")
            print(f"URL: {url}")
            try:
                resp = await client.get(url, headers=headers)
                print(f"Status Code: {resp.status_code}")
                print(f"Content-Type: {resp.headers.get('content-type')}")
                if resp.status_code != 200:
                    print(f"Error Response Body: {resp.text[:500]}")
                else:
                    print(f"Success! Received {len(resp.content)} bytes.")
            except Exception as e:
                print(f"Exception: {e}")

async def test_huggingface():
    print("\n--- TESTING HUGGING FACE ---")
    prompt = "a beautiful anime fantasy illustration of a quiet street at sunset, cinematic lighting, 16:9"
    
    # 1. Paid API with the key in config
    paid_url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
    key = settings.huggingface_api_key
    print(f"HF Key in config: {key[:10] if key else 'None'}...")
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        if key:
            print("\nPaid model with current key:")
            try:
                resp = await client.post(
                    paid_url,
                    headers={"Authorization": f"Bearer {key}"},
                    json={"inputs": prompt, "parameters": {"width": 1024, "height": 576}}
                )
                print(f"Status Code: {resp.status_code}")
                if resp.status_code != 200:
                    print(f"Response Body: {resp.text[:500]}")
                else:
                    print(f"Success! Received {len(resp.content)} bytes.")
            except Exception as e:
                print(f"Exception: {e}")
                
        # 2. Free model (XL base 1.0) with current key vs no key
        free_url = "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-xl-base-1.0"
        
        for name, headers in [
            ("Free model with current key", {"Authorization": f"Bearer {key}"} if key else {}),
            ("Free model without key", {}),
        ]:
            print(f"\n{name}:")
            try:
                resp = await client.post(
                    free_url,
                    headers=headers,
                    json={"inputs": prompt}
                )
                print(f"Status: {resp.status_code}")
                if resp.status_code != 200:
                    print(f"Response: {resp.text[:500]}")
                else:
                    print(f"Success! Received {len(resp.content)} bytes.")
            except Exception as e:
                print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_pollinations())
    asyncio.run(test_huggingface())
