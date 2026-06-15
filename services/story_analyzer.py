"""
services/story_analyzer.py
──────────────────────────
Step 1 of the StoryForge pipeline.

Responsibilities
────────────────
• Send the raw story text to Groq LLM (llama3-70b-8192).
• Receive a structured JSON payload describing:
    - characters  (name, age, hair, eyes, clothing, role, personality)
    - locations   (all distinct places in the story)
    - mood        (overall story mood)
    - scenes[]    (scene_number, title, text, setting, location, mood,
                   image_prompt, characters_present)
• Parse the response with json.loads().
• Validate every field with Pydantic models.
• On any parse or validation failure → retry once with an explicit
  repair prompt before returning a failure result.

Return contract
───────────────
Success:
    {
        "success": True,
        "data": {
            "scenes":           [<Scene dicts>],
            "character_memory": <CharacterMemory dict>,
            "locations":        ["...", ...],
            "mood":             "...",
        }
    }

Failure (after exhausting retries):
    { "success": False, "error": "<human-readable message>" }
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from models.character import Character, CharacterMemory
from models.scene import Scene
from utils.groq_client import llm_chat

logger = logging.getLogger("storyforge.story_analyzer")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MODEL = "llama-3.3-70b-versatile"
TEMPERATURE = 0.4          # Low temperature for deterministic structured output
MAX_TOKENS = 3000
MAX_RETRIES = 1            # One retry after first failure = two attempts total

# ─────────────────────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert story-to-video production assistant.
Your sole task is to analyse a written story and decompose it into a \
structured JSON document suitable for automated video production.

CRITICAL OUTPUT RULES — you MUST follow these exactly:
1. Respond with ONLY valid JSON. No markdown code fences (```), no prose, \
no explanation, no apologies.
2. Do NOT wrap the JSON in ```json ... ``` or any other delimiter.
3. Start your response with the opening brace { and end with }.
4. Every string value must use double quotes. Never use single quotes.
5. All lists must be properly comma-separated with square brackets.
6. The JSON must be parseable by Python's json.loads() without any \
pre-processing.

Any response that is not pure, parseable JSON is considered a failure.\
"""

# ─────────────────────────────────────────────────────────────────────────────
# Analysis prompt
# ─────────────────────────────────────────────────────────────────────────────

CHUNK_ANALYSIS_PROMPT_TEMPLATE = """\
You are analysing a segment of a larger story.
This is Chunk {chunk_index} of {total_chunks}.

KNOWN CHARACTERS SO FAR:
{known_characters}

KNOWN LOCATIONS SO FAR:
{known_locations}

Analyse the following story chunk and return a single JSON object matching this schema.
Do not add, rename, or omit any top-level keys.

JSON SCHEMA:
{{
  "mood": "<emotional tone of this chunk>",
  "locations": ["<distinct place 1 in this chunk>", "<distinct place 2 in this chunk>"],
  "characters": [
    {{
      "name":            "<full character name>",
      "gender":          "<gender of the character, e.g. 'male', 'female', 'non-binary'>",
      "age":             "<age or age range, e.g. 'mid-30s', 'teenage girl'>",
      "hair":            "<hair colour, length, and style, e.g. 'long wavy brown hair'>",
      "eyes":            "<eye colour and notable features, e.g. 'large piercing blue eyes'>",
      "facial_features": "<facial shape, skin tone, facial hair, distinct marks like scars/freckles>",
      "body_type":       "<height, build, posture, e.g. 'tall and athletic', 'slender'>",
      "clothing":        "<detailed signature outfit worn throughout the story>",
      "role":            "<exactly one of: protagonist | antagonist | supporting | minor>",
      "personality":     "<two or three personality traits>"
    }}
  ],
  "scenes": [
    {{
      "scene_number":        1,
      "title":               "<short scene title (5 words max)>",
      "text":                "<the verbatim narration text from this chunk to be read aloud>",
      "setting":             "<physical environment and atmosphere, rich and descriptive>",
      "location":            "<named place where this scene occurs>",
      "mood":                "<emotional tone of this scene>",
      "camera":              "<camera motion instruction: slow_zoom_in | slow_zoom_out | pan_left | pan_right | pan_up | pan_down | static>",
      "key_objects":         ["<exact object/prop mentioned in the narration text with details/color>", "<another object/prop>"],
      "image_prompt":        "<cinematic, high-detail structured prompt for the scene following this EXACT format: 'Subject: [detailed action, posture, and intense facial expression of subjects - e.g. wide-eyed shock, furrowed brow of desperation, weeping, or looking anxious. Show active character emotion rather than a static pose]. Environment: [specific background elements, weather, and atmosphere]. Key Objects: [exact objects, colors, and textures from the narration]. Composition: [shot type, camera angle, e.g. Close-up shot, Medium close-up, low angle]. Lighting: [lighting style, e.g. Dramatic sunlight, high-contrast shadow play]'. Describe only visuals, DO NOT use character names. Ensure all objects from 'key_objects' are explicitly described in detail here.>",
      "characters_present":  ["<character name>"]
    }}
  ]
}}

RULES:
- You MUST partition the ENTIRE story chunk into scenes sequentially. EVERY single sentence in the story chunk must be included in the "text" field of exactly one scene, in the correct order, with absolutely no sentences skipped, omitted, or modified.
- Split this chunk into between 1 and 3 scenes of roughly equal length (typically 2 to 3 scenes, or 1 scene if the chunk is very short).
- Each scene's "text" field must contain 2–4 consecutive sentences from the story chunk. Reconstructing the "text" of all scenes in order must equal the story chunk word-for-word. Do not invent, change, paraphrase, or omit any text.
- Maintain the 40-30-30 pacing ratio across scenes:
  * 40% of scenes: Focus directly on characters, using close-ups or medium shots showing facial expressions, posture, and active emotions.
  * 30% of scenes: Focus on setting details and key mystery elements (e.g., clocks, peculiar items, shadowy corners) rather than generic backgrounds.
  * 30% of scenes: Focus on establishing shots of the environment and transitions.
- For character emotions: If the narration mentions fear, shock, debt, or struggle, the image_prompt MUST describe the character's facial muscles, eyes, and physical tension to make the emotion visually strong.
- For cliffhangers or new characters: Make the entrance high impact. Use dramatic framing like a dark silhouette standing in a backlit doorway casting a long shadow, or a close-up of a hand reaching out.
- image_prompt must follow the structured format exactly. Do not use character names; describe their visual traits from the character profiles instead. Ensure it describes the key_objects.
- Select a cinematic "camera" motion instruction that matches the movement, mood, or focal point of the scene.
- Extract any physical objects, colors, or props explicitly mentioned in the narration text into "key_objects".
- If a character appears who is listed in KNOWN CHARACTERS SO FAR, reuse their name and visual profile. If a new character is introduced, provide their full profile in "characters".
- If a location appears that is listed in KNOWN LOCATIONS SO FAR, reuse it exactly.
- "locations" is the deduplicated list of places appearing in this chunk.

STORY CHUNK:
{story_chunk}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Repair prompt (used on retry after a bad first response)
# ─────────────────────────────────────────────────────────────────────────────

REPAIR_PROMPT_TEMPLATE = """\
Your previous response could not be parsed as valid JSON.
Error: {error}

Problematic response (first 500 chars):
{bad_response}

Fix the JSON and return ONLY the corrected, complete JSON object.
Do not add any explanation or markdown. Start directly with {{ and end with }}.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


async def analyze_story(story_text: str) -> dict[str, Any]:
    """
    Analyse *story_text* with the Groq LLM and return structured scene data.
    Uses chunked analysis to stay within TPM rate limits.

    Args:
        story_text: The raw story content read from the uploaded .txt file.

    Returns:
        On success::

            {
                "success": True,
                "data": {
                    "scenes":           [<Scene dicts>],
                    "character_memory": <CharacterMemory dict>,
                    "locations":        ["...", ...],
                    "mood":             "...",
                }
            }

        On failure::

            { "success": False, "error": "<message>" }
    """
    story_text = story_text.strip()
    if not story_text:
        return {"success": False, "error": "Story text is empty."}

    chunks = _chunk_story(story_text, max_words=150)
    logger.info(
        "Starting story analysis | chunks=%d | total_chars=%d | model=%s",
        len(chunks), len(story_text), MODEL
    )

    all_scenes = []
    all_characters = {}
    all_locations = set()
    all_moods = []

    for i, chunk in enumerate(chunks, start=1):
        logger.info("Analysing chunk %d/%d (words=%d) ...", i, len(chunks), len(chunk.split()))
        
        known_chars_str = _format_known_characters(list(all_characters.values()))
        known_locs_str = _format_known_locations(list(all_locations))
        
        user_prompt = CHUNK_ANALYSIS_PROMPT_TEMPLATE.format(
            chunk_index=i,
            total_chunks=len(chunks),
            known_characters=known_chars_str,
            known_locations=known_locs_str,
            story_chunk=chunk
        )

        raw, llm_error = await _call_llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        if llm_error:
            return {"success": False, "error": f"LLM call failed on chunk {i}: {llm_error}"}

        parsed, parse_error = _parse_json(raw)

        if parse_error:
            logger.warning(
                "Chunk %d LLM response failed to parse (%s). Attempting repair …",
                i,
                parse_error,
            )
            repair_prompt = REPAIR_PROMPT_TEMPLATE.format(
                error=parse_error,
                bad_response=raw[:500],
            )
            raw, llm_error = await _call_llm(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=repair_prompt,
            )
            if llm_error:
                return {"success": False, "error": f"Repair LLM call failed on chunk {i}: {llm_error}"}

            parsed, parse_error = _parse_json(raw)
            if parse_error:
                logger.error(
                    "Repair attempt for chunk %d also failed to produce valid JSON.\n"
                    "Final raw response (first 800 chars):\n%s",
                    i,
                    raw[:800],
                )
                return {
                    "success": False,
                    "error": (
                        f"LLM returned invalid JSON on chunk {i} after retry. "
                        f"Last parse error: {parse_error}"
                    ),
                }

        # Aggregate results from this chunk
        chunk_scenes = parsed.get("scenes", [])
        if not isinstance(chunk_scenes, list):
            chunk_scenes = [chunk_scenes]
        all_scenes.extend(chunk_scenes)

        for char in parsed.get("characters", []):
            if isinstance(char, dict) and "name" in char:
                name = str(char["name"]).strip()
                if name.lower() not in all_characters:
                    all_characters[name.lower()] = char
                else:
                    existing = all_characters[name.lower()]
                    for key, val in char.items():
                        if val and (not existing.get(key) or len(str(val)) > len(str(existing.get(key)))):
                            existing[key] = val

        for loc in parsed.get("locations", []):
            if loc:
                all_locations.add(str(loc).strip())

        mood = parsed.get("mood")
        if mood:
            all_moods.append(str(mood).strip())

        # Inter-chunk rate limit cooldown sleep
        if i < len(chunks):
            await asyncio.sleep(3)

    # Build the combined payload
    combined = {
        "mood": ", ".join(sorted(list(set(all_moods)))),
        "locations": sorted(list(all_locations)),
        "characters": list(all_characters.values()),
        "scenes": all_scenes
    }

    # Re-number scenes sequentially
    for idx, scene in enumerate(combined["scenes"], start=1):
        if isinstance(scene, dict):
            scene["scene_number"] = idx

    # ── Validate with Pydantic ───────────────────────────────────────────────
    validated, validation_error = _validate(combined)
    if validation_error:
        logger.error("Pydantic validation failed: %s", validation_error)
        return {"success": False, "error": f"Validation error: {validation_error}"}

    scenes, character_memory, locations, mood = validated

    logger.info(
        "Story analysis complete | scenes=%d | characters=%d | locations=%d",
        len(scenes),
        len(character_memory.characters),
        len(locations),
    )

    return {
        "success": True,
        "data": {
            "scenes":           [s.model_dump() for s in scenes],
            "character_memory": character_memory.model_dump(),
            "locations":        locations,
            "mood":             mood,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────


def _chunk_story(story_text: str, max_words: int = 400) -> list[str]:
    """Split story by sentences into chunks of at most max_words words."""
    # Split text into sentences using simple regex
    sentences = re.split(r'(?<=[.!?])\s+', story_text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]
    
    chunks = []
    current_chunk = []
    current_word_count = 0
    
    for sentence in sentences:
        words = sentence.split()
        s_words = len(words)
        
        # If a single sentence is longer than max_words, split it by words
        if s_words > max_words:
            if current_chunk:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
                current_word_count = 0
            for i in range(0, s_words, max_words):
                sub_words = words[i : i + max_words]
                chunks.append(" ".join(sub_words))
            continue
            
        if current_word_count + s_words > max_words and current_chunk:
            chunks.append(" ".join(current_chunk))
            current_chunk = [sentence]
            current_word_count = s_words
        else:
            current_chunk.append(sentence)
            current_word_count += s_words
            
    if current_chunk:
        chunks.append(" ".join(current_chunk))
        
    return chunks


def _format_known_characters(characters: list[dict]) -> str:
    """Format already extracted characters for inclusion in subsequent prompts."""
    if not characters:
        return "None (this is the first chunk)"
    lines = []
    for c in characters:
        lines.append(
            f"- {c.get('name')} ({c.get('role')}): gender={c.get('gender')}, age={c.get('age')}, hair={c.get('hair')}, "
            f"eyes={c.get('eyes')}, facial_features={c.get('facial_features')}, body_type={c.get('body_type')}, "
            f"clothing={c.get('clothing')}, personality={c.get('personality')}"
        )
    return "\n".join(lines)


def _format_known_locations(locations: list[str]) -> str:
    """Format already extracted locations for inclusion in subsequent prompts."""
    if not locations:
        return "None (this is the first chunk)"
    return ", ".join(locations)


async def _call_llm(
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str | None]:
    """
    Wrapper around llm_chat that catches all exceptions.

    Returns:
        (raw_response, None)        on success
        ("",           error_msg)   on failure
    """
    try:
        raw = await llm_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=MODEL,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
        logger.debug(
            "LLM response received | chars=%d | preview=%.120s",
            len(raw),
            raw,
        )
        return raw, None
    except Exception as exc:
        logger.exception("Groq LLM call raised an exception.")
        return "", str(exc)


def _parse_json(raw: str) -> tuple[dict | None, str | None]:
    """
    Clean the raw LLM response and attempt json.loads().

    Cleaning steps (in order):
      1. Strip leading/trailing whitespace.
      2. Remove markdown code fences (``` or ```json … ```).
      3. Extract the first {...} block if there is surrounding text.
      4. Attempt json.loads().

    Returns:
        (parsed_dict, None)         on success
        (None,        error_string)  on failure
    """
    cleaned = _clean_raw_response(raw)

    if not cleaned:
        return None, "LLM returned an empty response."

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Try to extract a JSON object substring as a last resort
        extracted = _extract_json_object(cleaned)
        if extracted:
            try:
                parsed = json.loads(extracted)
            except json.JSONDecodeError as exc2:
                return None, f"JSONDecodeError after extraction attempt: {exc2}"
        else:
            return None, f"JSONDecodeError: {exc}"

    if not isinstance(parsed, dict):
        return None, f"Expected a JSON object (dict), got {type(parsed).__name__}."

    return parsed, None


def _validate(
    parsed: dict,
) -> tuple[
    tuple[list[Scene], CharacterMemory, list[str], str] | None,
    str | None,
]:
    """
    Validate the parsed dict against the Pydantic models.

    Validates:
      - Each entry in parsed["characters"]  → Character
      - Collected characters               → CharacterMemory
      - Each entry in parsed["scenes"]     → Scene
      - Top-level "locations" and "mood"

    Returns:
        ((scenes, character_memory, locations, mood), None)  on success
        (None, error_string)                                  on failure
    """
    errors: list[str] = []

    # ── Characters ────────────────────────────────────────────────────────────
    characters: list[Character] = []
    for i, raw_char in enumerate(parsed.get("characters", [])):
        try:
            characters.append(Character(**raw_char))
        except (ValidationError, TypeError) as exc:
            errors.append(f"characters[{i}]: {exc}")

    if errors:
        return None, "; ".join(errors)

    character_memory = CharacterMemory(characters=characters)

    # ── Scenes ────────────────────────────────────────────────────────────────
    scenes_raw: list[dict] = parsed.get("scenes", [])
    if not scenes_raw:
        return None, "LLM returned zero scenes."

    scenes: list[Scene] = []
    for i, raw_scene in enumerate(scenes_raw):
        try:
            # Normalise: accept "scene" as an alias for "scene_number"
            if "scene_number" not in raw_scene and "scene" in raw_scene:
                raw_scene = dict(raw_scene)
                raw_scene["scene_number"] = raw_scene.pop("scene")

            # Normalise: accept "narration" as an alias for "text" in case the
            # LLM used the old field name despite the prompt instructions.
            if "text" not in raw_scene and "narration" in raw_scene:
                raw_scene = dict(raw_scene)        # don't mutate original
                raw_scene["text"] = raw_scene.pop("narration")

            # Provide a sensible default for "setting" if the LLM omitted it
            if "setting" not in raw_scene or not raw_scene["setting"]:
                raw_scene = dict(raw_scene)
                raw_scene["setting"] = raw_scene.get("location", "unspecified setting")

            scenes.append(Scene(**raw_scene))
        except (ValidationError, TypeError) as exc:
            errors.append(f"scenes[{i}] (scene_number={raw_scene.get('scene_number', '?')}): {exc}")

    if errors:
        return None, "; ".join(errors)

    # Enforce ascending scene_number order
    scenes.sort(key=lambda s: s.scene_number)

    # ── Locations & mood ──────────────────────────────────────────────────────
    locations: list[str] = parsed.get("locations", [])
    if not isinstance(locations, list):
        locations = [str(locations)]
    locations = [str(loc).strip() for loc in locations if loc]

    mood: str = str(parsed.get("mood", "")).strip()

    return (scenes, character_memory, locations, mood), None


# ─────────────────────────────────────────────────────────────────────────────
# String-cleaning utilities
# ─────────────────────────────────────────────────────────────────────────────


def _clean_raw_response(text: str) -> str:
    """
    Progressively clean the raw LLM text to isolate valid JSON.

    Steps
    ─────
    1.  Strip whitespace.
    2.  Remove ```json … ``` or ``` … ``` fences (greedy, multiline).
    3.  Strip any remaining backticks.
    4.  Strip the word "json" if the model prepended it.
    5.  Strip trailing commas before } or ] (common LLM mistake).
    6.  Return the stripped result.
    """
    text = text.strip()

    # Remove code fences (handles ``` at start/end and ```json variant)
    text = re.sub(
        r"^```(?:json)?\s*\n?",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip("`").strip()

    # Strip a leading "json" word if the LLM forgot the fence but added the tag
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)

    # Fix trailing commas before closing braces/brackets (invalid JSON)
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return text.strip()


def _extract_json_object(text: str) -> str | None:
    """
    Find the first top-level { … } block in *text* using bracket counting.

    Used as a last-resort extraction if the LLM surrounded the JSON with
    extra prose even after cleaning.

    Returns the extracted substring, or None if no balanced block found.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None
