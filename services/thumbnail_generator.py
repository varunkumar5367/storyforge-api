"""
services/thumbnail_generator.py
─────────────────────────────────
Step 7 of the pipeline: create a YouTube thumbnail by overlaying bold text
on the best scene image using Pillow.

Strategy:
  • Pick the scene with the highest visual impact (scene 1 by default,
    or whichever scene has "thumbnail: true" in a future enhancement).
  • Darken the bottom-third of the image.
  • Draw the video title in large bold white text.
  • Add a subtle gradient overlay for contrast.

Output: {job_dir}/final/thumbnail.png (1280×720)

Returns:
  { "success": True,  "data": { "thumbnail_path": "..." } }
  or { "success": False, "error": "..." }
"""

from __future__ import annotations

import logging
from pathlib import Path

from models.scene import Scene
from utils.file_handler import final_thumbnail_path

logger = logging.getLogger("storyforge.thumbnail_generator")

# Thumbnail canvas dimensions
THUMB_W = 1280
THUMB_H = 720


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------
async def generate_thumbnail(
    job_id: str,
    scenes: list[dict],
    title: str = "",
    scene_number: int | None = None,
    custom_image_path: Path | None = None,
) -> dict:
    """
    Create a thumbnail image for the episode.

    Args:
        job_id:  Unique job identifier.
        scenes:  List of scene dicts (image_path must be populated).
        title:   Video title text to overlay.
        scene_number: Scene number to use as background (optional).
        custom_image_path: Custom image Path to use as background (optional).

    Returns:
        { "success": True, "data": { "thumbnail_path": "..." } }
        or { "success": False, "error": "..." }
    """
    logger.info("Generating thumbnail [job=%s] …", job_id)

    best_image = None
    if custom_image_path and custom_image_path.exists():
        best_image = custom_image_path
    elif scene_number is not None:
        scene = next((s for s in scenes if s.get("scene_number") == scene_number), None)
        if scene and scene.get("image_path") and Path(scene["image_path"]).exists():
            best_image = Path(scene["image_path"])

    if not best_image:
        best_image = _pick_best_image(scenes)
    if not best_image:
        return {"success": False, "error": "No scene images available for thumbnail."}

    out_path = final_thumbnail_path(job_id)

    try:
        _create_thumbnail(best_image, out_path, title)
    except Exception as exc:
        logger.exception("Thumbnail creation failed.")
        return {"success": False, "error": str(exc)}

    logger.info("Thumbnail saved → %s", out_path)
    return {"success": True, "data": {"thumbnail_path": str(out_path)}}


# ---------------------------------------------------------------------------
# Pillow rendering
# ---------------------------------------------------------------------------
def _create_thumbnail(image_path: Path, out_path: Path, title: str) -> None:
    """
    Render the thumbnail using Pillow.

    Layers (bottom → top):
      1. Base scene image (resized to 1280×720)
      2. Dark gradient overlay on bottom third
      3. Title text in bold white with drop shadow
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    # 1. Load and resize base image
    base = Image.open(image_path).convert("RGBA")
    base = base.resize((THUMB_W, THUMB_H), Image.LANCZOS)

    if title.strip():
        # 2. Gradient overlay — semi-transparent black on bottom 40%
        overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
        draw_overlay = ImageDraw.Draw(overlay)

        gradient_top = THUMB_H * 60 // 100  # starts at 60% from top
        for y in range(gradient_top, THUMB_H):
            alpha = int(200 * (y - gradient_top) / (THUMB_H - gradient_top))
            draw_overlay.rectangle(
                [(0, y), (THUMB_W, y + 1)],
                fill=(0, 0, 0, alpha),
            )

        base = Image.alpha_composite(base, overlay)

        # 3. Draw title text
        draw = ImageDraw.Draw(base)

        # Try to load a bold font; fall back to PIL default if not available
        font_size = 72
        font = _load_font(font_size)

        # Wrap title text to fit width
        lines = _wrap_text(title, font, draw, max_width=THUMB_W - 80)

        # Calculate text block height
        line_height = font_size + 10
        text_block_h = len(lines) * line_height
        y_start = THUMB_H - text_block_h - 60  # 60px from bottom

        for i, line in enumerate(lines):
            y = y_start + i * line_height
            # Drop shadow
            draw.text((42, y + 3), line, font=font, fill=(0, 0, 0, 180))
            # Main text
            draw.text((40, y), line, font=font, fill=(255, 255, 255, 255))

    # TODO: Add a small logo / watermark in the top-right corner.
    # TODO: Add a coloured "episode number" badge in the top-left corner.

    # Save as RGB PNG
    final = base.convert("RGB")
    final.save(str(out_path), "PNG", optimize=True)


def _load_font(size: int):
    """Load Arial Bold or fall back to Pillow's default font."""
    from PIL import ImageFont

    font_candidates = [
        "arialbd.ttf",     # Windows
        "Arial Bold.ttf",  # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
    ]
    for candidate in font_candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            continue

    # Last resort — Pillow built-in (very small, no size parameter)
    logger.warning("No TTF font found; using Pillow default font.")
    return ImageFont.load_default()


def _wrap_text(text: str, font, draw, max_width: int) -> list[str]:
    """Break text into lines that fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()
        # getlength / textlength compat
        try:
            w = draw.textlength(test, font=font)
        except AttributeError:
            w = font.getlength(test)

        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines or [text]  # fallback: single unwrapped line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pick_best_image(scenes: list[dict]) -> Path | None:
    """
    Pick the image to use as the thumbnail base.

    Current strategy: use the middle scene for visual variety.
    TODO: Score scenes by "visual impact" keywords in the image_prompt.
    """
    valid = [
        Scene(**s)
        for s in scenes
        if (s.get("image_path") and Path(s["image_path"]).exists())
    ]
    if not valid:
        return None

    mid = len(valid) // 2
    return Path(valid[mid].image_path)
