"""Comprehensive M2-A unit and integration tests for ToolRegistry and 32 headless PDF tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pikepdf
from PIL import Image, ImageChops
import pypdf
import pypdfium2 as pdfium
import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from pdf_lector.core.i18n import HUB_CATEGORIES
from pdf_lector.core.tools import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    dpi_param_spec,
    emit_progress,
    quality_param_spec,
    source_lang_param_spec,
)
from tests.e2e.fixtures import (
    make_acroform_pdf,
    make_blank_and_text_pdf,
    make_blank_only_pdf,
    make_corrupt_pdf,
    make_encrypted_aes256_pdf,
    make_facturx_pdfa3,
    make_image_pdf,
    make_metadata_dirty_pdf,
    make_multipage_toc_pdf,
    make_pdf_with_image_and_attachment,
    make_redaction_target_pdf,
    make_repairable_bad_xref_pdf,
    make_scanned_text_pdf,
    make_self_signed_pkcs12,
    make_watermark_overlay_pdf,
)

_M2A_TOOL_IDS = {
    "merge_pdf",
    "split_pdf",
    "rotate_pdf",
    "delete_pages",
    "delete_blank_pages",
    "extract_pages",
    "reorder_pages",
    "extract_images",
    "extract_attachments",
    "nup_pages",
    "edit_bookmarks",
    "edit_pdf",
    "add_watermark",
    "add_page_numbers",
    "overlay_pdf",
    "annotate_pdf",
    "crop_pdf",
    "edit_metadata",
    "resize_pages",
    "make_fillable",
    "compress_pdf",
    "optimize_web",
    "flatten_pdf",
    "repair_pdf",
    "check_pdfa",
    "protect_pdf",
    "unlock_pdf",
    "sign_pdf",
    "redact_pdf",
    "strip_metadata",
    "compare_pdf",
    "viewer_preferences",
}

_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+")


# ---------------------------------------------------------------------------
# 1. Registry, ParamSpec, Lazy Loading & Hygiene Contracts
# ---------------------------------------------------------------------------

def test_registry_registers_all_32_m2a_tools_and_unknown_raises() -> None:
    ids = set(ToolRegistry.list_ids())
    assert _M2A_TOOL_IDS.issubset(ids)
    for tid in _M2A_TOOL_IDS:
        assert ToolRegistry.has_tool(tid) is True
        defn = ToolRegistry.get(tid)
        assert isinstance(defn, ToolDefinition)
        assert defn.id == tid
        assert defn.category in ("organize", "optimize", "security", "view_check")

    expected_from_i18n = {
        entry.tool_id
        for cat in HUB_CATEGORIES
        if cat.cat_id in ("organize", "optimize", "security", "view_check")
        for entry in cat.tools
        if entry.tool_id != "pdf_ocr"
    }
    assert expected_from_i18n == _M2A_TOOL_IDS

    with pytest.raises(KeyError):
        ToolRegistry.get("nonexistent_tool_abc_123")


def test_param_specs_validation_and_emit_progress() -> None:
    dpi_spec = dpi_param_spec()
    assert dpi_spec.choices == (72, 150, 200, 300, 600)
    assert dpi_spec.allow_custom is True
    for good_dpi in (36, 72, 150, 200, 240, 300, 600, 1200):
        ok, norm, _ = dpi_spec.validate(good_dpi)
        assert ok is True and norm == good_dpi
    for bad_dpi in (0, -72, 35, 1201, True, 150.5, "abc"):
        ok, _, _ = dpi_spec.validate(bad_dpi)
        assert ok is False

    q_spec = quality_param_spec()
    for good_q in (10, 55, 75, 100):
        ok, norm, _ = q_spec.validate(good_q)
        assert ok is True and norm == good_q
    for bad_q in (-1, 0, 9, 101, False):
        ok, _, _ = q_spec.validate(bad_q)
        assert ok is False

    lang_spec = source_lang_param_spec()
    for good_lang in ("auto", "es", "en", "pt", "fr", "de", "it", "multi"):
        ok, norm, _ = lang_spec.validate(good_lang)
        assert ok is True and norm == good_lang
    for bad_lang in ("", "klingon", "123", "es;rm"):
        ok, _, _ = lang_spec.validate(bad_lang)
        assert ok is False

    seen_1: list[int] = []
    seen_2: list[tuple[int, str]] = []
    emit_progress(lambda p: seen_1.append(p), 42, "step")
    emit_progress(lambda p, m="": seen_2.append((p, m)), 105, "done")
    assert seen_1 == [42]
    assert seen_2 == [(100, "done")]


def test_tools_package_keeps_heavy_libs_lazy_and_zero_emojis() -> None:
    tools_dir = Path(__file__).resolve().parents[1] / "src" / "pdf_lector" / "core" / "tools"
    for py_file in tools_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert _EMOJI_RE.search(content) is None, f"Emoji found in {py_file.name}"

    self_content = Path(__file__).read_text(encoding="utf-8")
    assert _EMOJI_RE.search(self_content) is None

    # Verify fresh Python import of core.tools does not eagerly import heavy PDF libs
    import subprocess

    code = (
        "import sys; from pdf_lector.core.tools import ToolRegistry; "
        "ToolRegistry.list_ids(); "
        "heavy = [m for m in ('pikepdf','pypdf','pyhanko','pypdfium2','reportlab') if m in sys.modules]; "
        "print(','.join(heavy))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# 2. Organize & Edit Tools (20 tools)
# ---------------------------------------------------------------------------

def test_organize_merge_split_extract_reorder_delete(tmp_path: Path) -> None:
    p1 = make_multipage_toc_pdf(tmp_path / "p1.pdf", pages=4)
    p2 = make_multipage_toc_pdf(tmp_path / "p2.pdf", pages=2, title="Second")

    merged = tmp_path / "merged.pdf"
    res_m = ToolRegistry.run("merge_pdf", [p1, p2], merged, {})
    assert res_m.success is True and res_m.details["page_count"] == 6

    split_dir = tmp_path / "split"
    res_s = ToolRegistry.run("split_pdf", [merged], split_dir, {"ranges": "1-2,3-6"})
    assert res_s.success is True and len(res_s.output_paths) == 2

    res_every = ToolRegistry.run("split_pdf", [p1], tmp_path / "split_n", {"mode": "every_n", "chunk_size": 2})
    assert res_every.success is True and len(res_every.output_paths) == 2

    extracted = tmp_path / "extracted.pdf"
    res_ext = ToolRegistry.run("extract_pages", [p1], extracted, {"pages": "2,4"})
    assert res_ext.success is True
    with pikepdf.open(extracted) as pdf:
        assert len(pdf.pages) == 2

    reordered = tmp_path / "reordered.pdf"
    res_reord = ToolRegistry.run("reorder_pages", [p1], reordered, {"order": [4, 3, 2, 1]})
    assert res_reord.success is True
    doc = pdfium.PdfDocument(str(reordered))
    try:
        tp = doc[0].get_textpage()
        try:
            assert "Anotaciones ISO 32000" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()

    deleted = tmp_path / "deleted.pdf"
    res_del = ToolRegistry.run("delete_pages", [p1], deleted, {"pages": [1, 3]})
    assert res_del.success is True
    with pikepdf.open(deleted) as pdf:
        assert len(pdf.pages) == 2

    assert ToolRegistry.run("delete_pages", [p2], tmp_path / "fail_all.pdf", {"pages": [1, 2]}).success is False
    assert ToolRegistry.run("reorder_pages", [p1], tmp_path / "fail_dup.pdf", {"order": [1, 1, 2, 3]}).success is False
    assert ToolRegistry.run("split_pdf", [p1], tmp_path / "fail_rev", {"ranges": "4-2"}).success is False


def test_organize_delete_blank_pages_preserves_scanned_image_page(tmp_path: Path) -> None:
    mixed = make_blank_and_text_pdf(tmp_path / "mixed.pdf")
    scan = make_scanned_text_pdf(tmp_path / "scan.pdf")
    merged = tmp_path / "with_scan.pdf"
    assert ToolRegistry.run("merge_pdf", [mixed, scan], merged, {}).success is True

    cleaned = tmp_path / "cleaned.pdf"
    res = ToolRegistry.run("delete_blank_pages", [merged], cleaned, {})
    assert res.success is True
    # mixed had 3 pages (1 blank removed -> 2) + 1 image-only scan page preserved = 3
    with pikepdf.open(cleaned) as pdf:
        assert len(pdf.pages) == 3

    all_blank = make_blank_only_pdf(tmp_path / "blank_only.pdf")
    assert ToolRegistry.run("delete_blank_pages", [all_blank], tmp_path / "no_out.pdf", {}).success is False


def test_organize_rotate_nup_crop_resize(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=4)

    rot = tmp_path / "rot.pdf"
    assert ToolRegistry.run("rotate_pdf", [src], rot, {"pages": [2], "angle": 90}).success is True
    with pikepdf.open(rot) as pdf:
        assert int(pdf.pages[1].get("/Rotate", 0)) == 90
    assert ToolRegistry.run("rotate_pdf", [src], tmp_path / "bad_rot.pdf", {"angle": 45}).success is False

    nup = tmp_path / "nup.pdf"
    assert ToolRegistry.run("nup_pages", [src], nup, {"nup": 4}).success is True
    with pikepdf.open(nup) as pdf:
        assert len(pdf.pages) == 1
    assert ToolRegistry.run("nup_pages", [src], tmp_path / "bad_nup.pdf", {"nup": 3}).success is False

    cropped = tmp_path / "cropped.pdf"
    assert ToolRegistry.run("crop_pdf", [src], cropped, {"margin": 20}).success is True
    with pikepdf.open(cropped) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        cb = [float(v) for v in pdf.pages[0].CropBox]
        assert cb[0] == pytest.approx(mb[0] + 20.0)
        assert cb[2] == pytest.approx(mb[2] - 20.0)
    assert ToolRegistry.run("crop_pdf", [src], tmp_path / "bad_crop.pdf", {"margin": 5000}).success is False

    custom_sz = tmp_path / "custom_sz.pdf"
    assert ToolRegistry.run("resize_pages", [src], custom_sz, {"preset": "Custom", "width_pt": 500, "height_pt": 700}).success is True
    with pikepdf.open(custom_sz) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        assert abs((mb[2] - mb[0]) - 500.0) <= 1.0
        assert abs((mb[3] - mb[1]) - 700.0) <= 1.0


def test_organize_extract_images_and_zipslip_attachment_sanitization(tmp_path: Path) -> None:
    payload = b"<payload>SAFE_BYTES</payload>"
    # Embed attachment with traversal path inside /F and /UF
    bundle = make_pdf_with_image_and_attachment(tmp_path / "zipslip.pdf", "../../evil_folder/secret.xml", payload)

    img_dir = tmp_path / "imgs"
    r_img = ToolRegistry.run("extract_images", [bundle], img_dir, {})
    assert r_img.success is True and len(r_img.output_paths) == 1
    with Image.open(r_img.output_paths[0]) as im:
        assert im.size == (128, 128)

    att_dir = tmp_path / "atts"
    r_att = ToolRegistry.run("extract_attachments", [bundle], att_dir, {})
    assert r_att.success is True and len(r_att.output_paths) == 1
    out_att = r_att.output_paths[0]
    assert out_att.resolve().parent == att_dir.resolve()
    assert out_att.name == "secret.xml"
    assert out_att.read_bytes() == payload


def test_organize_bookmarks_edit_watermark_numbers_overlay_metadata_annotate_fillable(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    wm = make_watermark_overlay_pdf(tmp_path / "wm.pdf", label="BORRADOR")

    bm = tmp_path / "bm.pdf"
    assert ToolRegistry.run(
        "edit_bookmarks",
        [src],
        bm,
        {"bookmarks": [{"title": "Root A", "page": 1, "children": [{"title": "Child B", "page": 2}]}]},
    ).success is True
    doc = pdfium.PdfDocument(str(bm))
    try:
        titles = [item.get_title() for item in doc.get_toc()]
        assert "Root A" in titles and "Child B" in titles
    finally:
        doc.close()

    edited = tmp_path / "edited.pdf"
    assert ToolRegistry.run("edit_pdf", [src], edited, {"page": 2, "text": "M2A_STAMP_OK", "x": 72, "y": 500}).success is True
    doc2 = pdfium.PdfDocument(str(edited))
    try:
        tp = doc2[1].get_textpage()
        try:
            assert "M2A_STAMP_OK" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc2.close()

    assert ToolRegistry.run("add_watermark", [src], tmp_path / "wm_out.pdf", {"text": "CONFIDENCIAL", "opacity": 0.3}).success is True
    assert ToolRegistry.run("add_watermark", [src], tmp_path / "wm_bad.pdf", {"text": "", "image_path": r"\\evil\share\x.png"}).success is False
    assert ToolRegistry.run("add_page_numbers", [src], tmp_path / "num.pdf", {"format": "Pag {n}/{total}", "position": "top_center"}).success is True
    assert ToolRegistry.run("overlay_pdf", [src, wm], tmp_path / "ov_under.pdf", {"mode": "under"}).success is True

    meta_out = tmp_path / "meta.pdf"
    assert ToolRegistry.run("edit_metadata", [src], meta_out, {"title": "Doc T", "author": "Auth A", "keywords": "k1,k2"}).success is True
    with pikepdf.open(meta_out) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Doc T"
        assert str(pdf.docinfo["/Author"]) == "Auth A"

    ann_out = tmp_path / "ann.pdf"
    assert ToolRegistry.run(
        "annotate_pdf",
        [src],
        ann_out,
        {
            "annotations": [
                {"kind": "highlight", "page_index": 0, "rect": [72, 650, 200, 670], "content": "Review"},
                {"kind": "ink", "page_index": 1, "points": [[[80, 300], [160, 340]]], "author": ""},
            ]
        },
    ).success is True
    with pikepdf.open(ann_out) as pdf:
        assert len(pdf.pages[0]["/Annots"]) == 1
        assert len(pdf.pages[1]["/Annots"]) == 1
        assert str(pdf.pages[1]["/Annots"][0]["/T"]) == "An\u00f3nimo"

    fill_out = tmp_path / "fillable.pdf"
    assert ToolRegistry.run(
        "make_fillable",
        [src],
        fill_out,
        {
            "fields": [
                {"name": "FullName", "kind": "text", "page": 1, "rect": [72, 600, 260, 624]},
                {"name": "Agree", "kind": "checkbox", "page": 1, "rect": [72, 560, 90, 578], "checked": True},
                {"name": "Country", "kind": "choice", "page": 2, "rect": [72, 500, 200, 524], "options": ["CO", "ES"]},
            ]
        },
    ).success is True
    reader = pypdf.PdfReader(str(fill_out))
    fields = reader.get_fields() or {}
    assert {"FullName", "Agree", "Country"}.issubset(set(fields.keys()))


# ---------------------------------------------------------------------------
# 3. Optimize & Repair Tools (5 tools)
# ---------------------------------------------------------------------------

def test_optimize_compress_pdf_dpi_and_quality_produce_measurable_differences(tmp_path: Path) -> None:
    # Build a high-res noisy photo-like 800x800 image PDF drawn at 432x432 pt (6x6 inches)
    g_r = Image.linear_gradient("L").resize((800, 800))
    g_g = g_r.rotate(90)
    g_b = g_r.rotate(180)
    img = Image.merge(
        "RGB",
        (
            ImageChops.add(g_r, Image.effect_noise((800, 800), 18), scale=2.0),
            ImageChops.add(g_g, Image.effect_noise((800, 800), 18), scale=2.0),
            ImageChops.add(g_b, Image.effect_noise((800, 800), 18), scale=2.0),
        ),
    )
    src_pdf = tmp_path / "hi_res.pdf"
    c = canvas.Canvas(str(src_pdf), pagesize=LETTER)
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(img), 72, 200, width=432, height=432)
    c.showPage()
    c.save()

    low_pdf = tmp_path / "low.pdf"
    high_pdf = tmp_path / "high.pdf"
    r_low = ToolRegistry.run("compress_pdf", [src_pdf], low_pdf, {"dpi": 72, "quality": 25})
    r_high = ToolRegistry.run("compress_pdf", [src_pdf], high_pdf, {"dpi": 300, "quality": 95})

    assert r_low.success is True and r_high.success is True
    assert low_pdf.stat().st_size < high_pdf.stat().st_size < src_pdf.stat().st_size
    assert r_low.details["images_recompressed"] >= 1

    with pikepdf.open(low_pdf) as p_low, pikepdf.open(high_pdf) as p_high:
        im_low = next(iter(p_low.pages[0].get_images(recursive=True).values()))
        im_high = next(iter(p_high.pages[0].get_images(recursive=True).values()))
        assert int(im_low["/Width"]) < int(im_high["/Width"])


def test_optimize_web_flatten_repair_and_check_pdfa(tmp_path: Path) -> None:
    img_pdf = make_image_pdf(tmp_path / "img.pdf", size=(512, 512))
    web = tmp_path / "web.pdf"
    r_web = ToolRegistry.run("optimize_web", [img_pdf], web, {"dpi": 72, "downsample_images": True})
    assert r_web.success is True and r_web.details["linearized"] is True
    with pikepdf.open(web) as pdf:
        assert pdf.is_linearized is True

    acro = make_acroform_pdf(tmp_path / "acro.pdf")
    flat_vec = tmp_path / "flat_vec.pdf"
    r_fv = ToolRegistry.run("flatten_pdf", [acro], flat_vec, {"mode": "vector", "dpi": 150})
    assert r_fv.success is True and r_fv.details["has_acroform"] is False
    with pikepdf.open(flat_vec) as pdf:
        assert "/AcroForm" not in pdf.Root

    flat_rast_lo = tmp_path / "flat_72.pdf"
    flat_rast_hi = tmp_path / "flat_200.pdf"
    assert ToolRegistry.run("flatten_pdf", [acro], flat_rast_lo, {"mode": "rasterize", "dpi": 72, "quality": 50}).success is True
    assert ToolRegistry.run("flatten_pdf", [acro], flat_rast_hi, {"mode": "rasterize", "dpi": 200, "quality": 95}).success is True
    assert flat_rast_lo.stat().st_size < flat_rast_hi.stat().st_size

    bad_xref = make_repairable_bad_xref_pdf(tmp_path / "bad_xref.pdf")
    repaired = tmp_path / "repaired.pdf"
    assert ToolRegistry.run("repair_pdf", [bad_xref], repaired, {}).success is True
    garbage = make_corrupt_pdf(tmp_path / "garbage.pdf")
    unrep = tmp_path / "should_not_exist.pdf"
    assert ToolRegistry.run("repair_pdf", [garbage], unrep, {}).success is False
    assert not unrep.exists()

    fx_ok = make_facturx_pdfa3(tmp_path / "fx.pdf", valid=True)
    r_non_strict = ToolRegistry.run("check_pdfa", [fx_ok], tmp_path / "pdfa_ns.json", {"strict": False})
    r_strict = ToolRegistry.run("check_pdfa", [fx_ok], tmp_path / "pdfa_st.json", {"strict": True})
    assert r_non_strict.success is True and r_non_strict.details["conformant"] is True
    assert r_strict.success is True and r_strict.details["conformant"] is False
    assert json.loads((tmp_path / "pdfa_ns.json").read_text(encoding="utf-8"))["conformant"] is True


# ---------------------------------------------------------------------------
# 4. Security & Privacy Tools (5 tools)
# ---------------------------------------------------------------------------

def test_security_protect_unlock_sign_redact_strip(tmp_path: Path) -> None:
    src = make_redaction_target_pdf(tmp_path / "contract.pdf", secret="SECRET_TOKEN_XYZ")

    # Redact in-place safety + destructive check
    red_copy = tmp_path / "redact_inplace.pdf"
    red_copy.write_bytes(src.read_bytes())
    r_red = ToolRegistry.run(
        "redact_pdf",
        [red_copy],
        red_copy,
        {"targets": [{"page_index": 0, "rect": [60, 620, 420, 675]}], "search_terms": ["SECRET_TOKEN_XYZ"]},
    )
    assert r_red.success is True and r_red.details["verified_clean"] is True
    assert b"SECRET_TOKEN_XYZ" not in red_copy.read_bytes()
    assert ToolRegistry.run("redact_pdf", [src], tmp_path / "bad_idx.pdf", {"targets": [{"page_index": 99, "rect": [10, 10, 50, 50]}]}).success is False
    assert ToolRegistry.run("redact_pdf", [src], tmp_path / "bad_rect.pdf", {"targets": [{"page_index": 0, "rect": [0, 0, 0, 0]}]}).success is False

    # Metadata strip
    dirty = make_metadata_dirty_pdf(tmp_path / "dirty.pdf")
    clean = tmp_path / "clean.pdf"
    r_strip = ToolRegistry.run("strip_metadata", [dirty], clean, {})
    assert r_strip.success is True
    with pikepdf.open(clean) as pdf:
        assert "/Metadata" not in pdf.Root and "/PieceInfo" not in pdf.Root
        assert len(pdf.docinfo) == 0

    # Protect with user-only password (verify empty-owner bypass is closed!)
    locked_user_only = tmp_path / "locked_u.pdf"
    r_prot = ToolRegistry.run("protect_pdf", [clean], locked_user_only, {"user_password": "onlyuser", "owner_password": ""})
    assert r_prot.success is True
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(locked_user_only)

    unlocked = tmp_path / "unlocked.pdf"
    assert ToolRegistry.run("unlock_pdf", [locked_user_only], unlocked, {"password": "wrong"}).success is False
    assert ToolRegistry.run("unlock_pdf", [locked_user_only], unlocked, {"password": "onlyuser"}).success is True

    # Sign PDF twice (disambiguates field_name cleanly) + bad passphrase fails
    p12 = make_self_signed_pkcs12(tmp_path / "signer.p12", password="pass")
    signed1 = tmp_path / "signed1.pdf"
    signed2 = tmp_path / "signed2.pdf"
    r_s1 = ToolRegistry.run("sign_pdf", [unlocked], signed1, {"p12_path": str(p12), "password": "pass", "visual": True})
    assert r_s1.success is True and r_s1.details["intact"] is True and r_s1.details["valid"] is True
    assert b"/ByteRange" in signed1.read_bytes()

    r_s2 = ToolRegistry.run("sign_pdf", [signed1], signed2, {"p12_path": str(p12), "password": "pass"})
    assert r_s2.success is True and r_s2.details["field_name"] != r_s1.details["field_name"]

    assert ToolRegistry.run("sign_pdf", [unlocked], tmp_path / "bad_sig.pdf", {"p12_path": str(p12), "password": "wrong"}).success is False
    assert ToolRegistry.run("sign_pdf", [unlocked], tmp_path / "unc_sig.pdf", {"p12_path": r"\\192.168.1.9\s\c.p12", "password": "pass"}).success is False


# ---------------------------------------------------------------------------
# 5. View & Check Tools (2 tools)
# ---------------------------------------------------------------------------

def test_view_compare_pdf_and_viewer_preferences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))

    p_a = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=2, title="Alpha")
    p_b_base = make_multipage_toc_pdf(tmp_path / "b_base.pdf", pages=2, title="Beta")
    p_b = tmp_path / "b.pdf"
    assert ToolRegistry.run("edit_pdf", [p_b_base], p_b, {"page": 1, "text": "DIFF_DELTA_LINE", "x": 72, "y": 450}).success is True

    same_json = tmp_path / "same.json"
    r_same = ToolRegistry.run("compare_pdf", [p_a, p_a], same_json, {"dpi": 72})
    assert r_same.success is True and r_same.details["has_differences"] is False

    diff_pdf = tmp_path / "diff.pdf"
    r_diff = ToolRegistry.run("compare_pdf", [p_a, p_b], diff_pdf, {"dpi": 72})
    assert r_diff.success is True and r_diff.details["has_differences"] is True
    assert diff_pdf.exists() and diff_pdf.with_suffix(".json").exists()
    assert ToolRegistry.run("compare_pdf", [p_a], tmp_path / "err.json", {}).success is False

    # Viewer preferences JSON mode and PDF mode
    prefs_json = tmp_path / "prefs.json"
    r_pj = ToolRegistry.run("viewer_preferences", [], prefs_json, {"default_zoom": 1.5, "default_rail_mode": "both"})
    assert r_pj.success is True and json.loads(prefs_json.read_text(encoding="utf-8"))["default_zoom"] == 1.5

    prefs_pdf = tmp_path / "prefs.pdf"
    r_pp = ToolRegistry.run(
        "viewer_preferences",
        [p_a],
        prefs_pdf,
        {"page_layout": "TwoPageLeft", "page_mode": "UseOutlines", "hide_toolbar": True},
    )
    assert r_pp.success is True
    with pikepdf.open(prefs_pdf) as pdf:
        assert str(pdf.Root["/PageLayout"]) == "/TwoPageLeft"
        assert str(pdf.Root["/PageMode"]) == "/UseOutlines"
        assert bool(pdf.Root["/ViewerPreferences"]["/HideToolbar"]) is True

    assert ToolRegistry.run("viewer_preferences", [], tmp_path / "bad_prefs.json", {"default_zoom": 99.0}).success is False


# ---------------------------------------------------------------------------
# 6. Regression Guards (In-Place WinError 5, Flatten, Compress, Check PDF/A, Security)
# ---------------------------------------------------------------------------

def test_inplace_execution_succeeds_across_modifying_tools(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "base.pdf", pages=3)
    extra = make_multipage_toc_pdf(tmp_path / "extra.pdf", pages=1)
    img_src = make_image_pdf(tmp_path / "img_base.pdf", size=(256, 256))
    p12 = make_self_signed_pkcs12(tmp_path / "signer.p12", password="pw")

    def _clone(name: str, template: Path = src) -> Path:
        p = tmp_path / name
        p.write_bytes(template.read_bytes())
        return p

    p_rot = _clone("rot_inplace.pdf")
    assert ToolRegistry.run("rotate_pdf", [p_rot], p_rot, {"angle": 90}).success is True

    p_del = _clone("del_inplace.pdf")
    assert ToolRegistry.run("delete_pages", [p_del], p_del, {"pages": [3]}).success is True

    p_mrg = _clone("merge_inplace.pdf")
    assert ToolRegistry.run("merge_pdf", [p_mrg, extra], p_mrg, {}).success is True
    with pikepdf.open(p_mrg) as pdf:
        assert len(pdf.pages) == 4

    p_crp = _clone("crop_inplace.pdf")
    assert ToolRegistry.run("crop_pdf", [p_crp], p_crp, {"margin": 12}).success is True

    p_meta = _clone("meta_inplace.pdf")
    assert ToolRegistry.run("edit_metadata", [p_meta], p_meta, {"title": "InPlace"}).success is True

    p_cmp = _clone("comp_inplace.pdf", img_src)
    assert ToolRegistry.run("compress_pdf", [p_cmp], p_cmp, {"dpi": 72, "quality": 60}).success is True

    p_web = _clone("web_inplace.pdf", img_src)
    assert ToolRegistry.run("optimize_web", [p_web], p_web, {"dpi": 72}).success is True

    p_flt = _clone("flat_inplace.pdf")
    assert ToolRegistry.run("flatten_pdf", [p_flt], p_flt, {"mode": "vector"}).success is True

    p_sec = _clone("sec_inplace.pdf")
    assert ToolRegistry.run("protect_pdf", [p_sec], p_sec, {"user_password": "u1", "owner_password": "o1"}).success is True
    assert ToolRegistry.run("unlock_pdf", [p_sec], p_sec, {"password": "o1"}).success is True
    assert ToolRegistry.run("sign_pdf", [p_sec], p_sec, {"p12_path": str(p12), "passphrase": "pw", "visible": True}).success is True

    p_vp = _clone("vp_inplace.pdf")
    assert ToolRegistry.run("viewer_preferences", [p_vp], p_vp, {"page_layout": "TwoPageRight"}).success is True
    with pikepdf.open(p_vp) as pdf:
        assert str(pdf.Root["/PageLayout"]) == "/TwoPageRight"


def test_flatten_pdf_without_acroform_and_ink_without_print_flag(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "plain.pdf", pages=1)
    ann = tmp_path / "annotated.pdf"
    assert ToolRegistry.run(
        "annotate_pdf",
        [src],
        ann,
        {"annotations": [{"kind": "highlight", "page_index": 0, "rect": [72, 650, 220, 670], "content": "Note"}]},
    ).success is True

    # Inject an /Ink annotation deliberately omitting /F 4 (Print flag)
    with pikepdf.open(ann, allow_overwriting_input=True) as pk:
        assert "/AcroForm" not in pk.Root
        ink = pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Ink"),
            Rect=pikepdf.Array([80, 500, 220, 600]),
            C=pikepdf.Array([0.0, 0.0, 1.0]),
            InkList=pikepdf.Array([pikepdf.Array([80, 550, 220, 550])]),
            BS=pikepdf.Dictionary(W=4.0, S=pikepdf.Name("/S")),
        )
        pk.pages[0]["/Annots"].append(pk.make_indirect(ink))
        pk.save(ann)

    flat = tmp_path / "flat_no_acro.pdf"
    res = ToolRegistry.run("flatten_pdf", [ann], flat, {"mode": "vector", "dpi": 150})
    assert res.success is True
    assert res.details["has_acroform"] is False
    assert res.details["remaining_annots"] == 0

    doc = pdfium.PdfDocument(str(flat))
    try:
        bm = doc[0].render(scale=1.0)
        try:
            im = bm.to_pil().convert("RGB")
            blue_px = sum(
                1
                for y in range(230, 255)
                for x in range(80, 220)
                if im.getpixel((x, y))[2] > 180 and im.getpixel((x, y))[0] < 80
            )
            assert blue_px > 0
        finally:
            bm.close()
    finally:
        doc.close()


def test_compress_pdf_shared_form_xobject_decode_and_no_flate_bloat(tmp_path: Path) -> None:
    import zlib

    # Shared Form XObject drawn at 36pt on page 1 and 432pt on page 2, with /Decode present
    pk = pikepdf.Pdf.new()
    im = Image.new("RGB", (1000, 1000), (20, 120, 220))
    im_s = pikepdf.Stream(
        pk,
        zlib.compress(im.tobytes()),
        Type=pikepdf.Name("/XObject"),
        Subtype=pikepdf.Name("/Image"),
        Width=1000,
        Height=1000,
        ColorSpace=pikepdf.Name("/DeviceRGB"),
        BitsPerComponent=8,
        Filter=pikepdf.Name("/FlateDecode"),
        Decode=pikepdf.Array([0, 1, 0, 1, 0, 1]),
    )
    form_s = pikepdf.Stream(
        pk,
        b"q 1 0 0 1 0 0 cm /Img Do Q",
        Type=pikepdf.Name("/XObject"),
        Subtype=pikepdf.Name("/Form"),
        FormType=1,
        BBox=pikepdf.Array([0, 0, 1, 1]),
        Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Img=pk.make_indirect(im_s))),
    )
    form_ind = pk.make_indirect(form_s)
    p1 = pk.add_blank_page(page_size=(612, 792))
    p1["/Resources"] = pikepdf.Dictionary(XObject=pikepdf.Dictionary(F1=form_ind))
    p1["/Contents"] = pk.make_stream(b"q 36 0 0 36 72 700 cm /F1 Do Q")
    p2 = pk.add_blank_page(page_size=(612, 792))
    p2["/Resources"] = pikepdf.Dictionary(XObject=pikepdf.Dictionary(F1=form_ind))
    p2["/Contents"] = pk.make_stream(b"q 432 0 0 432 72 200 cm /F1 Do Q")
    fx_pdf = tmp_path / "shared_form.pdf"
    pk.save(fx_pdf)
    pk.close()

    fx_out = tmp_path / "shared_form_72.pdf"
    r_fx = ToolRegistry.run("compress_pdf", [fx_pdf], fx_out, {"dpi": 72, "quality": 60})
    assert r_fx.success is True
    with pikepdf.open(fx_out) as chk:
        f_img = chk.pages[1]["/Resources"]["/XObject"]["/F1"]["/Resources"]["/XObject"]["/Img"]
        assert int(f_img["/Width"]) == 432
        assert int(f_img["/Height"]) == 432
        assert "/Decode" not in f_img

    # High quality on compact unresized Flate image must not bloat file size
    flat_img_pdf = make_image_pdf(tmp_path / "compact_flat.pdf", size=(512, 512))
    flat_out = tmp_path / "compact_out.pdf"
    r_hi = ToolRegistry.run("compress_pdf", [flat_img_pdf], flat_out, {"dpi": 300, "quality": 95})
    assert r_hi.success is True
    assert flat_out.stat().st_size <= flat_img_pdf.stat().st_size


def test_check_pdfa_output_none_utf16_xmp_and_type0_descendant_fonts(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    r_none = ToolRegistry.run("check_pdfa", [src], None, {})
    assert r_none.success is True
    assert r_none.output_paths == []
    assert r_none.details["conformant"] is False

    xmp_str = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/">'
        '<pdfaid:part>3</pdfaid:part><pdfaid:conformance>B</pdfaid:conformance>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    utf16_bom = b"\xff\xfe" + xmp_str.encode("utf-16-le")
    u16_pdf = tmp_path / "u16_type0.pdf"
    with pikepdf.open(src) as pk:
        pk.Root["/Metadata"] = pk.make_indirect(
            pikepdf.Stream(pk, utf16_bom, Type=pikepdf.Name("/Metadata"), Subtype=pikepdf.Name("/XML"))
        )
        ff2 = pk.make_indirect(pikepdf.Stream(pk, b"dummyfontbytes"))
        fd = pk.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name("/FontDescriptor"), FontName=pikepdf.Name("/TestCID"), FontFile2=ff2)
        )
        cid = pk.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/CIDFontType2"),
                BaseFont=pikepdf.Name("/TestCID"),
                FontDescriptor=fd,
            )
        )
        t0 = pk.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/Type0"),
                BaseFont=pikepdf.Name("/TestCID"),
                DescendantFonts=pikepdf.Array([cid]),
            )
        )
        pk.pages[0]["/Resources"]["/Font"]["/T0"] = t0
        pk.save(u16_pdf)

    r_u16 = ToolRegistry.run("check_pdfa", [u16_pdf], None, {})
    assert r_u16.success is True
    assert r_u16.details["conformant"] is True
    assert r_u16.details["part"] == "3"
    assert "/TestCID" not in r_u16.details["unembedded_fonts"]

    # Raw UTF-16LE XXE payload in /Metadata must trigger unsafe_xmp_dtd
    from pdf_lector.core.tools.optimize import _decode_xmp_bytes

    xxe_u16 = b"\xff\xfe" + '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///C:/win.ini">]><x/>'.encode(
        "utf-16-le"
    )
    decoded_xxe = _decode_xmp_bytes(xxe_u16)
    assert "<!DOCTYPE" in decoded_xxe.upper()


def test_organize_and_security_edge_cases_radio_dos_split_bool_aliases(tmp_path: Path) -> None:
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=4)

    # bool rejected as page index in edit_pdf and make_fillable
    assert ToolRegistry.run("edit_pdf", [src], tmp_path / "b1.pdf", {"edits": [{"page": True, "text": "X"}]}).success is False
    assert (
        ToolRegistry.run(
            "make_fillable",
            [src],
            tmp_path / "b2.pdf",
            {"fields": [{"name": "F", "kind": "text", "page_index": False, "rect": [10, 10, 100, 30]}]},
        ).success
        is False
    )

    # make_fillable radio group across pages registers parent field in /AcroForm/Fields
    rad_out = tmp_path / "radio.pdf"
    r_rad = ToolRegistry.run(
        "make_fillable",
        [src],
        rad_out,
        {
            "fields": [
                {"name": "choice_grp", "kind": "radio", "page": 1, "rect": [72, 500, 90, 518], "value": "A", "checked": False},
                {"name": "choice_grp", "kind": "radio", "page": 2, "rect": [120, 500, 138, 518], "value": "B", "checked": True},
            ]
        },
    )
    assert r_rad.success is True
    rad_fields = pypdf.PdfReader(str(rad_out)).get_fields() or {}
    assert "choice_grp" in rad_fields

    # split_pdf with ranges=[] and mode="every_n"
    sp_dir = tmp_path / "split_empty_ranges"
    r_sp = ToolRegistry.run("split_pdf", [src], sp_dir, {"ranges": [], "mode": "every_n", "chunk_size": 2})
    assert r_sp.success is True and len(r_sp.output_paths) == 2

    # extract_attachments sanitizes Windows DOS reserved names & case-insensitive duplicates
    att_pdf = tmp_path / "dos_atts.pdf"
    att_pdf.write_bytes(src.read_bytes())
    with pikepdf.open(att_pdf, allow_overwriting_input=True) as pk:
        pk.attachments["NUL"] = pikepdf.AttachedFileSpec(pk, b"1", filename="NUL")
        pk.attachments["CON"] = pikepdf.AttachedFileSpec(pk, b"2", filename="CON")
        pk.attachments["aux.txt"] = pikepdf.AttachedFileSpec(pk, b"3", filename="aux.txt")
        pk.attachments["COM1"] = pikepdf.AttachedFileSpec(pk, b"4", filename="COM1")
        pk.attachments["Report.txt"] = pikepdf.AttachedFileSpec(pk, b"5", filename="Report.txt")
        pk.attachments["report.txt"] = pikepdf.AttachedFileSpec(pk, b"6", filename="report.txt")
        pk.save(att_pdf)

    att_dir = tmp_path / "dos_atts_out"
    r_att = ToolRegistry.run("extract_attachments", [att_pdf], att_dir, {})
    assert r_att.success is True and len(r_att.output_paths) == 6
    lower_names = [p.name.lower() for p in r_att.output_paths]
    assert len(set(lower_names)) == 6
    assert all(p.exists() and p.is_file() for p in r_att.output_paths)

    # sign_pdf aliases (passphrase & visible)
    p12 = make_self_signed_pkcs12(tmp_path / "alias_signer.p12", password="secret")
    sig_out = tmp_path / "alias_signed.pdf"
    r_sig = ToolRegistry.run("sign_pdf", [src], sig_out, {"p12_path": str(p12), "passphrase": "secret", "visible": True})
    assert r_sig.success is True and r_sig.details["intact"] is True
    with pikepdf.open(sig_out) as spk:
        assert len(spk.pages[0].get("/Annots", [])) >= 1
