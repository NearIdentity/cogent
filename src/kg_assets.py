"""Image handling for assets served over MCP: normalise to PNG, downscale, and stamp paywalled content.

Figures taken from paywalled (copyrighted) standards are never served as the original bytes: they are re-rendered
as PNG with a tiled diagonal watermark and a copyright banner underneath. Needs Pillow (+ PyMuPDF for SVG)."""
from __future__ import annotations

import io
import textwrap
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

TILE_TEXT = "REFERENCE ONLY  © COPYRIGHTED"


def to_png(data: bytes, max_px: int = 1600) -> tuple[bytes, tuple[int, int]]:
    """Any raster image (or SVG) -> PNG, long edge <= max_px. Returns (png, (width, height))."""
    if data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:2000]:
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="svg")
        data = doc[0].get_pixmap(dpi=144).tobytes("png")
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGBA")
    if max(im.size) > max_px:
        k = max_px / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue(), im.size


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=max(8, size))


def watermark(png: bytes, banner: str, tile: str = TILE_TEXT) -> bytes:
    """Tile a diagonal semi-transparent mark over the image and append a banner with ``banner`` below it."""
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    fs = max(12, w // 24)
    font = _font(fs)
    # diagonal tiling on an oversized canvas, rotated, then cropped to the image
    diag = int((w * w + h * h) ** 0.5) + 2 * fs
    layer = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tw = int(d.textlength(tile, font=font)) + 2 * fs
    step_y = fs * 3
    for row, y in enumerate(range(0, diag, step_y)):
        for x in range(-tw if row % 2 else 0, diag, tw):
            d.text((x, y), tile, font=font, fill=(200, 0, 0, 90))
    layer = layer.rotate(30, resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    im = Image.alpha_composite(im, layer.crop((left, top, left + w, top + h)))
    # banner
    bf = _font(max(11, w // 45))
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    cols = max(20, int(w / max(1.0, probe.textlength("n", font=bf))) - 2)
    lines = textwrap.wrap(banner, cols) or [banner]
    lh = int(bf.size * 1.35)
    bh = lh * len(lines) + 12
    out = Image.new("RGBA", (w, h + bh), (255, 245, 200, 255))
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    d.line([(0, h), (w, h)], fill=(160, 0, 0, 255), width=2)
    for i, line in enumerate(lines):
        d.text((6, h + 6 + i * lh), line, font=bf, fill=(90, 0, 0, 255))
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def fit_bytes(png: bytes, limit: int = 2_000_000, floor_px: int = 400) -> bytes:
    """Shrink a PNG until it is at most ``limit`` bytes (best effort)."""
    while len(png) > limit:
        im = Image.open(io.BytesIO(png))
        if max(im.size) <= floor_px:
            break
        im = im.resize((int(im.width * 0.8), int(im.height * 0.8)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
    return png


def notice(holder: Optional[str] = None) -> str:
    who = f"the publisher of {holder}" if holder else "its publisher"
    return (f"© Copyrighted material of {who}, taken from a purchased copy. Reproduced for reference only; "
            "not for redistribution, publication or reliance in place of the original standard.")
