"""Local file storage under STORAGE_DIR (mount a Railway volume there so files survive redeploys)."""
import io
import re
from pathlib import Path
from PIL import Image, ImageOps

from ..config import STORAGE_DIR

OUT_W, OUT_H = 1200, 1500          # SkyBlue website listing size (4:5)


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"


def save(rel: str, data: bytes) -> str:
    p = STORAGE_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    from . import mirror
    mirror.queue_upload(rel)            # safe copy in Drive, restored after a redeploy
    return rel


def read(rel: str) -> bytes:
    p = STORAGE_DIR / rel
    if not p.exists():
        from . import mirror
        mirror.restore(rel)
    return p.read_bytes()


def path(rel: str) -> Path:
    p = (STORAGE_DIR / rel).resolve()
    if STORAGE_DIR.resolve() not in p.parents:
        raise ValueError("bad path")
    if not p.exists():
        from . import mirror
        mirror.restore(rel)
    return p


def to_output(data: bytes) -> bytes:
    """Fit any generated image to exactly 4:5, 1200x1500, sRGB JPEG."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im = ImageOps.fit(im, (OUT_W, OUT_H), Image.LANCZOS, centering=(0.5, 0.5))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=84, optimize=True, progressive=True)   # compressed for fast website loading
    return out.getvalue()


def normalise_raw(data: bytes) -> bytes:
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    im = im.convert("RGB")
    im.thumbnail((3000, 3000))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def image_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size
