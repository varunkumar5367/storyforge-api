"""
models/scene.py — Pydantic models representing a single story scene.

Schema matches story_analyzer LLM output:
  scene_number, title, text (narration), setting, location,
  mood, image_prompt, characters_present
"""

from __future__ import annotations

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
        description="Detailed, vivid Pollinations.ai image generation prompt"
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
