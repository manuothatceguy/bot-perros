from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError


PHOTO_KIND_LABELS = {
    "package": "Caja / Frasco",
    "dose": "Pastilla / Dosis",
    "other": "Referencia",
}


def get_photos_dir(db_path: str) -> Path:
    """Return directory where medication photos are stored, creating it if needed."""
    if db_path == ":memory:":
        path = Path(tempfile.gettempdir()) / "bot-perros-photos"
    else:
        path = Path(db_path).resolve().parent / "photos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def process_and_save_photo(
    raw_bytes: bytes, photos_dir: Path, max_dim: int = 1280
) -> tuple[str, str]:
    """Validate, EXIF-orient, resize, compress to JPEG, and persist a photo."""
    photos_dir.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(BytesIO(raw_bytes)) as opened:
            img = ImageOps.exif_transpose(opened)
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            filename = f"{uuid4().hex}.jpg"
            img.save(photos_dir / filename, format="JPEG", quality=82, optimize=True)
            return filename, "image/jpeg"
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("El archivo no es una imagen válida.") from exc


def _photo_label(row: dict[str, Any]) -> str:
    base = PHOTO_KIND_LABELS.get(str(row.get("kind") or "other"), "Referencia")
    caption = str(row.get("caption") or "").strip()
    return f"{base} ({caption})" if caption else base


def build_reference_image(
    photo_rows: list[dict[str, Any]], photos_dir: Path
) -> tuple[bytes, str, str] | None:
    """Return image bytes, filename, and label note (compositing 2+ photos side-by-side)."""
    existing: list[tuple[dict[str, Any], Path]] = []
    for row in photo_rows:
        p = photos_dir / str(row.get("filename") or "")
        if p.is_file():
            existing.append((row, p))

    if not existing:
        return None

    if len(existing) == 1:
        row, path = existing[0]
        return path.read_bytes(), row["filename"], _photo_label(row)

    (row1, path1), (row2, path2) = existing[0], existing[1]
    with Image.open(path1) as o1, Image.open(path2) as o2:
        im1 = ImageOps.exif_transpose(o1).convert("RGB")
        im2 = ImageOps.exif_transpose(o2).convert("RGB")

        target_h = min(720, max(im1.height, im2.height, 1))
        w1 = max(1, round(im1.width * target_h / max(1, im1.height)))
        w2 = max(1, round(im2.width * target_h / max(1, im2.height)))
        r1 = im1.resize((w1, target_h), Image.Resampling.LANCZOS)
        r2 = im2.resize((w2, target_h), Image.Resampling.LANCZOS)

        pad = 12
        canvas_w = w1 + w2 + (pad * 3)
        canvas_h = target_h + (pad * 2)
        canvas = Image.new("RGB", (canvas_w, canvas_h), (15, 23, 42))
        canvas.paste(r1, (pad, pad))
        canvas.paste(r2, (pad * 2 + w1, pad))

        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=84, optimize=True)
        note = f"Izq: {_photo_label(row1)} · Der: {_photo_label(row2)}"
        return buf.getvalue(), "referencia.jpg", note


def delete_photo_file(photos_dir: Path, filename: str) -> None:
    """Delete a stored photo file safely if it exists."""
    if not filename:
        return
    (photos_dir / filename).unlink(missing_ok=True)
