"""
models/scene.py — Pydantic models representing a single story scene.

Schema matches story_analyzer LLM output:
  scene_number, title, text (narration), setting, location,
  mood, image_prompt, characters_present
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Scene(BaseModel):
    """
    One narrative scene extracted from the story by the LLM.

    Key fields:
      text              — the narration read aloud by TTS (alias: narration)
      setting           — physical environment / atmosphere description
      location          — named place or area (e.g. "abandoned lighthouse")
      image_prompt      — full Pollinations.ai prompt for this scene
      characters_present— list of character names visible in this scene
    """

    scene_number: int = Field(description="1-indexed scene position")
    title: str = Field(default="Untitled Scene", description="Short, descriptive scene title")

    # Primary narration field; 'text' is accepted as an alias from the LLM
    text: str = Field(
        default="",
        description="The narration text read aloud verbatim by TTS"
    )
    # 'narration' is kept as a computed property for backward-compat with
    # other services that reference scene.narration
    narration: str = Field(
        default="",
        description="Alias of text — populated automatically by model_validator",
    )

    setting: str = Field(
        default="unspecified setting",
        description=(
            "Physical environment and atmosphere of the scene, "
            "e.g. 'a dimly lit stone dungeon with flickering torches'"
        )
    )
    location: str = Field(
        default="unspecified location",
        description="Named place or area where the scene takes place"
    )
    mood: str = Field(
        default="neutral",
        description="Emotional tone of the scene: e.g. tense, joyful, mysterious"
    )
    image_prompt: str = Field(
        default="a storytelling scene",
        description="Detailed, vivid image generation prompt"
    )
    camera: str = Field(
        default="slow_zoom_in",
        description="Camera instruction: slow_zoom_in | slow_zoom_out | pan_left | pan_right | pan_up | pan_down | static"
    )
    key_objects: list[str] = Field(
        default_factory=list,
        description="Key objects/props present in this scene"
    )
    characters_present: list[str] = Field(
        default_factory=list,
        description="Character names appearing in this scene",
    )
    duration_hint: float | None = Field(
        default=None,
        description="Estimated scene duration in seconds (filled after TTS)",
    )

    # Populated during the pipeline — not from LLM
    image_path: str | None = None
    image_provider: str | None = None
    audio_path: str | None = None
    subtitle_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def sanitize_llm_output(cls, data: Any) -> Any:
        """
        Robustly coerce LLM output quirks before Pydantic validation runs.

        Handles two common LLM hallucination patterns:
          1. String fields returned as a list  → joined with ', '
          2. characters_present items returned as dicts → name extracted
        """
        if not isinstance(data, dict):
            return data

        # ── 1. Coerce string fields that arrived as lists ─────────────────
        str_fields = ("location", "setting", "mood", "title", "text", "narration",
                      "image_prompt", "camera")
        for field in str_fields:
            val = data.get(field)
            if isinstance(val, list):
                # Join non-empty string items; fall back to empty string
                data[field] = ", ".join(str(v) for v in val if v) or ""

        # ── 2. Coerce characters_present items that arrived as dicts ──────
        chars = data.get("characters_present")
        if isinstance(chars, list):
            cleaned = []
            for c in chars:
                if isinstance(c, dict):
                    name = c.get("name") or c.get("character") or str(c)
                    cleaned.append(str(name))
                else:
                    cleaned.append(str(c))
            data["characters_present"] = cleaned

        # ── 3. Coerce key_objects items that arrived as dicts ─────────────
        key_objs = data.get("key_objects")
        if isinstance(key_objs, list):
            cleaned_objs = []
            for obj in key_objs:
                if isinstance(obj, dict):
                    label = obj.get("name") or obj.get("object") or str(obj)
                    cleaned_objs.append(str(label))
                else:
                    cleaned_objs.append(str(obj))
            data["key_objects"] = cleaned_objs

        return data

    @model_validator(mode="after")
    def sync_narration(self) -> "Scene":
        """
        Keep `narration` in sync with `text` so downstream services
        (voice_generator, subtitle_generator) can use either attribute.
        """
        if not self.narration:
            self.narration = self.text
        elif not self.text:
            self.text = self.narration
        return self


class SceneList(BaseModel):
    scenes: list[Scene]
