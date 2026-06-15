# scratch/test_story_analysis.py
import asyncio
import sys
import json
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from services.story_analyzer import analyze_story

async def main():
    print("--- STARTING STORY ANALYSIS VERIFICATION ---")
    
    story_file = Path(__file__).parent / "test_story.txt"
    if not story_file.exists():
        print(f"Error: test story file not found at {story_file}")
        return

    with open(story_file, "r", encoding="utf-8") as f:
        story_text = f.read()

    print(f"Loaded story text: {len(story_text)} characters, approximately {len(story_text.split())} words.")
    
    print("Running analyze_story pipeline step...")
    try:
        result = await analyze_story(story_text)
        
        if result["success"]:
            print("\n[SUCCESS] STORY ANALYSIS SUCCESSFUL!")
            data = result["data"]
            scenes = data["scenes"]
            chars = data["character_memory"].get("characters", [])
            locs = data.get("locations", [])
            mood = data.get("mood", "")
            
            print(f"Total Scenes Extracted: {len(scenes)}")
            print(f"Total Characters Identified: {len(chars)}")
            print(f"Total Locations Found: {len(locs)}")
            print(f"Overall Mood: {mood}")
            
            print("\nFirst Scene Details:")
            if scenes:
                fs = scenes[0]
                print(f"  Scene #{fs.get('scene_number')}: {fs.get('title')}")
                print(f"  Verbatim Text: {fs.get('text')}")
                print(f"  Setting: {fs.get('setting')}")
                print(f"  Image Prompt: {fs.get('image_prompt')}")
            
            # Print the total number of characters in the JSON output
            json_str = json.dumps(data)
            print(f"\nSize of output JSON data: {len(json_str)} characters (~{len(json_str) // 4} tokens).")
            
        else:
            print(f"\n[FAILURE] STORY ANALYSIS FAILED: {result.get('error')}")
            
    except Exception as exc:
        print(f"\n[EXCEPTION] EXCEPTION OCCURRED DURING ANALYSIS: {exc}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
