"""Figure export: image sniffing, conversion and the 'never write .bin' rule."""
import base64
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import kg_ingest as K  # noqa: E402

IM = Image.new("RGB", (200, 120), (200, 30, 30))


def enc(fmt, **kw):
    b = io.BytesIO()
    IM.save(b, fmt, **kw)
    return b.getvalue()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="blue"/></svg>'


@pytest.mark.parametrize("name,data,ext", [
    ("png", enc("PNG"), ".png"), ("jpeg", enc("JPEG"), ".jpg"), ("gif", enc("GIF"), ".gif"), ("tiff", enc("TIFF"), ".tif"),
    ("webp", enc("WEBP"), ".webp"), ("jp2", enc("JPEG2000"), ".jp2"), ("ico", enc("ICO"), ".ico"),
    ("svg", SVG, ".svg"), ("svg+comment", b"<!-- Generator: Illustrator -->" + SVG, ".svg"),
    ("svg+bom", b"\xef\xbb\xbf" + SVG, ".svg"), ("svg+ws+xml", b'\n  <?xml version="1.0"?>\n<!DOCTYPE svg><svg/>', ".svg"),
    ("avif-magic", b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00", ".avif"),
])
def test_image_ext_recognises_formats(name, data, ext):
    assert K._image_ext(data) == ext


@pytest.mark.parametrize("data", [b"<!DOCTYPE html><html><body><svg></svg>Not found</body></html>", b"<html>x</html>",
                                  b"\x1f\x8b\x08\x00abc", b"just some text", b""])
def test_non_images_are_rejected(data):
    assert K._image_ext(data) == ""


def _html_figures(tmp_path, payloads):
    figs = "".join(f'<figure><img src="data:application/octet-stream;base64,{base64.b64encode(p).decode()}" alt="img{i}">'
                   f"<figcaption>Fig {i}</figcaption></figure>" for i, p in enumerate(payloads))
    h = tmp_path / "doc.html"
    h.write_text(f"<html><body>{figs}</body></html>")
    els = K.load_html(str(h))
    root = K.export_assets(els, str(h), str(tmp_path / "out"), "png")
    return root, json.loads((root / "manifest.json").read_text())


def test_html_export_never_writes_bin_and_reports_skips(tmp_path):
    payloads = [b"<!-- x -->" + SVG, b"\xef\xbb\xbf" + SVG, enc("ICO"), enc("WEBP"), enc("JPEG2000"),
                b"<!DOCTYPE html><html>soft 404</html>", b"\x1f\x8b\x08\x00zz"]
    root, man = _html_figures(tmp_path, payloads)
    files = [a["file"] for a in man["assets"]]
    assert not any(f.endswith(".bin") for f in files), files
    assert not list(root.rglob("*.bin"))
    assert len(files) == 5 and files[0].endswith(".svg") and files[1].endswith(".svg")
    assert files[2].endswith(".png") and files[3].endswith(".png") and files[4].endswith(".png")   # converted
    assert [s["reason"] for s in man["skipped"]] == ["content is not an image"] * 2
    for f in files:
        assert (root / f).stat().st_size > 0


def test_pdf_native_mode_keeps_true_extension(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 60), "Figure 1: test image")
    pg.insert_image(pymupdf.Rect(72, 100, 372, 280), stream=enc("JPEG2000"))
    p = tmp_path / "jpx.pdf"
    doc.save(p)
    K.set_pdf_engine("basic")
    els = K.load_pdf(str(p))
    for mode, want in (("png", ".png"), ("native", ".jp2")):
        root = K.export_assets(els, f"{p}#{mode}", str(tmp_path / "out"), mode)
        files = [a["file"] for a in json.loads((root / "manifest.json").read_text())["assets"]]
        assert files and files[0].endswith(want), (mode, files)
