# StoryForge AI — Backend API

> Converts a written story (.txt) into a complete YouTube-ready video automatically.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Framework | FastAPI + Uvicorn |
| Database | SQLite via aiosqlite |
| LLM | Groq API (llama3-70b-8192) |
| Images | Hugging Face Inference API (FLUX.1-schnell) |
| TTS | VoiceForge API |
| Subtitles | Groq Whisper (whisper-large-v3) |
| Video | FFmpeg (subprocess) |
| Thumbnails | Pillow |
| Hosting | Render.com (free tier) |

---

## Pipeline

```
story.txt
    │
    ▼
1. Story Analyzer      (Groq LLM)       → scenes[], character_memory
    │
    ▼
2. Image Generator     (Hugging Face)   → scene_001.png … scene_NNN.png
    │
    ▼
3. Voice Generator     (VoiceForge TTS) → scene_001.mp3 … scene_NNN.mp3
    │
    ▼
4. Subtitle Generator  (Groq Whisper)   → scene_001.srt … scene_NNN.srt
    │
    ▼
5. Video Composer      (FFmpeg)         → episode.mp4
    │
    ▼
6. Metadata Generator  (Groq LLM)       → title.txt, description.txt, hashtags.txt
    │
    ▼
7. Thumbnail Generator (Pillow)         → thumbnail.png
```

---

## Project Structure

```
storyforge-api/
├── main.py                  ← FastAPI app entry point
├── database.py              ← SQLite async CRUD layer
├── requirements.txt
├── render.yaml              ← Render.com deployment config
├── .env                     ← Secrets (not committed)
│
├── models/
│   ├── job.py               ← Job request/response pydantic models
│   ├── scene.py             ← Scene pydantic model
│   └── character.py         ← Character + CharacterMemory models
│
├── utils/
│   ├── groq_client.py       ← Async Groq LLM + Whisper client
│   └── file_handler.py      ← I/O helpers, path conventions, URL builder
│
├── services/
│   ├── story_analyzer.py    ← Step 1: LLM story decomposition
│   ├── image_generator.py   ← Step 2: Pollinations.ai image fetching
│   ├── voice_generator.py   ← Step 3: VoiceForge TTS
│   ├── subtitle_generator.py← Step 4: Groq Whisper → SRT
│   ├── video_composer.py    ← Step 5: FFmpeg Ken Burns + xfade
│   ├── metadata_generator.py← Step 6: LLM YouTube metadata
│   ├── thumbnail_generator.py←Step 7: Pillow thumbnail overlay
│   └── orchestrator.py      ← Master pipeline runner
│
├── routes/
│   ├── analyze.py           ← POST /api/analyze/upload
│   ├── generate.py          ← POST /api/generate/{step}/{job_id}
│   ├── status.py            ← GET  /api/status/{job_id}
│   └── download.py          ← GET  /api/download/{job_id}
│
└── output/
    └── {job_id}/
        ├── images/           scene_001.png …
        ├── audio/            scene_001.mp3 …
        ├── subtitles/        scene_001.srt …
        └── final/
            ├── episode.mp4
            ├── thumbnail.png
            ├── title.txt
            ├── description.txt
            └── hashtags.txt
```

---

## Quick Start

### 1. Prerequisites
- Python 3.11+
- FFmpeg installed and on PATH
- Groq API key

### 2. Install

```bash
cd storyforge-api
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and add your GROQ_API_KEY
```

### 4. Run

```bash
uvicorn main:app --reload --port 8000
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) for the interactive API docs.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/analyze/upload` | Upload story .txt, start pipeline |
| `GET`  | `/api/status/{job_id}` | Poll job progress |
| `GET`  | `/api/status/` | List all jobs |
| `GET`  | `/api/download/{job_id}` | Get output file URLs |
| `POST` | `/api/generate/images/{job_id}` | Re-run image generation |
| `POST` | `/api/generate/voices/{job_id}` | Re-run voice generation |
| `POST` | `/api/generate/subtitles/{job_id}` | Re-run subtitle generation |
| `POST` | `/api/generate/video/{job_id}` | Re-run video composition |
| `POST` | `/api/generate/metadata/{job_id}` | Re-run metadata generation |
| `POST` | `/api/generate/thumbnail/{job_id}` | Re-run thumbnail generation |

---

## Deploying to Render.com

1. Push the repo to GitHub.
2. Create a new **Web Service** on Render and connect your repo.
3. Render detects `render.yaml` and auto-configures the service.
4. Set the `GROQ_API_KEY` environment variable in the Render dashboard.
5. The free-tier disk is mounted at `/opt/render/project/src/output`.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key (required) |
| `HUGGINGFACE_API_KEY` | Hugging Face Access Token (required for stable image generation) |
| `VOICEFORGE_URL` | VoiceForge base URL |
| `OUTPUT_DIR` | Local output directory (default: `./output`) |
| `DATABASE_URL` | SQLite file path (default: `./storyforge.db`) |
| `FRONTEND_URL` | Frontend URL for CORS whitelist |

---

## TODO / Next Steps

- [ ] Rate limiting on upload endpoint
- [ ] Job cancellation endpoint (`DELETE /api/jobs/{job_id}`)
- [ ] WebSocket progress streaming (instead of polling)
- [ ] Multi-language TTS support
- [ ] Scene-level SRT timestamp merging for full-video subtitle track
- [ ] FFmpeg filter graph via ffmpeg-python library for robustness
- [ ] Background music layer (royalty-free audio mixing)
- [ ] Multiple episode support (playlist generation)
