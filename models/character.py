"""
models/character.py — Pydantic models for character memory.

Character now carries fine-grained visual fields (age, hair, eyes, clothing)
so the image generator can build consistent prompts without freeform text.
"""

from __future__ import annotations

from typing import Literal, Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Character
# ---------------------------------------------------------------------------
class Character(BaseModel):
    """
    Persistent character description injected into every image prompt
    to keep visual consistency across all scenes.

    All fields used directly in Pollinations.ai image prompts.
    """

    name: str = Field(description="Full character name as it appears in the story")
    gender: str = Field(description="Gender of the character, e.g. 'male', 'female', 'non-binary'")
    age: str = Field(
        description="Age or age range, e.g. '30s', 'teenager', 'elderly woman'"
    )
    hair: str = Field(
        description="Hair colour, length, and style, e.g. 'long auburn wavy hair'"
    )
    eyes: str = Field(
        description="Eye colour and notable features, e.g. 'piercing green eyes'"
    )
    facial_features: str = Field(
        description="Distinct facial features, skin tone, shape of face, scars, or facial hair"
    )
    body_type: str = Field(
        description="Physical build, height, or posture, e.g. 'tall and athletic', 'slender'"
    )
    clothing: str = Field(
        description="Typical outfit / attire worn throughout the story"
    )
    role: Literal["protagonist", "antagonist", "supporting", "minor"] = Field(
        description="Narrative role in the story"
    )
    personality: str | None = Field(
        default=None,
        description="Two or three personality traits relevant to narration tone",
    )

    @field_validator("role", mode="before")
    @classmethod
    def normalise_role(cls, v: str) -> str:
        """Accept minor role variations like 'hero', 'villain', 'side character'."""
        mapping = {
            "hero": "protagonist",
            "main character": "protagonist",
            "lead": "protagonist",
            "villain": "antagonist",
            "side character": "supporting",
            "background": "minor",
        }
        return mapping.get(v.lower().strip(), v.lower().strip())

    @field_validator("personality", mode="before")
    @classmethod
    def normalise_personality(cls, v: Any) -> str | None:
        """Accept personality as a list of strings and join them, or single string."""
        if isinstance(v, list):
            return ", ".join(str(item).strip() for item in v if item)
        if v is None:
            return None
        return str(v).strip()

    def to_image_description(self) -> str:
        """
        Return a compact visual description string suitable for injection
        into an image generation prompt.
        """
        parts = [
            f"{self.age} {self.gender}",
            f"{self.body_type} build",
            self.hair,
            self.eyes,
            self.facial_features,
            f"wearing {self.clothing}",
        ]
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# CharacterMemory
# ---------------------------------------------------------------------------
class CharacterMemory(BaseModel):
    """Container holding all characters extracted from the story."""

    characters: list[Character] = Field(default_factory=list)

    def to_prompt_injection(self) -> str:
        """
        Returns a compact multi-line block describing every character.
        Appended to image prompts for visual consistency across scenes.

        Example output:
            Character reference (maintain visual consistency):
              • Elena (protagonist): woman in her 30s, long auburn wavy hair, …
              • Daren (antagonist): tall man in his 40s, close-cropped silver hair, …
        """
        if not self.characters:
            return ""
        lines = ["Character reference (maintain visual consistency):"]
        for c in self.characters:
            lines.append(f"  • {c.to_image_description()}")
        return "\n".join(lines)

    def get(self, name: str) -> Character | None:
        """Look up a character by name (case-insensitive)."""
        name_lower = name.lower()
        for c in self.characters:
            if c.name.lower() == name_lower:
                return c
        return None
