"""
services/metadata_generator.py
────────────────────────────────
Step 6 of the pipeline: generate YouTube metadata with Groq LLM.

Outputs:
  • title.txt        — catchy YouTube video title
  • description.txt  — full YouTube description with timestamps
  • hashtags.txt     — comma-separated hashtags

Returns:
  { "success": True,  "data": { "title": "...", "description": "...", "hashtags": "..." } }
  or { "success": False, "error": "..." }
"""

from __future__ import annotations

import logging
import re

from models.scene import Scene
from utils.groq_client import llm_chat
from utils.file_handler import (
    final_title_path,
    final_description_path,
    final_hashtags_path,
    write_text,
)

logger = logging.getLogger("storyforge.metadata_generator")

SYSTEM_PROMPT = """You are an expert YouTube content strategist specialising
in story-driven narrative videos. Your job is to write compelling, SEO-optimised
metadata that maximises click-through rate and watch time.

Always respond with ONLY valid JSON — no markdown fences, no extra commentary.
"""

USER_PROMPT_TEMPLATE = """Generate YouTube metadata for the following story video.

Story summary:
{story_summary}

Scenes:
{scene_summaries}

Return a JSON object with this exact structure:
{{
  "title": "Compelling YouTube title (max 70 chars, no clickbait)",
  "description": "Full YouTube description (300-500 words). Include:\\n- Hook (first 2 lines)\\n- Story synopsis\\n- Timestamps (one per scene)\\n- Call to action",
  "hashtags": "#tag1 #tag2 #tag3 ... (15-20 relevant hashtags)"
}}
"""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------
async def generate_metadata(
    job_id: str,
    story_text: str,
    scenes: list[dict],
) -> dict:
    """
    Generate YouTube title, description, and hashtags for the video.

    Args:
        job_id:     Unique job identifier.
        story_text: Original story text.
        scenes:     List of scene dicts.

    Returns:
        {
            "success": True,
            "data": {
                "title": "...",
                "description": "...",
                "hashtags": "..."
            }
        }
        or { "success": False, "error": "..." }
    """
    logger.info("Generating YouTube metadata [job=%s] …", job_id)

    story_summary = story_text[:800].strip()
    scene_summaries = _build_scene_summary(scenes)

    try:
        raw = await llm_chat(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=USER_PROMPT_TEMPLATE.format(
                story_summary=story_summary,
                scene_summaries=scene_summaries,
            ),
            temperature=0.7,
            max_tokens=1024,
        )
    except Exception as exc:
        return {"success": False, "error": f"LLM call failed: {exc}"}

    # Parse JSON response
    try:
        import json
        json_text = _strip_fences(raw)
        parsed = json.loads(json_text)
    except Exception as exc:
        logger.error("Failed to parse metadata JSON: %s\nRaw: %s", exc, raw[:300])
        # Fallback: save raw text as title
        parsed = {
            "title": "An Epic Story — Full Video",
            "description": raw,
            "hashtags": "#story #narration #youtube",
        }

    title = parsed.get("title", "").strip()
    description = parsed.get("description", "").strip()
    hashtags = parsed.get("hashtags", "").strip()

    # Persist to disk
    await write_text(final_title_path(job_id), title)
    await write_text(final_description_path(job_id), description)
    await write_text(final_hashtags_path(job_id), hashtags)

    logger.info("Metadata saved for job %s.", job_id)
    return {
        "success": True,
        "data": {"title": title, "description": description, "hashtags": hashtags},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_scene_summary(scenes: list[dict]) -> str:
    lines = []
    for s in scenes:
        scene = Scene(**s)
        lines.append(
            f"  Scene {scene.scene_number}: {scene.title} — {scene.mood} — {scene.location}"
        )
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    return text.strip()
