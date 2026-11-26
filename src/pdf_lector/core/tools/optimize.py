"""Optimize and Repair PDF tools (5 tools) with real DPI/quality compression and PDF/A checks."""

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zlib

from ..win32_bridge import atomic_write_text
from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    dpi_param_spec,
    emit_progress,
    get_shared_temp_manager,
    is_unsafe_reparse_or_system,
    make_sibling_stage_path,
    quality_param_spec,
    validate_output_any_path,
    validate_output_pdf_path,
)

_PDFAID_NS = "http://www.aiim.org/pdfa/ns/id/"


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _mat_mul(
    m1: tuple[float, float, float, float, float, float],
    m2: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _round_ctm_linear(
    ctm: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    return (round(ctm[0], 2), round(ctm[1], 2), round(ctm[2], 2), round(ctm[3], 2))


def _get_form_matrix(form_stream: Any) -> tuple[float, float, float, float, float, float]:
    raw = form_stream.get("/Matrix")
    if raw is not None and len(raw) == 6:
        try:
            vals = tuple(float(v) for v in raw)
            if all(math.isfinite(v) for v in vals):
                return vals  # type: ignore[return-value]
        except Exception:
            pass
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _collect_image_draw_sizes(
    container: Any,
    parent_ctm: tuple[float, float, float, float, float, float],
    size_map: dict[tuple[int, int], tuple[float, float]],
    visited_forms: set[tuple[tuple[int, int], tuple[float, float, float, float]]],
    fallback_size: tuple[float, float],
    active_forms: set[tuple[int, int]] | None = None,
) -> None:
    import pikepdf

    if active_forms is None:
        active_forms = set()

    res = container.get("/Resources")
    xobjs = res.get("/XObject") if isinstance(res, pikepdf.Dictionary) else None

    ctm_stack: list[tuple[float, float, float, float, float, float]] = []
    ctm = parent_ctm
    do_called_forms: set[tuple[int, int]] = set()
    try:
        instructions = pikepdf.parse_content_stream(container)
    except Exception:
        instructions = []

    for operands, operator in instructions:
        op = str(operator)
        if op == "q":
            ctm_stack.append(ctm)
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == "cm" and len(operands) == 6:
            try:
                m = tuple(float(v) for v in operands)
                ctm = _mat_mul(m, ctm)  # type: ignore[arg-type]
            except Exception:
                pass
        elif op == "Do" and len(operands) == 1 and isinstance(xobjs, pikepdf.Dictionary):
            name = str(operands[0])
            target = xobjs.get(name)
            if not isinstance(target, pikepdf.Stream):
                continue
            subtype = str(target.get("/Subtype", ""))
            if subtype == "/Image":
                og = getattr(target, "objgen", (0, 0))
                w_pt = max(1.0, math.hypot(ctm[0], ctm[1]))
                h_pt = max(1.0, math.hypot(ctm[2], ctm[3]))
                if og in size_map:
                    prev_w, prev_h = size_map[og]
                    size_map[og] = (max(prev_w, w_pt), max(prev_h, h_pt))
                else:
                    size_map[og] = (w_pt, h_pt)
            elif subtype == "/Form":
                og = getattr(target, "objgen", (0, 0))
                do_called_forms.add(og)
                eff_ctm = _mat_mul(_get_form_matrix(target), ctm)
                ctm_key = (og, _round_ctm_linear(eff_ctm))
                if og != (0, 0):
                    if og in active_forms or ctm_key in visited_forms:
                        continue
                    visited_forms.add(ctm_key)
                    active_forms.add(og)
                try:
                    _collect_image_draw_sizes(target, eff_ctm, size_map, visited_forms, fallback_size, active_forms)
                finally:
                    if og != (0, 0):
                        active_forms.discard(og)

    # Also ensure any XObjects not explicitly matched via Do get fallback size
    if isinstance(xobjs, pikepdf.Dictionary):
        for _, target in xobjs.items():
            if not isinstance(target, pikepdf.Stream):
                continue
            subtype = str(target.get("/Subtype", ""))
            og = getattr(target, "objgen", (0, 0))
            if subtype == "/Image":
                if og not in size_map:
                    size_map[og] = fallback_size
            elif subtype == "/Form" and og not in do_called_forms:
                eff_ctm = _mat_mul(_get_form_matrix(target), parent_ctm)
                ctm_key = (og, _round_ctm_linear(eff_ctm))
                if og != (0, 0):
                    if og in active_forms or ctm_key in visited_forms:
                        continue
                    visited_forms.add(ctm_key)
                    active_forms.add(og)
                try:
                    _collect_image_draw_sizes(target, eff_ctm, size_map, visited_forms, fallback_size, active_forms)
                finally:
                    if og != (0, 0):
                        active_forms.discard(og)


def _downsample_and_recompress_images(
    pdf: Any,
    dpi: int,
    quality: int,
    progress_cb: Any = None,
) -> int:
    import pikepdf
    from PIL import Image

    size_map: dict[tuple[int, int], tuple[float, float]] = {}
    image_objs: list[tuple[tuple[int, int], Any]] = []
    seen_imgs: set[tuple[int, int]] = set()
    visited_forms: set[tuple[tuple[int, int], tuple[float, float, float, float]]] = set()

    for page in pdf.pages:
        mb = [float(v) for v in page.MediaBox]
        fallback = (max(72.0, mb[2] - mb[0]), max(72.0, mb[3] - mb[1]))
        _collect_image_draw_sizes(
            page.obj,
            (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            size_map,
            visited_forms,
            fallback,
        )

    def _gather_images(container: Any, seen_f: set[tuple[int, int]]) -> None:
        res = container.get("/Resources")
        if not isinstance(res, pikepdf.Dictionary):
            return
        xobjs = res.get("/XObject")
        if not isinstance(xobjs, pikepdf.Dictionary):
            return
        for _, obj in xobjs.items():
            if not isinstance(obj, pikepdf.Stream):
                continue
            subtype = str(obj.get("/Subtype", ""))
            og = getattr(obj, "objgen", (0, 0))
            if subtype == "/Image":
                if og == (0, 0) or og not in seen_imgs:
                    if og != (0, 0):
                        seen_imgs.add(og)
                    image_objs.append((og, obj))
            elif subtype == "/Form":
                if og != (0, 0):
                    if og in seen_f:
                        continue
                    seen_f.add(og)
                _gather_images(obj, seen_f)

    visited_gather: set[tuple[int, int]] = set()
    for page in pdf.pages:
        _gather_images(page.obj, visited_gather)

    recompressed = 0
    total_imgs = len(image_objs)
    for idx, (og, obj) in enumerate(image_objs, start=1):
        emit_progress(progress_cb, int(15 + 65 * (idx / max(1, total_imgs))), f"Optimizing image {idx}/{total_imgs}")
        try:
            if bool(obj.get("/ImageMask", False)):
                continue
            pimg = pikepdf.PdfImage(obj)
            orig_w, orig_h = int(pimg.width), int(pimg.height)
            if orig_w <= 0 or orig_h <= 0:
                continue

            drawn_w_pt, drawn_h_pt = size_map.get(og, (float(orig_w), float(orig_h)))
            target_w = min(orig_w, max(1, round(drawn_w_pt * dpi / 72.0)))
            target_h = min(orig_h, max(1, round(drawn_h_pt * dpi / 72.0)))

            pil = pimg.as_pil_image()
            resized = False
            if pil.width > target_w or pil.height > target_h:
                pil = pil.resize((target_w, target_h), Image.Resampling.LANCZOS)
                resized = True

            orig_raw_len = len(bytes(obj.read_raw_bytes()))
            orig_flt = obj.get("/Filter")
            if orig_flt is not None and "ASCII" in str(orig_flt):
                try:
                    orig_cmp_len = len(zlib.compress(bytes(obj.read_bytes()), level=6))
                except Exception:
                    orig_cmp_len = orig_raw_len
            else:
                orig_cmp_len = orig_raw_len

            has_alpha = pil.mode in ("RGBA", "LA") or (pil.mode == "P" and "transparency" in pil.info)
            if has_alpha:
                rgba = pil.convert("RGBA")
                rgb = rgba.convert("RGB")
                alpha = rgba.getchannel("A")
                if quality < 100 or resized:
                    buf = io.BytesIO()
                    rgb.save(buf, format="JPEG", quality=quality, optimize=True)
                    jpeg_bytes = buf.getvalue()
                    if resized or len(jpeg_bytes) < orig_cmp_len:
                        obj.write(jpeg_bytes, filter=pikepdf.Name("/DCTDecode"))
                        obj["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
                        obj["/BitsPerComponent"] = 8
                        obj["/Width"] = rgb.width
                        obj["/Height"] = rgb.height
                        for stale_key in ("/DecodeParms", "/Decode"):
                            if stale_key in obj:
                                del obj[stale_key]
                        smask = pikepdf.Stream(
                            pdf,
                            zlib.compress(alpha.tobytes(), level=9),
                            Type=pikepdf.Name("/XObject"),
                            Subtype=pikepdf.Name("/Image"),
                            Width=alpha.width,
                            Height=alpha.height,
                            ColorSpace=pikepdf.Name("/DeviceGray"),
                            BitsPerComponent=8,
                            Filter=pikepdf.Name("/FlateDecode"),
                        )
                        obj["/SMask"] = pdf.make_indirect(smask)
                        recompressed += 1
            else:
                if pil.mode in ("1", "L"):
                    work = pil.convert("L")
                    cs_name = pikepdf.Name("/DeviceGray")
                elif pil.mode == "CMYK":
                    work = pil
                    cs_name = pikepdf.Name("/DeviceCMYK")
                else:
                    work = pil.convert("RGB")
                    cs_name = pikepdf.Name("/DeviceRGB")

                if quality < 100 or resized:
                    buf = io.BytesIO()
                    work.save(buf, format="JPEG", quality=quality, optimize=True)
                    jpeg_bytes = buf.getvalue()
                    if resized:
                        flate_bytes = zlib.compress(work.tobytes(), level=9)
                        if len(flate_bytes) < len(jpeg_bytes):
                            obj.write(flate_bytes, filter=pikepdf.Name("/FlateDecode"))
                        else:
                            obj.write(jpeg_bytes, filter=pikepdf.Name("/DCTDecode"))
                        obj["/Width"] = work.width
                        obj["/Height"] = work.height
                        obj["/ColorSpace"] = cs_name
                        obj["/BitsPerComponent"] = 8
                        for stale_key in ("/DecodeParms", "/Decode"):
                            if stale_key in obj:
                                del obj[stale_key]
                        recompressed += 1
                    elif len(jpeg_bytes) < orig_cmp_len:
                        obj.write(jpeg_bytes, filter=pikepdf.Name("/DCTDecode"))
                        obj["/Width"] = work.width
                        obj["/Height"] = work.height
                        obj["/ColorSpace"] = cs_name
                        obj["/BitsPerComponent"] = 8
                        for stale_key in ("/DecodeParms", "/Decode"):
                            if stale_key in obj:
                                del obj[stale_key]
                        recompressed += 1
        except Exception:
            continue

    return recompressed


def _handle_compress_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("compress_pdf", err_out, "invalid_output")

    dpi = int(params.get("dpi", 150))
    quality = int(params.get("quality", 75))
    recompress_streams = bool(params.get("recompress_streams", True))
    pw = str(params.get("password") or "")

    in_path = inputs[0]
    input_bytes = in_path.stat().st_size

    try:
        pdf = pikepdf.open(in_path, password=pw) if pw else pikepdf.open(in_path)
    except pikepdf.PasswordError:
        return _error_result("compress_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("compress_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    temp_mgr = get_shared_temp_manager()
    try:
        emit_progress(progress_cb, 10, "Compressing images and streams")
        img_count = _downsample_and_recompress_images(pdf, dpi, quality, progress_cb=progress_cb)
        pdf.remove_unreferenced_resources()

        emit_progress(progress_cb, 88, "Writing compressed PDF")
        with temp_mgr.task_scope(secure=False) as tid:
            stage = make_sibling_stage_path(out_path)
            temp_mgr.register_path(stage, task_id=tid)
            pdf.save(
                stage,
                compress_streams=True,
                recompress_flate=recompress_streams,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
            pdf.close()
            atomic_replace_file(stage, out_path)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    output_bytes = out_path.stat().st_size
    ratio = round(output_bytes / max(1, input_bytes), 4)
    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Compressed PDF from {input_bytes} to {output_bytes} bytes ({img_count} image(s) processed).",
        details={
            "input_bytes": input_bytes,
            "output_bytes": output_bytes,
            "ratio": ratio,
            "images_recompressed": img_count,
            "dpi": dpi,
            "quality": quality,
        },
    )


def _handle_optimize_web(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("optimize_web", err_out, "invalid_output")

    dpi = int(params.get("dpi", 150))
    quality = int(params.get("quality", 85))
    downsample = bool(params.get("downsample_images", False))
    pw = str(params.get("password") or "")
    in_path = inputs[0]
    input_bytes = in_path.stat().st_size

    try:
        pdf = pikepdf.open(in_path, password=pw) if pw else pikepdf.open(in_path)
    except pikepdf.PasswordError:
        return _error_result("optimize_web", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("optimize_web", f"Invalid PDF: {exc}", "invalid_pdf")

    temp_mgr = get_shared_temp_manager()
    img_count = 0
    try:
        emit_progress(progress_cb, 20, "Optimizing for web linearization")
        if downsample or dpi < 150:
            img_count = _downsample_and_recompress_images(pdf, dpi, quality, progress_cb=progress_cb)
        pdf.remove_unreferenced_resources()

        emit_progress(progress_cb, 85, "Saving linearized PDF")
        with temp_mgr.task_scope(secure=False) as tid:
            stage = make_sibling_stage_path(out_path)
            temp_mgr.register_path(stage, task_id=tid)
            pdf.save(
                stage,
                linearize=True,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
            pdf.close()
            atomic_replace_file(stage, out_path)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    with pikepdf.open(out_path) as check_pdf:
        linearized = bool(check_pdf.is_linearized)

    output_bytes = out_path.stat().st_size
    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Optimized PDF for web (linearized={linearized}, {output_bytes} bytes).",
        details={
            "linearized": linearized,
            "dpi": dpi,
            "quality": quality,
            "images_recompressed": img_count,
            "input_bytes": input_bytes,
            "output_bytes": output_bytes,
        },
    )


def _escape_pdf_literal(text: str) -> bytes:
    latin = text.encode("latin-1", errors="replace")
    out = bytearray()
    for b in latin:
        if b in (0x5C, 0x28, 0x29):  # \, (, )
            out.append(0x5C)
        if b in (0x0A, 0x0D):
            out.append(0x20)
        else:
            out.append(b)
    return bytes(out)


def _ensure_freetext_appearance(pdf_doc: Any, annot: Any) -> None:
    import pikepdf

    if "/AP" in annot:
        return
    rect = annot.get("/Rect")
    if not isinstance(rect, pikepdf.Array) or len(rect) != 4:
        return
    try:
        x0, y0, x1, y1 = [float(v) for v in rect]
    except Exception:
        return
    w = max(1.0, abs(x1 - x0))
    h = max(1.0, abs(y1 - y0))
    contents = str(annot.get("/Contents", "") or "")
    color_arr = annot.get("/C")
    fill_ops = b""
    if isinstance(color_arr, pikepdf.Array) and len(color_arr) >= 3:
        try:
            r, g, b = [max(0.0, min(1.0, float(color_arr[i]))) for i in range(3)]
            fill_ops = f"q {r:.3f} {g:.3f} {b:.3f} rg 0 0 {w:.2f} {h:.2f} re f Q\n".encode("ascii")
        except Exception:
            fill_ops = b""
    text_y = max(4.0, h - 14.0)
    esc = _escape_pdf_literal(contents)
    stream_bytes = (
        fill_ops
        + f"q 0.2 0.2 0.2 RG 0.75 w 0 0 {w:.2f} {h:.2f} re S Q\n".encode("ascii")
        + b"BT /Helv 11 Tf 0 0 0 rg "
        + f"4 {text_y:.2f} Td (".encode("ascii")
        + esc
        + b") Tj ET\n"
    )
    helv_font = pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"),
        Subtype=pikepdf.Name("/Type1"),
        BaseFont=pikepdf.Name("/Helvetica"),
    )
    ap_stream = pikepdf.Stream(
        pdf_doc,
        stream_bytes,
        Type=pikepdf.Name("/XObject"),
        Subtype=pikepdf.Name("/Form"),
        FormType=1,
        BBox=pikepdf.Array([0.0, 0.0, w, h]),
        Matrix=pikepdf.Array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0]),
        Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(Helv=helv_font)),
    )
    annot["/AP"] = pikepdf.Dictionary(N=pdf_doc.make_indirect(ap_stream))


def _handle_flatten_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("flatten_pdf", err_out, "invalid_output")

    dpi = int(params.get("dpi", 150))
    quality = int(params.get("quality", 90))
    mode = str(params.get("mode") or "vector").strip().lower()
    if mode not in ("vector", "rasterize"):
        return _error_result("flatten_pdf", f"Unknown flatten mode: {mode}", "invalid_params")
    flatten_forms = bool(params.get("flatten_forms", True))
    flatten_annotations = bool(params.get("flatten_annotations", True))
    pw = str(params.get("password") or "")
    in_path = inputs[0]

    temp_mgr = get_shared_temp_manager()
    with temp_mgr.task_scope(secure=False) as tid:
        stage_out = make_sibling_stage_path(out_path)
        temp_mgr.register_path(stage_out, task_id=tid)

        if mode == "rasterize":
            doc = None
            try:
                doc = pdfium.PdfDocument(str(in_path), password=pw or None)
                doc.init_forms()
                total = len(doc)
                if total == 0:
                    return _error_result("flatten_pdf", "PDF has zero pages", "empty_pdf")
                c = canvas.Canvas(str(stage_out))
                scale = dpi / 72.0
                for idx in range(total):
                    emit_progress(progress_cb, int(10 + 80 * ((idx + 1) / total)), f"Rasterizing page {idx + 1}")
                    page = doc[idx]
                    bitmap = None
                    try:
                        w_pt, h_pt = page.get_size()
                        c.setPageSize((w_pt, h_pt))
                        bitmap = page.render(scale=scale)
                        pil_img = bitmap.to_pil().convert("RGB")
                        img_buf = io.BytesIO()
                        if quality >= 98:
                            pil_img.save(img_buf, format="PNG")
                        else:
                            pil_img.save(img_buf, format="JPEG", quality=quality, optimize=True)
                        img_buf.seek(0)
                        c.drawImage(ImageReader(img_buf), 0, 0, width=w_pt, height=h_pt)
                        c.showPage()
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        page.close()
                c.save()
            except Exception as exc:
                return _error_result("flatten_pdf", f"Rasterize flatten failed: {exc}", "flatten_error")
            finally:
                if doc is not None:
                    doc.close()
            atomic_replace_file(stage_out, out_path)
        else:
            pre_flat = temp_mgr.create_temp_file(suffix=".pdf", prefix="preflat_", task_id=tid)
            inter_pdf = temp_mgr.create_temp_file(suffix=".pdf", prefix="flat_", task_id=tid)
            kept_annots_pdf = temp_mgr.create_temp_file(suffix=".pdf", prefix="kept_", task_id=tid)
            try:
                src_pk = pikepdf.open(in_path, password=pw) if pw else pikepdf.open(in_path)
            except pikepdf.PasswordError:
                return _error_result("flatten_pdf", "Encrypted PDF requires password", "encrypted_pdf")
            except (pikepdf.PdfError, OSError) as exc:
                return _error_result("flatten_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

            has_kept_annots = False
            has_kept_acroform = False
            needs_pdfium_flatten = False
            try:
                total_pages = len(src_pk.pages)
                if total_pages == 0:
                    return _error_result("flatten_pdf", "PDF has zero pages", "empty_pdf")

                had_acroform = "/AcroForm" in src_pk.Root
                has_any_annots = any(bool(p.get("/Annots")) for p in src_pk.pages)

                if (not had_acroform and not has_any_annots) or (not flatten_forms and not flatten_annotations):
                    src_pk.save(stage_out, compress_streams=True)
                else:
                    if flatten_forms != flatten_annotations:
                        src_pk.save(kept_annots_pdf)
                        has_kept_acroform = had_acroform and not flatten_forms
                        has_kept_annots = has_any_annots

                    for page in src_pk.pages:
                        annots = page.get("/Annots")
                        if not isinstance(annots, pikepdf.Array):
                            continue
                        filtered = pikepdf.Array()
                        for annot in annots:
                            if not isinstance(annot, pikepdf.Dictionary):
                                continue
                            is_widget = str(annot.get("/Subtype", "")) == "/Widget"
                            should_flatten = (is_widget and flatten_forms) or ((not is_widget) and flatten_annotations)
                            if not should_flatten:
                                continue
                            needs_pdfium_flatten = True
                            if str(annot.get("/Subtype", "")) == "/FreeText":
                                _ensure_freetext_appearance(src_pk, annot)
                            filtered.append(annot)
                        if len(filtered) > 0:
                            page["/Annots"] = filtered
                        elif "/Annots" in page:
                            del page["/Annots"]

                    if had_acroform and flatten_forms:
                        needs_pdfium_flatten = True

                    if needs_pdfium_flatten:
                        if not had_acroform or not flatten_forms:
                            # Provide temporary minimal /AcroForm so PDFium init_forms() initializes formenv
                            src_pk.Root["/AcroForm"] = src_pk.make_indirect(
                                pikepdf.Dictionary(Fields=pikepdf.Array([]), NeedAppearances=True)
                            )
                        src_pk.save(pre_flat)
                    else:
                        src_pk.save(stage_out, compress_streams=True)
            finally:
                src_pk.close()

            if not needs_pdfium_flatten:
                atomic_replace_file(stage_out, out_path)
            else:
                doc = None
                try:
                    doc = pdfium.PdfDocument(str(pre_flat))
                    doc.init_forms()
                    total = len(doc)
                    if total == 0:
                        return _error_result("flatten_pdf", "PDF has zero pages", "empty_pdf")
                    if doc.formenv is not None:
                        for idx in range(total):
                            emit_progress(progress_cb, int(10 + 55 * ((idx + 1) / total)), f"Flattening page {idx + 1}")
                            page = doc[idx]
                            try:
                                page.flatten(pdfium_c.FLAT_NORMALDISPLAY)
                            finally:
                                page.close()
                    doc.save(str(inter_pdf))
                except Exception as exc:
                    return _error_result("flatten_pdf", f"Vector flatten failed: {exc}", "flatten_error")
                finally:
                    if doc is not None:
                        doc.close()

                try:
                    with pikepdf.open(inter_pdf) as pdf:
                        emit_progress(progress_cb, 80, "Cleaning AcroForm and Annots")
                        if "/AcroForm" in pdf.Root:
                            del pdf.Root["/AcroForm"]
                        for page in pdf.pages:
                            if "/Annots" in page:
                                del page["/Annots"]

                        if has_kept_annots or has_kept_acroform:
                            with pikepdf.open(kept_annots_pdf) as kept_src:
                                if has_kept_acroform and "/AcroForm" in kept_src.Root:
                                    kept_acro = kept_src.Root["/AcroForm"]
                                    if not kept_acro.is_indirect:
                                        kept_acro = kept_src.make_indirect(kept_acro)
                                    pdf.Root["/AcroForm"] = pdf.copy_foreign(kept_acro)
                                for pidx, kpage in enumerate(kept_src.pages):
                                    if pidx >= len(pdf.pages):
                                        break
                                    for ka in kpage.get("/Annots", []):
                                        if not isinstance(ka, pikepdf.Dictionary):
                                            continue
                                        is_widget = str(ka.get("/Subtype", "")) == "/Widget"
                                        keep_this = (is_widget and not flatten_forms) or ((not is_widget) and not flatten_annotations)
                                        if not keep_this:
                                            continue
                                        if "/P" in ka:
                                            del ka["/P"]
                                        ind_ka = ka if ka.is_indirect else kept_src.make_indirect(ka)
                                        copied_ka = pdf.copy_foreign(ind_ka)
                                        copied_ka["/P"] = pdf.pages[pidx].obj
                                        if "/Annots" not in pdf.pages[pidx] or not isinstance(pdf.pages[pidx]["/Annots"], pikepdf.Array):
                                            pdf.pages[pidx]["/Annots"] = pikepdf.Array()
                                        pdf.pages[pidx]["/Annots"].append(copied_ka)

                        pdf.remove_unreferenced_resources()
                        pdf.save(stage_out, compress_streams=True)
                    atomic_replace_file(stage_out, out_path)
                except Exception as exc:
                    return _error_result("flatten_pdf", f"Flatten cleanup failed: {exc}", "flatten_error")

    remaining_annots = 0
    has_acroform = False
    with pikepdf.open(out_path) as chk:
        has_acroform = "/AcroForm" in chk.Root
        for p in chk.pages:
            remaining_annots += len(p.get("/Annots", []))

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Flattened PDF in '{mode}' mode.",
        details={
            "mode": mode,
            "dpi": dpi,
            "quality": quality,
            "has_acroform": has_acroform,
            "remaining_annots": remaining_annots,
        },
    )


def _handle_repair_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    import pypdf
    import pypdfium2 as pdfium

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("repair_pdf", err_out, "invalid_output")

    in_path = inputs[0]
    rebuild_streams = bool(params.get("rebuild_streams", True))

    try:
        header = in_path.read_bytes()[:1024]
    except OSError as exc:
        return _error_result("repair_pdf", f"Cannot read input PDF: {exc}", "io_error")

    if b"%PDF-" not in header:
        return _error_result("repair_pdf", "Input file does not contain a valid %PDF- header", "corrupt_pdf")

    temp_mgr = get_shared_temp_manager()
    with temp_mgr.task_scope(secure=False) as tid:
        stage = make_sibling_stage_path(out_path)
        temp_mgr.register_path(stage, task_id=tid)

        repaired_method = ""
        emit_progress(progress_cb, 30, "Attempting QPDF structure recovery")
        try:
            with pikepdf.open(in_path) as pdf:
                if len(pdf.pages) == 0:
                    raise ValueError("Recovered PDF has zero pages")
                for p in pdf.pages:
                    _ = p.MediaBox
                pdf.save(
                    stage,
                    compress_streams=rebuild_streams,
                    object_stream_mode=pikepdf.ObjectStreamMode.preserve,
                )
                repaired_method = "pikepdf_qpdf"
        except Exception as qpdf_exc:
            emit_progress(progress_cb, 55, "Trying secondary pypdf recovery")
            try:
                reader = pypdf.PdfReader(str(in_path), strict=False)
                if len(reader.pages) == 0:
                    raise ValueError("No pages salvaged")
                writer = pypdf.PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)
                inter = temp_mgr.create_temp_file(suffix=".pdf", prefix="rep_", task_id=tid)
                with open(inter, "wb") as fh:
                    writer.write(fh)
                with pikepdf.open(inter) as norm_pdf:
                    if len(norm_pdf.pages) == 0:
                        raise ValueError("No pages after normalization")
                    norm_pdf.save(stage, compress_streams=rebuild_streams)
                repaired_method = "pypdf_fallback"
            except Exception as pypdf_exc:
                return _error_result(
                    "repair_pdf",
                    f"Could not repair PDF ({qpdf_exc}; fallback: {pypdf_exc})",
                    "unrecoverable_pdf",
                )

        emit_progress(progress_cb, 85, "Verifying repaired PDF")
        doc = None
        try:
            doc = pdfium.PdfDocument(str(stage))
            page_count = len(doc)
            if page_count == 0:
                return _error_result("repair_pdf", "Repaired PDF contains zero pages", "unrecoverable_pdf")
        except Exception as exc:
            return _error_result("repair_pdf", f"Repaired PDF failed verification: {exc}", "unrecoverable_pdf")
        finally:
            if doc is not None:
                doc.close()

        atomic_replace_file(stage, out_path)

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Repaired PDF ({page_count} page(s)) via {repaired_method}.",
        details={"repaired": True, "method": repaired_method, "page_count": page_count},
    )


def _check_forbidden_action(action_obj: Any, errors: list[str], loc: str) -> None:
    import pikepdf

    if not isinstance(action_obj, pikepdf.Dictionary):
        return
    s_type = str(action_obj.get("/S", ""))
    if s_type in ("/JavaScript", "/Launch", "/Sound", "/Movie", "/RichMedia"):
        errors.append(f"forbidden_action:{s_type[1:]}@{loc}")
    if "/JS" in action_obj:
        errors.append(f"forbidden_js@{loc}")


def _decode_xmp_bytes(raw_bytes: bytes) -> str:
    if raw_bytes.startswith(b"\xff\xfe"):
        return raw_bytes.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if raw_bytes.startswith(b"\xfe\xff"):
        return raw_bytes.decode("utf-16-be", errors="replace").lstrip("\ufeff")
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        return raw_bytes.decode("utf-8-sig", errors="replace")
    if b"\x00" in raw_bytes[:64]:
        even_nulls = sum(1 for b in raw_bytes[0:64:2] if b == 0)
        odd_nulls = sum(1 for b in raw_bytes[1:64:2] if b == 0)
        if odd_nulls > even_nulls * 2 and odd_nulls >= 4:
            return raw_bytes.decode("utf-16-le", errors="replace")
        if even_nulls > odd_nulls * 2 and even_nulls >= 4:
            return raw_bytes.decode("utf-16-be", errors="replace")
    return raw_bytes.decode("utf-8", errors="replace")


def _handle_check_pdfa(
    inputs: list[Path],
    output: Path | None,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    in_path = inputs[0]
    strict = bool(params.get("strict", False))

    report_path: Path | None = None
    if output is not None:
        ok_out, out_p, err_out = validate_output_any_path(output)
        if not ok_out or out_p is None:
            return _error_result("check_pdfa", err_out, "invalid_output")
        if out_p.exists() and out_p.is_dir():
            report_path = out_p / f"{in_path.stem}_pdfa_report.json"
            if is_unsafe_reparse_or_system(report_path):
                return _error_result("check_pdfa", "reparse_or_system_blocked", "invalid_output")
        else:
            report_path = out_p

    errors: list[str] = []
    warnings: list[str] = []
    part: str | None = None
    conformance: str | None = None
    has_output_intents = False
    unembedded_fonts: list[str] = []

    emit_progress(progress_cb, 25, "Inspecting PDF/A structure")
    try:
        pdf = pikepdf.open(in_path)
    except pikepdf.PasswordError:
        errors.append("encrypted_pdf_forbidden")
        details = {
            "conformant": False,
            "part": None,
            "conformance": None,
            "has_output_intents": False,
            "unembedded_fonts": [],
            "errors": errors,
            "warnings": warnings,
            "strict": strict,
        }
        written_paths: list[Path] = []
        if report_path is not None:
            atomic_write_text(report_path, json.dumps(details, indent=2))
            written_paths.append(report_path)
        return ToolResult(True, written_paths, "PDF is encrypted and not PDF/A conformant.", details)
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("check_pdfa", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        if pdf.is_encrypted or "/Encrypt" in pdf.trailer:
            errors.append("encrypted_pdf_forbidden")

        # 1. XMP Metadata & pdfaid
        meta_obj = pdf.Root.get("/Metadata")
        if not isinstance(meta_obj, pikepdf.Stream):
            errors.append("missing_xmp_metadata")
        else:
            try:
                xmp_raw = _decode_xmp_bytes(bytes(meta_obj.read_bytes()))
                upper_xmp = xmp_raw.upper()
                if "<!DOCTYPE" in upper_xmp or "<!ENTITY" in upper_xmp:
                    errors.append("unsafe_xmp_dtd")
                else:
                    root_xml = ET.fromstring(xmp_raw)
                    for elem in root_xml.iter():
                        if elem.tag == f"{{{_PDFAID_NS}}}part" and elem.text:
                            part = elem.text.strip()
                        elif elem.tag == f"{{{_PDFAID_NS}}}conformance" and elem.text:
                            conformance = elem.text.strip().upper()
                        # Also check attribute-style XMP (rdf:Description pdfaid:part="3" ...)
                        if f"{{{_PDFAID_NS}}}part" in elem.attrib:
                            part = elem.attrib[f"{{{_PDFAID_NS}}}part"].strip()
                        if f"{{{_PDFAID_NS}}}conformance" in elem.attrib:
                            conformance = elem.attrib[f"{{{_PDFAID_NS}}}conformance"].strip().upper()
            except ET.ParseError:
                errors.append("malformed_xmp_metadata")

        if part not in ("1", "2", "3", "4"):
            errors.append("missing_or_invalid_pdfaid_part")
        if part in ("1", "2", "3") and conformance not in ("A", "B", "U"):
            errors.append("missing_or_invalid_pdfaid_conformance")

        # 2. Forbidden Actions & JavaScript
        names = pdf.Root.get("/Names")
        if isinstance(names, pikepdf.Dictionary) and "/JavaScript" in names:
            errors.append("forbidden_javascript_names")
        if "/OpenAction" in pdf.Root:
            _check_forbidden_action(pdf.Root["/OpenAction"], errors, "Catalog/OpenAction")
        if "/AA" in pdf.Root and isinstance(pdf.Root["/AA"], pikepdf.Dictionary):
            for k, act in pdf.Root["/AA"].items():
                _check_forbidden_action(act, errors, f"Catalog/AA{k}")

        # 3. Embedded Files vs PDF/A Part
        has_embedded = isinstance(names, pikepdf.Dictionary) and "/EmbeddedFiles" in names
        if part == "1" and has_embedded:
            errors.append("embedded_files_forbidden_in_pdfa1")
        if part == "3" and has_embedded:
            af_arr = pdf.Root.get("/AF")
            if not isinstance(af_arr, pikepdf.Array) or len(af_arr) == 0:
                warnings.append("pdfa3_missing_catalog_af")

        # 4. OutputIntents
        oi = pdf.Root.get("/OutputIntents")
        has_output_intents = isinstance(oi, pikepdf.Array) and len(oi) > 0
        if not has_output_intents:
            warnings.append("missing_output_intents")

        # 5. Per-page checks: Fonts, Annots, LZW, Transparency
        emit_progress(progress_cb, 65, "Checking fonts, streams, and annotations")
        for pidx, page in enumerate(pdf.pages):
            if "/AA" in page and isinstance(page["/AA"], pikepdf.Dictionary):
                for k, act in page["/AA"].items():
                    _check_forbidden_action(act, errors, f"Page{pidx + 1}/AA{k}")

            for aidx, annot in enumerate(page.get("/Annots", [])):
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                if part == "1" and str(annot.get("/Subtype", "")) == "/FileAttachment":
                    errors.append(f"embedded_file_annot_forbidden_in_pdfa1@Page{pidx + 1}")
                if "/A" in annot:
                    _check_forbidden_action(annot["/A"], errors, f"Page{pidx + 1}/Annot{aidx}/A")
                if "/AA" in annot and isinstance(annot["/AA"], pikepdf.Dictionary):
                    for k, act in annot["/AA"].items():
                        _check_forbidden_action(act, errors, f"Page{pidx + 1}/Annot{aidx}/AA{k}")

            if part == "1":
                grp = page.get("/Group")
                if isinstance(grp, pikepdf.Dictionary) and str(grp.get("/S", "")) == "/Transparency":
                    errors.append(f"transparency_group_forbidden_in_pdfa1@Page{pidx + 1}")

            res = page.get("/Resources")
            if isinstance(res, pikepdf.Dictionary):
                fonts = res.get("/Font")
                if isinstance(fonts, pikepdf.Dictionary):
                    for fname, fobj in fonts.items():
                        if not isinstance(fobj, pikepdf.Dictionary):
                            continue
                        subtype = str(fobj.get("/Subtype", ""))
                        if subtype == "/Type3":
                            continue
                        fd = fobj.get("/FontDescriptor")
                        if fd is None and subtype == "/Type0":
                            desc_fonts = fobj.get("/DescendantFonts")
                            if isinstance(desc_fonts, pikepdf.Array) and len(desc_fonts) > 0:
                                desc0 = desc_fonts[0]
                                if isinstance(desc0, pikepdf.Dictionary):
                                    fd = desc0.get("/FontDescriptor")
                        embedded = False
                        if isinstance(fd, pikepdf.Dictionary):
                            embedded = any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                        if not embedded:
                            base_font = str(fobj.get("/BaseFont", fname))
                            if base_font not in unembedded_fonts:
                                unembedded_fonts.append(base_font)

                if part == "1":
                    egs = res.get("/ExtGState")
                    if isinstance(egs, pikepdf.Dictionary):
                        for gs_name, gs in egs.items():
                            if not isinstance(gs, pikepdf.Dictionary):
                                continue
                            for alpha_k in ("/CA", "/ca"):
                                if alpha_k in gs:
                                    try:
                                        if float(gs[alpha_k]) < 1.0:
                                            errors.append(f"transparency_alpha_forbidden_in_pdfa1@Page{pidx + 1}{gs_name}")
                                    except (TypeError, ValueError):
                                        errors.append(f"invalid_extgstate_alpha@Page{pidx + 1}{gs_name}")
                            if "/SMask" in gs and str(gs["/SMask"]) != "/None":
                                errors.append(f"transparency_smask_forbidden_in_pdfa1@Page{pidx + 1}{gs_name}")

        # LZW check across objects
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                flt = obj.get("/Filter")
                if flt == pikepdf.Name("/LZWDecode") or (
                    isinstance(flt, pikepdf.Array) and pikepdf.Name("/LZWDecode") in flt
                ):
                    errors.append("lzw_compression_forbidden")
                    break

        if unembedded_fonts:
            warnings.append("unembedded_fonts")

        if strict and warnings:
            errors.extend(warnings)
    finally:
        pdf.close()

    conformant = len(errors) == 0
    details = {
        "conformant": conformant,
        "part": part,
        "conformance": conformance,
        "has_output_intents": has_output_intents,
        "unembedded_fonts": unembedded_fonts,
        "errors": errors,
        "warnings": warnings,
        "strict": strict,
    }
    written_paths = []
    if report_path is not None:
        atomic_write_text(report_path, json.dumps(details, indent=2))
        written_paths.append(report_path)

    emit_progress(progress_cb, 100, "Done")
    summary = (
        f"PDF/A-{part or '?'}{conformance or ''} conformant."
        if conformant
        else f"Not PDF/A conformant ({len(errors)} error(s))."
    )
    return ToolResult(success=True, output_paths=written_paths, summary=summary, details=details)


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="compress_pdf",
        category="optimize",
        icon="compress",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.optimize", "_handle_compress_pdf"),
        params=[
            dpi_param_spec(150),
            quality_param_spec(75),
            ToolParamSpec("recompress_streams", "bool", True),
        ],
    ),
    ToolDefinition(
        id="optimize_web",
        category="optimize",
        icon="compress",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.optimize", "_handle_optimize_web"),
        params=[
            dpi_param_spec(150),
            quality_param_spec(85),
            ToolParamSpec("downsample_images", "bool", False),
        ],
    ),
    ToolDefinition(
        id="flatten_pdf",
        category="optimize",
        icon="edit",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.optimize", "_handle_flatten_pdf"),
        params=[
            dpi_param_spec(150),
            quality_param_spec(90),
            ToolParamSpec("mode", "choice", "vector", choices=("vector", "rasterize")),
            ToolParamSpec("flatten_forms", "bool", True),
            ToolParamSpec("flatten_annotations", "bool", True),
        ],
    ),
    ToolDefinition(
        id="repair_pdf",
        category="optimize",
        icon="reset",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.optimize", "_handle_repair_pdf"),
        params=[ToolParamSpec("rebuild_streams", "bool", True)],
    ),
    ToolDefinition(
        id="check_pdfa",
        category="optimize",
        icon="check",
        badge="PDF/A",
        min_inputs=1,
        max_inputs=1,
        output_kind="any",
        allow_empty_output=True,
        handler=("pdf_lector.core.tools.optimize", "_handle_check_pdfa"),
        params=[ToolParamSpec("strict", "bool", False)],
    ),
)


def register_tools(registry: type[ToolRegistry]) -> None:
    """Register all 5 Optimize & Repair tools."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn)
