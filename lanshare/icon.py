"""The LanShare icon, a lightning bolt on an indigo tile (the same shape as static/favicon.svg)."""

from __future__ import annotations

from pathlib import Path

TILE = (79, 70, 229, 255)
BOLT = (251, 191, 36, 255)
# favicon.svg's bolt on its 24 x 24 grid.
_BOLT = ((13, 3), (5, 14), (11, 14), (10, 21), (18, 10), (12, 10))
_SUPERSAMPLE = 4


def draw_icon(size: int = 64, margin: float = 0.0):
    """The icon as a Pillow RGBA image. `margin` (a fraction of the size) leaves space around the tile."""
    from PIL import Image, ImageDraw

    big = size * _SUPERSAMPLE
    pad = round(big * margin)
    unit = (big - 2 * pad) / 24
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((pad, pad, big - pad - 1, big - pad - 1), radius=round(6 * unit), fill=TILE)
    draw.polygon([(pad + x * unit, pad + y * unit) for x, y in _BOLT], fill=BOLT)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_icon_files(directory: Path) -> dict[str, Path]:
    """Write LanShare.ico (Windows), LanShare.icns (macOS) and LanShare.png for packaging."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = {ext: directory / f"LanShare.{ext}" for ext in ("ico", "icns", "png")}
    draw_icon(256).save(paths["ico"], sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    # macOS icons leave a margin around the tile, like Apple's own.
    mac = draw_icon(1024, margin=0.09)
    mac.save(paths["icns"])
    mac.save(paths["png"])
    return paths
