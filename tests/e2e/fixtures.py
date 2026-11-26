"""Deterministic synthetic fixture generators for tests/e2e/ (zero top-level M1-M5 imports)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
from typing import Any
import zlib

from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def _build_minimal_pdf(
    objects: list[bytes],
    root_obj_num: int = 1,
    info_obj_num: int | None = None,
    corrupt_xref: bool = False,
) -> bytes:
    """Assemble a spec-compliant PDF byte stream with accurate xref offsets."""
    chunks: list[bytes] = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets: list[int] = [0]
    pos = len(chunks[0])
    for idx, body in enumerate(objects, start=1):
        offsets.append(pos)
        obj_bytes = f"{idx} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        chunks.append(obj_bytes)
        pos += len(obj_bytes)

    xref_offset = pos
    xref_lines = [f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")]
    for off in offsets[1:]:
        stored = 999999999 if corrupt_xref else off
        xref_lines.append(f"{stored:010d} 00000 n \n".encode("ascii"))
    chunks.extend(xref_lines)

    info_entry = f" /Info {info_obj_num} 0 R" if info_obj_num is not None else ""
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root {root_obj_num} 0 R{info_entry} >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    chunks.append(trailer)
    return b"".join(chunks)


def _append_incremental_catalog_patch(
    pdf_bytes: bytes,
    extra_catalog_entries: dict[str, str],
    extra_objects: dict[int, bytes],
) -> bytes:
    """Append a valid PDF incremental update patching the Root Catalog without external libs."""
    prev_match = list(re.finditer(rb"startxref\s+(\d+)\s+%%EOF", pdf_bytes))
    if not prev_match:
        return pdf_bytes
    prev_xref = int(prev_match[-1].group(1))

    root_match = list(re.finditer(rb"/Root\s+(\d+)\s+0\s+R", pdf_bytes))
    size_match = list(re.finditer(rb"/Size\s+(\d+)", pdf_bytes))
    if not root_match or not size_match:
        return pdf_bytes
    root_num = int(root_match[-1].group(1))
    old_size = int(size_match[-1].group(1))

    # Extract existing root dictionary body
    obj_pat = re.compile(rf"(?m)(?:^|\n){root_num}\s+0\s+obj\s*<<(.*?)>>\s*endobj".encode("ascii"), re.DOTALL)
    root_obj_match = obj_pat.search(pdf_bytes)
    if not root_obj_match:
        return pdf_bytes
    existing_root_inner = root_obj_match.group(1).decode("latin-1")
    patched_fields = " ".join(f"/{k} {v}" for k, v in extra_catalog_entries.items())
    new_root_body = f"<< {existing_root_inner} {patched_fields} >>".encode("latin-1")

    all_updates: dict[int, bytes] = {root_num: new_root_body}
    all_updates.update(extra_objects)
    max_num = max(old_size - 1, max(all_updates.keys()))

    pos = len(pdf_bytes)
    out: list[bytes] = [pdf_bytes]
    if not pdf_bytes.endswith(b"\n"):
        out.append(b"\n")
        pos += 1

    new_offsets: dict[int, int] = {}
    for obj_num in sorted(all_updates.keys()):
        new_offsets[obj_num] = pos
        chunk = f"{obj_num} 0 obj\n".encode("ascii") + all_updates[obj_num] + b"\nendobj\n"
        out.append(chunk)
        pos += len(chunk)

    xref_pos = pos
    out.append(b"xref\n")
    for obj_num in sorted(new_offsets.keys()):
        out.append(f"{obj_num} 1\n{new_offsets[obj_num]:010d} 00000 n \n".encode("ascii"))

    info_match = list(re.finditer(rb"/Info\s+(\d+\s+\d+\s+R)", pdf_bytes))
    info_entry = f" /Info {info_match[-1].group(1).decode('ascii')}" if info_match else ""
    trailer = (
        f"trailer\n<< /Size {max_num + 1} /Root {root_num} 0 R{info_entry} /Prev {prev_xref} >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    out.append(trailer)
    return b"".join(out)


def make_multipage_toc_pdf(
    path: Path,
    *,
    pages: int = 4,
    title: str = "PDF/MD Lector Specimen",
) -> Path:
    """Generate a multi-page PDF with multi-level TOC bookmarks, ES/EN accented words, and invoice text."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle(title)
    c.setAuthor("TRODAI QA")
    w, h = LETTER

    page_specs = [
        (
            "1. Lectura Nitida y Privacidad Local",
            0,
            [
                "PDF/MD Lector by TRODAI protege la privacidad local.",
                "Palabras clave: Privacidad, Lectura, Pagina, Electronica, Contrato.",
                "Accent check: P\u00e1gina Electr\u00f3nica criptogr\u00e1fica.",
            ],
        ),
        (
            "2. Arquitectura Modular y Rendimiento",
            0,
            [
                "Renderizado asincrono con pypdfium2 y cache LRU acotada.",
                "Soporte dual para documentos PDF y Markdown local seguro.",
            ],
        ),
        (
            "3. Factura Electronica DIAN UBL 2.1",
            1,
            [
                "EMISOR: TRODAI ANALITICA S.A.S. NIT 900123456",
                "Concepto: Licencia Open Source Factura Electronica",
                "Subtotal: 2400000  |  IVA 19%: 456000  |  CUFE: abcdef123456",
            ],
        ),
        (
            "4. Anotaciones ISO 32000 y Diccionario",
            0,
            [
                "Resaltado multicolor, notas adhesivas y tinta tactil /Ink.",
                "Diccionario offline estilo Kindle con busqueda insensible a acentos.",
            ],
        ),
    ]

    total_pages = max(1, int(pages))
    for idx in range(total_pages):
        heading, level, lines = page_specs[idx % len(page_specs)]
        if idx >= len(page_specs):
            heading = f"{idx + 1}. Seccion Adicional"
            level = 0
        c.setFillColor(colors.HexColor("#0A0F1D"))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(54, h - 72, heading if idx < len(page_specs) else f"{heading} ({idx + 1})")
        c.setFillColor(colors.HexColor("#334155"))
        c.setFont("Helvetica", 11)
        y = h - 110
        for line in lines:
            c.drawString(54, y, line)
            y -= 20
        c.setStrokeColor(colors.HexColor("#0EA5E9"))
        c.rect(54, 100, 180, 60, stroke=1, fill=0)
        c.setFont("Helvetica", 9)
        c.drawRightString(w - 54, 42, f"TRODAI Page {idx + 1} / {total_pages}")
        bm_key = f"toc_p{idx + 1}"
        c.bookmarkPage(bm_key)
        c.addOutlineEntry(heading, bm_key, level=level, closed=False)
        c.showPage()

    c.save()
    return path


def make_blank_and_text_pdf(path: Path) -> Path:
    """Generate a 3-page PDF where page index 1 (page 2) is completely blank."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER

    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, h - 80, "Page 1: Non-blank introductory text.")
    c.showPage()

    # Page 2 intentionally blank (zero text, zero shapes).
    c.showPage()

    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, h - 80, "Page 3: Final text after blank page.")
    c.showPage()
    c.save()
    return path


def make_blank_only_pdf(path: Path) -> Path:
    """Generate a 1-page completely blank PDF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.showPage()
    c.save()
    return path


def make_acroform_pdf(path: Path) -> Path:
    """Generate a PDF with interactive AcroForm fields (TextField, CheckBox, Choice)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER
    c.setFont("Helvetica-Bold", 14)
    c.drawString(54, h - 60, "Formulario AcroForm TRODAI")
    form = c.acroForm
    c.setFont("Helvetica", 11)
    c.drawString(54, h - 105, "Nombre completo:")
    form.textfield(
        name="full_name",
        tooltip="Full Name",
        x=170,
        y=h - 115,
        width=220,
        height=22,
        value="Ana",
        borderStyle="solid",
        forceBorder=True,
    )
    c.drawString(54, h - 150, "Acepto terminos:")
    form.checkbox(
        name="agree",
        tooltip="Agree",
        x=170,
        y=h - 155,
        size=18,
        checked=True,
        buttonStyle="check",
        forceBorder=True,
    )
    c.drawString(54, h - 195, "Perfil:")
    form.choice(
        name="role",
        tooltip="Role",
        x=170,
        y=h - 205,
        width=160,
        height=22,
        options=["Usuario", "Auditor", "Admin"],
        value="Auditor",
    )
    c.showPage()
    c.save()
    return path


def make_image_pdf(path: Path, size: tuple[int, int] = (128, 128)) -> Path:
    """Generate a 1-page PDF containing an embedded raster RGB image."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, "#0A0F1D")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, size[0] - 10, size[1] - 10], fill="#0EA5E9", outline="#F8FAFC", width=4)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.drawString(54, LETTER[1] - 54, "Embedded Image Specimen")
    c.drawImage(ImageReader(img), 54, LETTER[1] - 240, width=160, height=160)
    c.showPage()
    c.save()
    return path


def make_pdf_with_image_and_attachment(
    path: Path,
    attachment_name: str = "attachment.xml",
    attachment_bytes: bytes = b"<?xml version='1.0' encoding='UTF-8'?><trodai><note>Embedded OK</note></trodai>",
) -> Path:
    """Generate a valid PDF containing both a 128x128 RGB image XObject and an /EmbeddedFiles attachment."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create raw 128x128 RGB image stream (zlib compressed)
    img = Image.new("RGB", (128, 128), "#1E293B")
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, 112, 112], fill="#0EA5E9", outline="#F8FAFC", width=4)
    raw_rgb = img.tobytes("raw", "RGB")
    compressed_rgb = zlib.compress(raw_rgb)
    compressed_att = zlib.compress(attachment_bytes)

    content_stream = b"q\n128 0 0 128 72 540 cm\n/Im0 Do\nQ\nBT /F1 12 Tf 72 700 Td (Image and Attachment PDF) Tj ET\n"

    # Object numbering:
    # 1: Catalog
    # 2: Pages
    # 3: Page
    # 4: Image XObject (/Im0)
    # 5: Content stream
    # 6: Font (/F1)
    # 7: Filespec
    # 8: EmbeddedFile stream
    # 9: Names dictionary
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Names 9 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 6 0 R >> /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        (
            f"<< /Type /XObject /Subtype /Image /Width 128 /Height 128 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed_rgb)} >>\nstream\n".encode("ascii")
            + compressed_rgb
            + b"\nendstream"
        ),
        (
            f"<< /Length {len(content_stream)} >>\nstream\n".encode("ascii")
            + content_stream
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Type /Filespec /F ({attachment_name}) /UF ({attachment_name}) /EF << /F 8 0 R >> >>".encode("ascii"),
        (
            f"<< /Type /EmbeddedFile /Subtype /application#2Fxml /Params << /Size {len(attachment_bytes)} >> /Filter /FlateDecode /Length {len(compressed_att)} >>\nstream\n".encode("ascii")
            + compressed_att
            + b"\nendstream"
        ),
        f"<< /EmbeddedFiles << /Names [ ({attachment_name}) 7 0 R ] >> >>".encode("ascii"),
    ]
    path.write_bytes(_build_minimal_pdf(objects, root_obj_num=1))
    return path


def make_watermark_overlay_pdf(path: Path, label: str = "CONFIDENCIAL TRODAI") -> Path:
    """Generate a single-page transparent vector watermark/overlay PDF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER
    c.saveState()
    c.translate(w / 2.0, h / 2.0)
    c.rotate(35)
    c.setFillColorRGB(0.85, 0.15, 0.15, alpha=0.25)
    c.setFont("Helvetica-Bold", 38)
    c.drawCentredString(0, 0, label)
    c.restoreState()
    c.showPage()
    c.save()
    return path


def make_redaction_target_pdf(path: Path, secret: str = "SECRET_TOKEN_XYZ") -> Path:
    """Generate a 2-page PDF with secret text, vector rect, image XObject, and incremental update."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER

    # Page 1: target text + overlapping vector box + small image
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, h - 100, "Confidential Contract Document")
    c.setFont("Helvetica", 12)
    c.drawString(72, h - 140, f"Primary Secret Token: {secret}")
    c.drawString(72, h - 165, "Public Clause: This paragraph must remain readable.")
    c.setStrokeColor(colors.HexColor("#0EA5E9"))
    c.rect(68, h - 152, 260, 26, stroke=1, fill=0)

    small_img = Image.new("RGB", (48, 48), "#DC2626")
    c.drawImage(ImageReader(small_img), 72, h - 250, width=48, height=48)
    c.showPage()

    # Page 2: secondary mention
    c.setFont("Helvetica", 12)
    c.drawString(72, h - 100, f"Appendix Reference: {secret}-COPY")
    c.showPage()
    c.save()

    # Append a clean PDF incremental update revision containing a Form XObject with the secret,
    # attached to Page 0 /Resources /XObject and invoked via /Do in Page 0's /Contents stream.
    raw = path.read_bytes()
    size_match = list(re.finditer(rb"/Size\s+(\d+)", raw))
    next_obj = int(size_match[-1].group(1)) if size_match else 20

    kids_match = re.search(rb"/Kids\s*\[\s*(\d+)\s+0\s+R", raw)
    page0_num = int(kids_match.group(1)) if kids_match else 5
    page0_pat = re.compile(
        rf"(?m)(?:^|\n){page0_num}\s+0\s+obj\s*<<(.*?)>>\s*endobj".encode("ascii"),
        re.DOTALL,
    )
    page0_match = page0_pat.search(raw)
    extra_objects: dict[int, bytes] = {}
    font_res = "/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"
    if page0_match:
        page0_body = page0_match.group(1).decode("latin-1")
        font_match = re.search(r"/Font\s+(\d+\s+0\s+R)", page0_body)
        if font_match:
            font_res = f"/Font {font_match.group(1)}"
        contents_match = re.search(r"/Contents\s+(\d+\s+0\s+R)", page0_body)
        xobj_match = re.search(r"/XObject\s*<<(.*?)>>", page0_body, re.DOTALL)
        if contents_match and xobj_match:
            do_stream = b"q 1 0 0 1 72 660 cm /TRODAIXObj Do Q\n"
            do_obj_num = next_obj + 2
            extra_objects[do_obj_num] = (
                f"<< /Length {len(do_stream)} >>\nstream\n".encode("ascii") + do_stream + b"\nendstream"
            )
            new_page0_body = page0_body.replace(
                contents_match.group(0),
                f"/Contents [ {contents_match.group(1)} {do_obj_num} 0 R ]",
                1,
            ).replace(
                xobj_match.group(0),
                f"/XObject << {xobj_match.group(1).strip()} /TRODAIXObj {next_obj} 0 R >>",
                1,
            )
            extra_objects[page0_num] = f"<< {new_page0_body} >>".encode("latin-1")

    form_stream = f"BT /F1 10 Tf 10 10 Td ({secret}_XOBJ) Tj ET\n".encode("ascii")
    xobj = (
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 200 40] /Resources << {font_res} >> /Length {len(form_stream)} >>\nstream\n".encode("ascii")
        + form_stream
        + b"\nendstream"
    )
    piece_info = b"<< /TRODAIRevision << /SecretNote (incremental_marker) >> >>"
    extra_objects[next_obj] = xobj
    extra_objects[next_obj + 1] = piece_info
    patched = _append_incremental_catalog_patch(
        raw,
        {"PieceInfo": f"{next_obj + 1} 0 R"},
        extra_objects,
    )
    path.write_bytes(patched)
    return path


def make_metadata_dirty_pdf(path: Path) -> Path:
    """Generate a PDF containing /Info metadata, Catalog XMP /Metadata stream, and /PieceInfo."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setTitle("Confidential Draft Title")
    c.setAuthor("Internal Author Name")
    c.setSubject("Private M&A Subject")
    c.setKeywords("secret, internal, audit")
    c.setCreator("ReportLab Secret Creator")
    c.setFont("Helvetica", 12)
    c.drawString(72, LETTER[1] - 90, "Body text with rich metadata attached.")
    c.showPage()
    c.save()

    raw = path.read_bytes()
    size_match = list(re.finditer(rb"/Size\s+(\d+)", raw))
    next_obj = int(size_match[-1].group(1)) if size_match else 20

    xmp_bytes = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:creator><rdf:Seq><rdf:li>Hidden XMP Creator</rdf:li></rdf:Seq></dc:creator>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )
    xmp_obj = (
        f"<< /Type /Metadata /Subtype /XML /Length {len(xmp_bytes)} >>\nstream\n".encode("ascii")
        + xmp_bytes
        + b"\nendstream"
    )
    piece_obj = b"<< /PrivateApp << /User (hidden_pieceinfo_user) >> >>"
    patched = _append_incremental_catalog_patch(
        raw,
        {"Metadata": f"{next_obj} 0 R", "PieceInfo": f"{next_obj + 1} 0 R"},
        {next_obj: xmp_obj, next_obj + 1: piece_obj},
    )
    path.write_bytes(patched)
    return path


def make_scanned_text_pdf(
    path: Path,
    text: str = "FACTURA ELECTRONICA NIT 900123456\nSUBTOTAL 100000 IVA 19000 TOTAL 119000",
) -> Path:
    """Generate an image-only scanned PDF (zero text operators) for OCR and e-invoice extraction."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1200, 1600), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    y = 140
    for line in text.splitlines():
        draw.text((100, y), line, fill="#000000")
        y += 60
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.drawImage(ImageReader(img), 0, 0, width=LETTER[0], height=LETTER[1])
    c.showPage()
    c.save()
    return path


def make_corrupt_pdf(path: Path) -> Path:
    """Generate an unrecoverable corrupt/truncated PDF file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n%CORRUPT_BINARY_GARBAGE\x00\xff\xfe\xfd\n1 0 obj\n<< /Broken ")
    return path


def make_repairable_bad_xref_pdf(path: Path) -> Path:
    """Generate a PDF with intact objects/content but deliberately broken xref offsets."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"BT /F1 12 Tf 72 720 Td (Repairable PDF Content) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    path.write_bytes(_build_minimal_pdf(objects, root_obj_num=1, corrupt_xref=True))
    return path


def make_invoice_pdf(path: Path, profile: str = "dian") -> Path:
    """Generate a visual DIAN or Factur-X invoice PDF (with lazy qrcode if installed, vector fallback otherwise)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=LETTER)
    w, h = LETTER
    c.setFillColor(colors.HexColor("#0A0F1D"))
    c.setFont("Helvetica-Bold", 16)
    if profile == "dian":
        c.drawString(54, h - 64, "FACTURA ELECTRONICA DE VENTA - DIAN UBL 2.1")
        c.setFont("Helvetica", 11)
        c.drawString(54, h - 96, "Emisor: TRODAI ANALITICA S.A.S.  |  NIT: 900123456-1")
        c.drawString(54, h - 116, "Adquirente: CLIENTE PRUEBA S.A.S.  |  NIT: 800654321-0")
        c.drawString(54, h - 146, "Concepto: Servicio Licencia PDF/MD Lector (Cant: 1)")
        c.drawString(54, h - 166, "Subtotal: 100000.00 COP  |  IVA (19%): 19000.00 COP  |  Total: 119000.00 COP")
        qr_data = "NumFac=SETP990000001 FecFac=2026-11-24 NitFac=900123456 ValTolFac=119000.00"
    else:
        c.drawString(54, h - 64, "INVOICE - FACTUR-X EN 16931")
        c.setFont("Helvetica", 11)
        c.drawString(54, h - 96, "Seller: TRODAI Europe GmbH  |  VAT: DE123456789")
        c.drawString(54, h - 116, "Buyer: Example Client SAS  |  VAT: FR98765432100")
        c.drawString(54, h - 146, "Item: Enterprise Software License (Qty: 1)")
        c.drawString(54, h - 166, "Net: 100.00 EUR  |  VAT (19%): 19.00 EUR  |  Total: 119.00 EUR")
        qr_data = "FACTUR-X-EN16931-INV-2026-001"

    drew_qr = False
    try:
        import qrcode  # lazy optional M1 dependency

        qr_img = qrcode.make(qr_data).convert("RGB")
        c.drawImage(ImageReader(qr_img), w - 154, h - 170, width=100, height=100)
        drew_qr = True
    except ImportError:
        pass

    if not drew_qr:
        # Deterministic vector finder-pattern fallback
        x0, y0, box = w - 154, h - 170, 96
        c.setStrokeColor(colors.black)
        c.rect(x0, y0, box, box, stroke=1, fill=0)
        c.setFillColor(colors.black)
        c.rect(x0 + 6, y0 + box - 24, 18, 18, stroke=0, fill=1)
        c.rect(x0 + box - 24, y0 + box - 24, 18, 18, stroke=0, fill=1)
        c.rect(x0 + 6, y0 + 6, 18, 18, stroke=0, fill=1)

    c.showPage()
    c.save()
    return path


def _compute_sample_dian_cufe() -> str:
    raw = "SETP9900000012026-11-2412:00:00-05:00100000.000119000.00040.00030.00119000.00900123456800654321fc8eac422eba16e22ffd8c6f94b3f40a6e38162c2"
    return hashlib.sha384(raw.encode("utf-8")).hexdigest()


def make_invoice_xml(
    path: Path,
    *,
    profile: str = "dian",
    valid: bool = True,
    xxe: bool = False,
) -> Path:
    """Generate deterministic DIAN UBL 2.1 or Factur-X EN 16931 CII XML (valid, invalid, or XXE)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doctype = '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]>\n' if xxe else ""

    if profile == "dian":
        cufe = _compute_sample_dian_cufe() if valid else "00000000BADCUFE"
        nit_block = "<cbc:CompanyID>900123456</cbc:CompanyID>" if valid else ""
        payable = "119000.00" if valid else "999999.00"
        xxe_note = "  <cbc:Note>&xxe;</cbc:Note>\n" if xxe else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"{doctype}"
            '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
            'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" '
            'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">\n'
            "  <cbc:UBLVersionID>UBL 2.1</cbc:UBLVersionID>\n"
            "  <cbc:ProfileID>DIAN 2.1: Factura Electronica de Venta</cbc:ProfileID>\n"
            "  <cbc:ID>SETP990000001</cbc:ID>\n"
            f"  <cbc:UUID schemeName=\"CUFE-SHA384\">{cufe}</cbc:UUID>\n"
            "  <cbc:IssueDate>2026-11-24</cbc:IssueDate>\n"
            "  <cbc:DocumentCurrencyCode>COP</cbc:DocumentCurrencyCode>\n"
            f"{xxe_note}"
            "  <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme>\n"
            "    <cbc:RegistrationName>TRODAI ANALITICA S.A.S.</cbc:RegistrationName>\n"
            f"    {nit_block}\n"
            "  </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>\n"
            "  <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme>\n"
            "    <cbc:RegistrationName>CLIENTE PRUEBA S.A.S.</cbc:RegistrationName>\n"
            "    <cbc:CompanyID>800654321</cbc:CompanyID>\n"
            "  </cac:PartyTaxScheme></cac:Party></cac:AccountingCustomerParty>\n"
            "  <cac:TaxTotal>\n"
            '    <cbc:TaxAmount currencyID="COP">19000.00</cbc:TaxAmount>\n'
            "    <cac:TaxSubtotal>\n"
            '      <cbc:TaxableAmount currencyID="COP">100000.00</cbc:TaxableAmount>\n'
            '      <cbc:TaxAmount currencyID="COP">19000.00</cbc:TaxAmount>\n'
            "      <cac:TaxCategory><cbc:Percent>19.00</cbc:Percent><cac:TaxScheme><cbc:ID>01</cbc:ID><cbc:Name>IVA</cbc:Name></cac:TaxScheme></cac:TaxCategory>\n"
            "    </cac:TaxSubtotal>\n"
            "  </cac:TaxTotal>\n"
            "  <cac:LegalMonetaryTotal>\n"
            '    <cbc:LineExtensionAmount currencyID="COP">100000.00</cbc:LineExtensionAmount>\n'
            '    <cbc:TaxExclusiveAmount currencyID="COP">100000.00</cbc:TaxExclusiveAmount>\n'
            '    <cbc:TaxInclusiveAmount currencyID="COP">119000.00</cbc:TaxInclusiveAmount>\n'
            f'    <cbc:PayableAmount currencyID="COP">{payable}</cbc:PayableAmount>\n'
            "  </cac:LegalMonetaryTotal>\n"
            "</Invoice>\n"
        )
    else:
        grand_total = "119.00" if valid else "888.00"
        xxe_note = "<ram:IncludedNote><ram:Content>&xxe;</ram:Content></ram:IncludedNote>" if xxe else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f"{doctype}"
            '<rsm:CrossIndustryInvoice '
            'xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" '
            'xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" '
            'xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">\n'
            "  <rsm:ExchangedDocumentContext>\n"
            "    <ram:GuidelineSpecifiedDocumentContextParameter>\n"
            "      <ram:ID>urn:cen.eu:en16931:2017</ram:ID>\n"
            "    </ram:GuidelineSpecifiedDocumentContextParameter>\n"
            "  </rsm:ExchangedDocumentContext>\n"
            f"  <rsm:ExchangedDocument><ram:ID>FX-2026-001</ram:ID><ram:TypeCode>380</ram:TypeCode>{xxe_note}</rsm:ExchangedDocument>\n"
            "  <rsm:SupplyChainTradeTransaction>\n"
            "    <ram:ApplicableHeaderTradeAgreement>\n"
            "      <ram:SellerTradeParty><ram:Name>TRODAI Europe GmbH</ram:Name></ram:SellerTradeParty>\n"
            "      <ram:BuyerTradeParty><ram:Name>Example Client SAS</ram:Name></ram:BuyerTradeParty>\n"
            "    </ram:ApplicableHeaderTradeAgreement>\n"
            "    <ram:ApplicableHeaderTradeSettlement>\n"
            "      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>\n"
            "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>\n"
            "        <ram:LineTotalAmount>100.00</ram:LineTotalAmount>\n"
            "        <ram:TaxBasisTotalAmount>100.00</ram:TaxBasisTotalAmount>\n"
            '        <ram:TaxTotalAmount currencyID="EUR">19.00</ram:TaxTotalAmount>\n'
            f"        <ram:GrandTotalAmount>{grand_total}</ram:GrandTotalAmount>\n"
            f"        <ram:DuePayableAmount>{grand_total}</ram:DuePayableAmount>\n"
            "      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>\n"
            "    </ram:ApplicableHeaderTradeSettlement>\n"
            "  </rsm:SupplyChainTradeTransaction>\n"
            "</rsm:CrossIndustryInvoice>\n"
        )
    path.write_text(xml, encoding="utf-8")
    return path


def make_facturx_pdfa3(path: Path, *, valid: bool = True) -> Path:
    """Generate a synthetic PDF/A-3 document with embedded factur-x.xml (or missing/broken for valid=False)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_path = path.with_suffix(".tmp_fx.xml")
    make_invoice_xml(xml_path, profile="facturx", valid=valid)
    xml_bytes = xml_path.read_bytes()
    xml_path.unlink(missing_ok=True)

    pdfa_xmp = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">'
        b"<pdfaid:part>3</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )
    content = b"BT /F1 12 Tf 72 720 Td (Factur-X EN 16931 Invoice FX-2026-001 Total 119.00 EUR) Tj ET\n"

    if valid:
        compressed_xml = zlib.compress(xml_bytes)
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R /Metadata 6 0 R /Names 9 0 R /AF [7 0 R] >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Type /Metadata /Subtype /XML /Length {len(pdfa_xmp)} >>\nstream\n".encode("ascii") + pdfa_xmp + b"\nendstream",
            b"<< /Type /Filespec /F (factur-x.xml) /UF (factur-x.xml) /AFRelationship /Data /EF << /F 8 0 R >> >>",
            f"<< /Type /EmbeddedFile /Subtype /text#2Fxml /Filter /FlateDecode /Length {len(compressed_xml)} >>\nstream\n".encode("ascii") + compressed_xml + b"\nendstream",
            b"<< /EmbeddedFiles << /Names [ (factur-x.xml) 7 0 R ] >> >>",
        ]
    else:
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]

    path.write_bytes(_build_minimal_pdf(objects, root_obj_num=1))
    return path


def make_gfm_markdown_bundle(dir_path: Path) -> dict[str, Path]:
    """Create valid GFM .md and .markdown files with headings, tables, task lists, code, and local PNG."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    sub_dir = dir_path / "sub"
    sub_dir.mkdir(exist_ok=True)

    diagram = dir_path / "local_diagram.png"
    img = Image.new("RGB", (64, 64), "#0EA5E9")
    ImageDraw.Draw(img).rectangle([8, 8, 56, 56], fill="#0A0F1D", outline="#F8FAFC", width=2)
    img.save(diagram)

    ref_md = sub_dir / "ref.md"
    ref_md.write_text("# Referencia Local\n\nDocumento enlazado localmente.\n", encoding="utf-8")

    gfm_text = (
        "# Titulo Principal\n\n"
        "Texto introductorio con **negrita**, *cursiva* y `codigo en linea`.\n\n"
        "## 1. Arquitectura\n\n"
        "### 1.1 Detalles Tecnicos\n\n"
        "| Componente | Estado | Privacidad |\n"
        "| --- | --- | --- |\n"
        "| PDFium | Activo | Local |\n"
        "| Markdown | Activo | Sanitizado |\n\n"
        "- [x] Validacion local `path_guard`\n"
        "- [ ] Firma opcional PKCS#12\n\n"
        "> Nota arquitectonica: cero conexiones ocultas.\n\n"
        "```python\n"
        "def hello_trodai():\n"
        "    return 'PDF/MD Lector'\n"
        "```\n\n"
        "![Diagrama Local](./local_diagram.png)\n\n"
        "[Referencia interna](./sub/ref.md)\n\n"
        "## 2. Matriz de Pruebas\n\n"
        "Seccion final de verificacion.\n"
    )
    valid_md = dir_path / "valid_gfm.md"
    valid_md.write_text(gfm_text, encoding="utf-8")

    valid_markdown = dir_path / "valid_gfm.markdown"
    valid_markdown.write_text(gfm_text.replace("# Titulo Principal", "# Alternativo Markdown"), encoding="utf-8")

    return {
        "valid_md": valid_md,
        "valid_markdown": valid_markdown,
        "ref_md": ref_md,
        "diagram_png": diagram,
    }


def make_adversarial_markdown(dir_path: Path) -> dict[str, Path]:
    """Create malicious, edge-case, and stress Markdown test files."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)

    xss_md = dir_path / "xss_scripts.md"
    xss_md.write_text(
        "# XSS Probe\n\n"
        "<script>alert('xss')</script>\n\n"
        '<img src="local_diagram.png" onerror="alert(1)">\n\n'
        '<svg onload="fetch(\'http://evil.invalid\')"></svg>\n\n'
        '<iframe src="https://evil.invalid/frame"></iframe>\n\n'
        "[Click Me](javascript:alert(1))\n",
        encoding="utf-8",
    )

    remote_md = dir_path / "remote_images.md"
    remote_md.write_text(
        "# Remote Images\n\n"
        "![tracker](https://example.com/pixel.png)\n\n"
        "![plain_http](http://127.0.0.1:9999/tracker.gif)\n\n"
        '<img src="https://evil.org/photo.jpg">\n',
        encoding="utf-8",
    )

    traversal_md = dir_path / "path_traversal_unc.md"
    traversal_md.write_text(
        "# Path Traversal and UNC\n\n"
        "![unc](\\\\192.168.1.50\\share\\leak.png)\n\n"
        "![unc_fwd](//fileserver/pub/leak.png)\n\n"
        "![trav](../../outside_secret.png)\n\n"
        "![file_uri](file:///C:/Windows/System32/drivers/etc/hosts)\n\n"
        "![data_uri](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)\n",
        encoding="utf-8",
    )

    empty_md = dir_path / "empty.md"
    empty_md.write_text("", encoding="utf-8")

    no_headings_md = dir_path / "no_headings.md"
    no_headings_md.write_text("Plain paragraph without any Markdown heading markers.\n", encoding="utf-8")

    huge_lines = ["# Stress Heading 0\n"]
    for i in range(1, 520):
        level = "#" * ((i % 6) + 1)
        huge_lines.append(f"{level} Section {i}\n\nParagraph content for section {i}.\n\n")
    huge_lines.append("```python\nunclosed_fence = True\n")
    huge_md = dir_path / "huge.md"
    huge_md.write_text("".join(huge_lines), encoding="utf-8")

    return {
        "xss": xss_md,
        "remote": remote_md,
        "traversal": traversal_md,
        "empty": empty_md,
        "no_headings": no_headings_md,
        "huge": huge_md,
    }


def make_sample_images(dir_path: Path) -> dict[str, Path]:
    """Create PNG, JPG, WebP, multi-frame animated GIF, and corrupt PNG fixtures."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)

    png_path = dir_path / "sample.png"
    img_png = Image.new("RGB", (640, 480), "#0EA5E9")
    ImageDraw.Draw(img_png).rectangle([40, 40, 600, 440], fill="#0A0F1D", outline="#F8FAFC", width=4)
    img_png.save(png_path, format="PNG")

    jpg_path = dir_path / "sample.jpg"
    img_jpg = Image.new("RGB", (800, 600), "#3B82F6")
    img_jpg.save(jpg_path, format="JPEG", quality=90)

    webp_path = dir_path / "sample.webp"
    img_png.save(webp_path, format="WEBP")

    gif_path = dir_path / "animated.gif"
    frame1 = Image.new("RGB", (128, 64), "#0A0F1D")
    frame2 = Image.new("RGB", (128, 64), "#0EA5E9")
    frame1.save(gif_path, format="GIF", save_all=True, append_images=[frame2], duration=100, loop=0)

    corrupt_png = dir_path / "corrupt.png"
    corrupt_png.write_bytes(b"\x89PNG\r\n\x1a\nTRUNCATED_BAD_BYTES")

    return {
        "png": png_path,
        "jpg": jpg_path,
        "webp": webp_path,
        "gif": gif_path,
        "corrupt_png": corrupt_png,
    }


def make_local_glossary_json(path: Path, *, fmt: str = "list") -> Path:
    """Create valid or invalid local dictionary glossary JSON fixtures."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "list":
        payload: Any = [
            {"word": "contrat", "pos": "nom", "definition": "Accord juridique entre parties.", "synonyms": ["pacte", "accord"]},
            {"word": "confidentialite", "pos": "nom", "definition": "Protection locale des donnees.", "synonyms": ["secret"]},
        ]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif fmt == "dict":
        payload = {
            "datenschutz": {"pos": "Subst.", "definition": "Schutz personenbezogener Daten.", "synonyms": ["Privatsphaere"]},
            "vertrag": {"pos": "Subst.", "definition": "Rechtsgeschaeft zwischen Parteien.", "synonyms": ["Abkommen"]},
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif fmt == "simple":
        path.write_text(json.dumps({"rubrica": "Firma o trazo personal."}), encoding="utf-8")
    elif fmt == "malformed":
        path.write_text("{not valid json", encoding="utf-8")
    elif fmt == "empty":
        path.write_text("{}", encoding="utf-8")
    else:
        raise ValueError(f"Unknown glossary fixture format: {fmt}")
    return path


def make_theme_package(dir_path: Path, *, kind: str = "valid_dark") -> Path:
    """Create valid, unsafe, or corrupt .trodai-theme.json test packages."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)

    if kind == "valid_dark":
        banner = dir_path / "theme_banner.png"
        Image.new("RGB", (128, 64), "#0A0F1D").save(banner)
        out = dir_path / "valid_dark.trodai-theme.json"
        out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "TRODAI QA Dark",
                    "base_mode": "dark",
                    "motion_level": "modern",
                    "motion_preset": "polygonal_mesh",
                    "banner_path": str(banner),
                    "apply_to_hub_banner": True,
                    "apply_to_viewer_backdrop": True,
                    "opacity": 0.65,
                    "blur_radius": 4,
                    "auto_scrim": True,
                    "fps_cap": 60,
                    "pause_on_battery_saver": True,
                    "language": "es",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return out

    if kind == "valid_gif":
        imgs = make_sample_images(dir_path / "theme_media")
        out = dir_path / "valid_gif.trodai-theme.json"
        out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "TRODAI Animated GIF",
                    "base_mode": "dark",
                    "motion_level": "modern",
                    "motion_preset": "polygonal_mesh",
                    "banner_path": str(imgs["gif"]),
                    "apply_to_hub_banner": True,
                    "apply_to_viewer_backdrop": True,
                    "opacity": 0.6,
                    "blur_radius": 0,
                    "auto_scrim": True,
                    "fps_cap": 60,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return out

    if kind == "unsafe_remote":
        out = dir_path / "unsafe_remote.trodai-theme.json"
        out.write_text(
            json.dumps(
                {
                    "base_mode": "dark",
                    "motion_level": "modern",
                    "banner_path": "https://evil.invalid/tracker.png",
                    "opacity": 5.0,
                    "blur_radius": 99,
                    "fps_cap": 144,
                }
            ),
            encoding="utf-8",
        )
        return out

    if kind == "unsafe_unc":
        out = dir_path / "unsafe_unc.trodai-theme.json"
        out.write_text(
            json.dumps(
                {
                    "base_mode": "dark",
                    "motion_level": "modern",
                    "banner_path": r"\\192.168.1.50\share\bg.png",
                }
            ),
            encoding="utf-8",
        )
        return out

    if kind == "corrupt_json":
        out = dir_path / "corrupt.trodai-theme.json"
        out.write_text("{corrupt", encoding="utf-8")
        return out

    if kind == "corrupt_list":
        out = dir_path / "list.trodai-theme.json"
        out.write_text("[1, 2, 3]", encoding="utf-8")
        return out

    raise ValueError(f"Unknown theme package kind: {kind}")


def make_pii_trace_payload(path: Path | None = None) -> tuple[str, dict[str, str]]:
    """Build a synthetic crash traceback containing PII needles that anonymizers must scrub."""
    username = os.environ.get("USERNAME", "DairoTestUser") or "DairoTestUser"
    computer = os.environ.get("COMPUTERNAME", "WORKSTATION-QA99") or "WORKSTATION-QA99"
    email = "dairo.qa.test@example.org"
    ipv4 = "192.168.42.77"
    ipv6 = "fe80::1ff:fe23:4567:890a"
    secret_doc = "Contrato_Confidencial_NIT_900123456.pdf"
    onedrive_path = rf"C:\Users\{username}\OneDrive - Fundacion Mamonal\Privado\{secret_doc}"

    trace = (
        f"2026-11-24T17:45:00Z ERROR Host={computer} User={username} IP={ipv4} IPv6={ipv6}\n"
        f"Contact: {email}\n"
        f'Traceback (most recent call last):\n'
        f'  File "{onedrive_path}", line 42, in open_doc\n'
        f"    raise RuntimeError('Failed parsing {onedrive_path} for {email}')\n"
        f"RuntimeError: Failed parsing {secret_doc}\n"
    )
    needles = {
        "username": username,
        "computer": computer,
        "email": email,
        "ipv4": ipv4,
        "ipv6": ipv6,
        "secret_doc": secret_doc,
        "onedrive_path": onedrive_path,
    }
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trace, encoding="utf-8")
    return trace, needles


def make_update_bundle(
    dir_path: Path,
    *,
    version: str = "0.2.0",
    tamper: bool = False,
    base_url: str = "http://127.0.0.1:0",
) -> dict[str, Any]:
    """Create deterministic update installer bytes, SHA256SUMS.txt, and GitHub release JSON."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)

    filename = "PDF-MD-Lector-Setup.exe"
    payload = b"MZ_FAKE_PORTABLE_INSTALLER_PAYLOAD_" + (b"TRODAI_V020_" * 300)
    digest = hashlib.sha256(payload).hexdigest()

    served_bytes = bytearray(payload)
    if tamper:
        served_bytes[-1] ^= 0x01
    served = bytes(served_bytes)

    installer_path = dir_path / filename
    installer_path.write_bytes(served)

    sums_text = f"{digest}  {filename}\n"
    sums_path = dir_path / "SHA256SUMS.txt"
    sums_path.write_text(sums_text, encoding="utf-8")

    release_json = {
        "tag_name": f"v{version}",
        "name": f"PDF/MD Lector v{version}",
        "body": f"Release notes for v{version}: offline reliability and SHA-256 verification.",
        "html_url": f"{base_url}/releases/tag/v{version}",
        "assets": [
            {
                "name": filename,
                "browser_download_url": f"{base_url}/releases/download/v{version}/{filename}",
                "size": len(served),
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": f"{base_url}/releases/download/v{version}/SHA256SUMS.txt",
                "size": len(sums_text.encode("utf-8")),
            },
        ],
    }

    return {
        "version": version,
        "filename": filename,
        "installer_path": installer_path,
        "installer_bytes": served,
        "expected_sha256": digest,
        "sums_path": sums_path,
        "sums_text": sums_text,
        "release_json": release_json,
    }


def encode_nm_frame(payload: dict[str, Any] | bytes) -> bytes:
    """Encode a Chrome/Edge Native Messaging 4-byte little-endian length-prefixed frame."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def decode_nm_stream(stream_bytes: bytes) -> list[dict[str, Any]]:
    """Decode zero or more 4-byte LE Native Messaging JSON frames from raw stdout bytes."""
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(stream_bytes):
        if offset + 4 > len(stream_bytes):
            raise ValueError("Truncated 4-byte Native Messaging length header")
        (length,) = struct.unpack("<I", stream_bytes[offset : offset + 4])
        offset += 4
        if offset + length > len(stream_bytes):
            raise ValueError("Truncated Native Messaging payload body")
        body = stream_bytes[offset : offset + length]
        offset += length
        frames.append(json.loads(body.decode("utf-8")))
    return frames


def make_mcp_requests(pdf_path: Path, md_path: Path) -> list[dict[str, Any]]:
    """Generate deterministic stdio JSON-RPC 2.0 request sequence for pdf-md-lector-mcp."""
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "e2e-harness", "version": "1.0"}},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "read_pdf_text", "arguments": {"path": str(pdf_path), "page_index": 0}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "read_markdown", "arguments": {"path": str(md_path)}},
        },
    ]


def make_encrypted_aes256_pdf(
    path: Path,
    *,
    user_password: str = "user123",
    owner_password: str = "owner456",
) -> Path:
    """Lazily generate an AES-256 encrypted PDF (requires pikepdf or pypdf from M1+)."""
    path = Path(path)
    plain = path.with_suffix(".plain_tmp.pdf")
    make_multipage_toc_pdf(plain, pages=2, title="Protected Specimen")
    try:
        try:
            import pikepdf  # lazy M1+ dependency

            with pikepdf.open(plain) as pdf:
                pdf.save(
                    path,
                    encryption=pikepdf.Encryption(
                        user=user_password,
                        owner=owner_password,
                        R=6,
                    ),
                )
            return path
        except ImportError:
            from pypdf import PdfReader, PdfWriter  # lazy M1+ dependency

            reader = PdfReader(str(plain))
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="AES-256")
            with path.open("wb") as handle:
                writer.write(handle)
            return path
    finally:
        plain.unlink(missing_ok=True)


def make_self_signed_pkcs12(path: Path, *, password: str = "testpass") -> Path:
    """Lazily generate a self-signed RSA-2048 PKCS#12 (.p12) signing cert (requires cryptography)."""
    import datetime as dt
    from cryptography import x509  # lazy M1+ dependency
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CO"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TRODAI QA"),
            x509.NameAttribute(NameOID.COMMON_NAME, "TRODAI E2E Test Signer"),
        ]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    p12_bytes = pkcs12.serialize_key_and_certificates(
        name=b"trodai-test",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    path.write_bytes(p12_bytes)
    return path


def make_office_inputs(dir_path: Path) -> dict[str, Path]:
    """Generate HTML, TXT, and lazily DOCX/XLSX test inputs for conversion tools."""
    dir_path = Path(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)

    html_path = dir_path / "sample.html"
    html_path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>TRODAI HTML</title></head>"
        "<body><h1>TRODAI Document</h1><p>Local HTML conversion test.</p></body></html>",
        encoding="utf-8",
    )
    txt_path = dir_path / "sample.txt"
    txt_path.write_text("TRODAI Plain Text\nLine 2: Local conversion.\n", encoding="utf-8")

    outputs: dict[str, Path] = {"html": html_path, "txt": txt_path}

    import docx  # lazy M1+ dependency

    docx_path = dir_path / "sample.docx"
    doc = docx.Document()
    doc.add_heading("TRODAI DOCX Specimen", level=1)
    doc.add_paragraph("Structured local paragraph for conversion.")
    doc.save(str(docx_path))
    outputs["docx"] = docx_path

    import openpyxl  # lazy M1+ dependency

    xlsx_path = dir_path / "sample.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Items"
    ws.append(["Producto", "Cantidad", "Valor"])
    ws.append(["Licencia PDF/MD", 1, 100000])
    wb.save(str(xlsx_path))
    outputs["xlsx"] = xlsx_path

    return outputs
