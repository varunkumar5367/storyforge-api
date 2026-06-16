import asyncio
import edge_tts

async def test():
    text = "Hello world! This is a test of edge tts with rate and pitch."
    voice = "en-US-JennyNeural"
    # test rate and pitch formatting
    communicate = edge_tts.Communicate(text, voice, rate="+10%", pitch="-5Hz")
    
    audio_data = b""
    try:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data += chunk["data"]
        print(f"Success! Generated {len(audio_data)} bytes of audio.")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test())
