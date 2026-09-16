from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class PreparedImage:
    filename: str
    data: bytes
    colour: int
    display_name: str = "Изображение"


def safe_filename(name: str, index: int) -> str:
    stem = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "_", PurePosixPath(name).stem).strip("_")[:60]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        suffix = ".jpg"
    return f"{index}_{stem or 'image'}{suffix}"


def image_display_name(name: str) -> str:
    stem = PurePosixPath(name).stem.replace("_", " ")
    normalized = re.sub(r"\s+", " ", stem).strip()
    return (normalized or "Изображение")[:200]


def _dominant_colour(image: Image.Image) -> int:
    sample = image.convert("RGB")
    sample.thumbnail((64, 64))
    quantized = sample.quantize(colors=6)
    palette = quantized.getpalette() or []
    colors = quantized.getcolors() or []
    if not colors or not palette:
        return 0x5865F2
    candidates: list[tuple[float, tuple[int, int, int]]] = []
    for count, index in colors:
        rgb = tuple(palette[index * 3 : index * 3 + 3])
        if len(rgb) != 3:
            continue
        spread = max(rgb) - min(rgb)
        brightness = sum(rgb) / 3
        weight = count * (1 + spread / 255) * (0.4 if brightness > 245 or brightness < 12 else 1)
        candidates.append((weight, rgb))
    if not candidates:
        return 0x5865F2
    _, (red, green, blue) = max(candidates, key=lambda item: item[0])
    return (red << 16) | (green << 8) | blue


def prepare_image(name: str, data: bytes, max_bytes: int, index: int) -> PreparedImage:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.seek(0)
            image = ImageOps.exif_transpose(opened).copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"{name}: файл не удалось распознать как изображение") from exc

    colour = _dominant_colour(image)
    filename = safe_filename(name, index)
    display_name = image_display_name(name)
    if len(data) <= max_bytes and PurePosixPath(filename).suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return PreparedImage(filename, data, colour, display_name)

    if image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")

    quality = 90
    while True:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        compressed = output.getvalue()
        if len(compressed) <= max_bytes:
            return PreparedImage(f"{PurePosixPath(filename).stem}.jpg", compressed, colour, display_name)
        width, height = image.size
        if width <= 640 or height <= 640:
            raise ValueError(f"{name}: изображение не помещается в лимит Discord")
        image.thumbnail((int(width * 0.82), int(height * 0.82)), Image.Resampling.LANCZOS)
        quality = max(70, quality - 4)
