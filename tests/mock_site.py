"""A tiny local web site that imitates the sources the agent tracks (eCFR API,
Federal Register API, NRC RG index, DOE standards list, PDF documents), with a
``day`` knob that changes what is served, so update detection can be tested
offline."""
from __future__ import annotations

import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate

S = getSampleStyleSheet()


def make_scan(title: str, body: str) -> bytes:
    """An image-only PDF (no text layer): a text PDF rasterised page by page."""
    import pymupdf
    src = pymupdf.open(stream=make_pdf(title, body), filetype="pdf")
    out = pymupdf.open()
    for pg in src:
        page = out.new_page(width=pg.rect.width, height=pg.rect.height)
        page.insert_image(page.rect, pixmap=pg.get_pixmap(dpi=150))
    return out.tobytes()


def make_pdf(title: str, body: str) -> bytes:
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build([Paragraph(title, S["Title"]), Paragraph("1. Scope", S["Heading1"]),
                                  Paragraph(body, S["Normal"])])
    return buf.getvalue()


class Site:
    def __init__(self):
        self.day = 1
        self.hits: list[str] = []
        self.blocked_ua = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                u = urlparse(self.path)
                res = outer.route(u.path, parse_qs(u.query))
                if res is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body, ctype = res
                etag = '"%s"' % hashlib.md5(body).hexdigest()
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    # ---------------------------------------------------------------- routes
    def route(self, path: str, q: dict):
        d = self.day
        if path == "/docs/scanned.pdf":
            return make_scan("Scanned Standard", "Seismic design text that exists only as a picture."), "application/pdf"
        if path == "/docs/rg1233_rev0.pdf":
            return make_pdf("Regulatory Guide 1.233", "Rev 0 text. NRC endorses NEI 18-04. See 10 CFR 50.69."), "application/pdf"
        if path == "/docs/rg1233_rev1.pdf":
            return make_pdf("Regulatory Guide 1.233", "Rev 1 text. NRC endorses NEI 18-04 Rev. 1."), "application/pdf"
        if path == "/docs/doe1271_2025.pdf":
            return make_pdf("DOE-STD-1271-2025", "Authorization pathway 2025."), "application/pdf"
        if path == "/docs/doe1271_2026.pdf":
            return make_pdf("DOE-STD-1271-2026", "Authorization pathway 2026. Replaces 2025."), "application/pdf"
        if path == "/index/rg.html":
            rows = ("<tr><td>1.232</td><td>Rev. 0</td><td>04/2018</td><td><a href='/docs/rg1232.pdf'>pdf</a></td></tr>"
                    "<tr><td>1.233</td><td>Rev. 0</td><td>06/2020</td><td><a href='/docs/rg1233_rev0.pdf'>pdf</a></td></tr>")
            if d >= 3:
                rows = rows.replace("<tr><td>1.233</td><td>Rev. 0</td><td>06/2020</td><td><a href='/docs/rg1233_rev0.pdf'>pdf</a></td></tr>",
                                    "<tr><td>1.233</td><td>Rev. 1</td><td>03/2026</td><td><a href='/docs/rg1233_rev1.pdf'>pdf</a></td></tr>")
            return (f"<html><body><nav>menu</nav><p>Last updated: day {d}</p><table>{rows}</table></body></html>").encode(), "text/html"
        if path == "/store/asce43.html":
            ed = "ASCE/SEI 43-19" if d < 3 else "ASCE/SEI 43-24 (new)"
            return f"<html><body><h1>{ed} Seismic Design Criteria</h1><p>Buy now.</p></body></html>".encode(), "text/html"
        if path == "/docs/asce43_24.pdf":
            return make_pdf("ASCE/SEI 43-24", "Paywalled."), "application/pdf"
        if path == "/index/asce.html":
            items = "<ul><li><a href='/docs/asce43_19.pdf'>ASCE/SEI 43-2019</a> Seismic Design Criteria (2020 printing)</li></ul>"
            if d >= 3:
                items += "<ul><li><a href='/docs/asce43_24.pdf'>ASCE/SEI 43-2024</a> Seismic Design Criteria</li></ul>"
            return f"<html><body>{items}</body></html>".encode(), "text/html"
        if path == "/index/doe.html":
            items = "<ul><li><a href='/docs/doe1271_2025.pdf'>DOE-STD-1271-2025</a> Authorization Pathway</li></ul>"
            if d >= 3:
                items = "<ul><li><a href='/docs/doe1271_2026.pdf'>DOE-STD-1271-2026</a> Authorization Pathway</li></ul>"
            return f"<html><body>{items}</body></html>".encode(), "text/html"
        if path == "/api/versioner/v1/versions/title-10.json":
            vs = [{"date": "2024-05-01", "issue_date": "2024-05-01", "identifier": "50.69", "part": "50", "type": "section"},
                  {"date": "2024-05-01", "issue_date": "2024-05-01", "identifier": "50.2", "part": "50", "type": "section"}]
            if d >= 3:
                vs.append({"date": "2026-09-01", "issue_date": "2026-09-01", "identifier": "50.69", "part": "50", "type": "section"})
                vs.append({"date": "2026-09-01", "issue_date": "2026-09-01", "identifier": "50.34", "part": "50", "type": "section"})
            return json.dumps({"content_versions": vs}).encode(), "application/json"
        if path == "/api/versioner/v1/titles.json":
            return json.dumps({"titles": [{"number": 10, "up_to_date_as_of": "2024-05-01" if d < 3 else "2026-09-01"}]}).encode(), "application/json"
        if path.startswith("/api/versioner/v1/full/") and path.endswith("/title-10.xml"):
            extra = "" if d < 3 else "<P>(d) Licensees shall also comply with § 50.34 and § 50.2 as revised.</P>"
            xml = ("<?xml version='1.0'?><ECFR><DIV5 N='50' TYPE='PART'><HEAD>PART 50 DOMESTIC LICENSING</HEAD>"
                   "<DIV8 N='§ 50.69' TYPE='SECTION'><HEAD>§ 50.69 Risk-informed categorization and treatment of SSCs.</HEAD>"
                   "<P>(a) Definitions. See § 50.2 and §§ 50.34, 50.35 for terms. NRC reviews each application.</P>" + extra +
                   "</DIV8><DIV8 N='§ 50.2' TYPE='SECTION'><HEAD>§ 50.2 Definitions.</HEAD><P>Commission means the Nuclear Regulatory Commission.</P></DIV8>"
                   "</DIV5></ECFR>")
            return xml.encode(), "application/xml"
        if path.startswith("/current/title-10/"):
            return b"<html><body><div id='app'></div><script>/* SPA */</script></body></html>", "text/html"
        if path == "/api/v1/documents.json":
            docs = [{"document_number": "2024-0001", "title": "Old rule", "type": "Rule", "publication_date": "2024-01-02",
                     "html_url": self.base + "/fr/2024-0001", "cfr_references": [{"title": 10, "part": 50}]}]
            if d >= 3:
                docs.insert(0, {"document_number": "2026-0099", "title": "Part 50 risk-informed amendments", "type": "Proposed Rule",
                                "publication_date": "2026-09-02", "html_url": self.base + "/fr/2026-0099",
                                "cfr_references": [{"title": 10, "part": 50}, {"title": 10, "part": 53}]})
            return json.dumps({"results": docs}).encode(), "application/json"
        if path.startswith("/agencies/"):
            return b"<html><body>agency page</body></html>", "text/html"
        return None
