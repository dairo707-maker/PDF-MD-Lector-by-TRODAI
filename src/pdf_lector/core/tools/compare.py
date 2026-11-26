"""View and Check PDF tools (2 tools): visual/text PDF comparison and viewer preferences."""

from __future__ import annotations

import difflib
import io
import json
from pathlib import Path
from typing import Any

from ..path_guard import validate_local_pdf_path
from ..win32_bridge import atomic_write_text, default_app_data_dir
from . import (
    STANDARD_DPI_MAX,
    STANDARD_DPI_MIN,
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    emit_progress,
    get_shared_temp_manager,
    is_unsafe_reparse_or_system,
    make_sibling_stage_path,
    validate_output_any_path,
)

_DEFAULT_ANON_AUTHOR = "An\u00f3nimo"


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _render_diff_pdf(
    diff_frames: list[tuple[int, Any]],
    out_pdf: Path,
) -> None:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    temp_mgr = get_shared_temp_manager()
    with temp_mgr.task_scope(secure=False) as tid:
        stage = make_sibling_stage_path(out_pdf)
        temp_mgr.register_path(stage, task_id=tid)
        c = canvas.Canvas(str(stage), pagesize=LETTER)
        w_pt, h_pt = LETTER
        for page_num, pil_img in diff_frames:
            c.setFont("Helvetica-Bold", 11)
            c.drawString(36, h_pt - 30, f"PDF Visual Diff - Page {page_num}")
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            buf.seek(0)
            c.drawImage(
                ImageReader(buf),
                36,
                36,
                width=w_pt - 72,
                height=h_pt - 84,
                preserveAspectRatio=True,
                anchor="c",
            )
            c.showPage()
        if not diff_frames:
            c.setFont("Helvetica", 12)
            c.drawString(72, h_pt - 72, "No visual differences detected.")
            c.showPage()
        c.save()
        atomic_replace_file(stage, out_pdf)


def _handle_compare_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    from PIL import Image, ImageChops
    import pypdfium2 as pdfium

    ok_out, out_path, err_out = validate_output_any_path(output)
    if not ok_out or out_path is None:
        return _error_result("compare_pdf", err_out, "invalid_output")

    dpi = int(params.get("dpi", 100))
    raw_thresh = float(params.get("pixel_threshold", 12))
    if 0.0 < raw_thresh < 1.0:
        thresh_int = max(1, min(255, int(round(raw_thresh * 255.0))))
    else:
        thresh_int = max(0, min(255, int(round(raw_thresh))))
    generate_diff_pdf = bool(params.get("generate_diff_pdf", False))

    pdf_a, pdf_b = inputs[0], inputs[1]
    scale = dpi / 72.0

    doc_a = None
    doc_b = None
    page_diffs: list[dict[str, Any]] = []
    text_diff_pages: list[int] = []
    visual_diff_pages: list[int] = []
    diff_frames: list[tuple[int, Any]] = []

    try:
        doc_a = pdfium.PdfDocument(str(pdf_a))
        doc_b = pdfium.PdfDocument(str(pdf_b))
        count_a = len(doc_a)
        count_b = len(doc_b)
        max_pages = max(count_a, count_b)

        for i in range(max_pages):
            emit_progress(progress_cb, int(10 + 75 * ((i + 1) / max(1, max_pages))), f"Comparing page {i + 1}")
            page_num = i + 1
            if i >= count_a:
                text_diff_pages.append(page_num)
                visual_diff_pages.append(page_num)
                page_diffs.append(
                    {
                        "page": page_num,
                        "status": "added_in_b",
                        "text_changed": True,
                        "visual_changed": True,
                        "pixel_diff_ratio": 1.0,
                        "text_similarity": 0.0,
                    }
                )
                continue
            if i >= count_b:
                text_diff_pages.append(page_num)
                visual_diff_pages.append(page_num)
                page_diffs.append(
                    {
                        "page": page_num,
                        "status": "removed_in_b",
                        "text_changed": True,
                        "visual_changed": True,
                        "pixel_diff_ratio": 1.0,
                        "text_similarity": 0.0,
                    }
                )
                continue

            pa = doc_a[i]
            pb = doc_b[i]
            tpa = None
            tpb = None
            bma = None
            bmb = None
            try:
                tpa = pa.get_textpage()
                tpb = pb.get_textpage()
                text_a = tpa.get_text_range() or ""
                text_b = tpb.get_text_range() or ""
                text_changed = text_a != text_b
                similarity = round(difflib.SequenceMatcher(None, text_a, text_b).ratio(), 4)
                unified = list(
                    difflib.unified_diff(
                        text_a.splitlines(),
                        text_b.splitlines(),
                        fromfile=f"{pdf_a.name}:p{page_num}",
                        tofile=f"{pdf_b.name}:p{page_num}",
                        lineterm="",
                    )
                )[:120]

                bma = pa.render(scale=scale)
                bmb = pb.render(scale=scale)
                img_a = bma.to_pil().convert("RGB")
                img_b = bmb.to_pil().convert("RGB")

                size_mismatch = img_a.size != img_b.size
                if size_mismatch:
                    max_w = max(img_a.width, img_b.width)
                    max_h = max(img_a.height, img_b.height)
                    pad_a = Image.new("RGB", (max_w, max_h), (255, 255, 255))
                    pad_b = Image.new("RGB", (max_w, max_h), (255, 255, 255))
                    pad_a.paste(img_a, (0, 0))
                    pad_b.paste(img_b, (0, 0))
                    work_a, work_b = pad_a, pad_b
                else:
                    work_a, work_b = img_a, img_b

                diff_rgb = ImageChops.difference(work_a, work_b)
                diff_gray = diff_rgb.convert("L")
                hist = diff_gray.histogram()
                changed_px = sum(hist[thresh_int + 1 :]) if thresh_int < 255 else 0
                total_px = max(1, work_a.width * work_a.height)
                pixel_diff_ratio = round(changed_px / total_px, 6)
                visual_changed = size_mismatch or changed_px > 0

                bbox = None
                if changed_px > 0:
                    mask = diff_gray.point(lambda p: 255 if p > thresh_int else 0)
                    bbox = mask.getbbox()
                    if generate_diff_pdf or out_path.suffix.lower() == ".pdf":
                        highlight = Image.new("RGB", work_a.size, (220, 38, 38))
                        vis = Image.composite(highlight, work_a, mask)
                        diff_frames.append((page_num, vis))

                if text_changed:
                    text_diff_pages.append(page_num)
                if visual_changed:
                    visual_diff_pages.append(page_num)

                page_diffs.append(
                    {
                        "page": page_num,
                        "status": "modified" if (text_changed or visual_changed) else "identical",
                        "text_changed": text_changed,
                        "visual_changed": visual_changed,
                        "size_mismatch": size_mismatch,
                        "pixel_diff_ratio": pixel_diff_ratio,
                        "diff_bbox": list(bbox) if bbox is not None else None,
                        "text_similarity": similarity,
                        "text_diff_preview": unified,
                    }
                )
            finally:
                if bmb is not None:
                    bmb.close()
                if bma is not None:
                    bma.close()
                if tpb is not None:
                    tpb.close()
                if tpa is not None:
                    tpa.close()
                pb.close()
                pa.close()
    except Exception as exc:
        return _error_result("compare_pdf", f"Failed to compare PDFs: {exc}", "compare_error")
    finally:
        if doc_b is not None:
            doc_b.close()
        if doc_a is not None:
            doc_a.close()

    has_differences = (count_a != count_b) or bool(text_diff_pages) or bool(visual_diff_pages)
    report_payload = {
        "file_a": str(pdf_a),
        "file_b": str(pdf_b),
        "has_differences": has_differences,
        "page_count_a": count_a,
        "page_count_b": count_b,
        "text_diff_pages": text_diff_pages,
        "visual_diff_pages": visual_diff_pages,
        "page_diffs": page_diffs,
    }

    output_paths: list[Path] = []
    if out_path.suffix.lower() == ".pdf":
        json_sidecar = out_path.with_suffix(".json")
        if is_unsafe_reparse_or_system(json_sidecar):
            return _error_result("compare_pdf", "reparse_or_system_blocked", "invalid_output")
        _render_diff_pdf(diff_frames, out_path)
        output_paths.append(out_path)
        atomic_write_text(json_sidecar, json.dumps(report_payload, indent=2))
        output_paths.append(json_sidecar)
    else:
        json_target = (out_path / "compare_report.json") if (out_path.exists() and out_path.is_dir()) else out_path
        if is_unsafe_reparse_or_system(json_target):
            return _error_result("compare_pdf", "reparse_or_system_blocked", "invalid_output")
        pdf_target = json_target.with_suffix(".diff.pdf")
        if generate_diff_pdf and is_unsafe_reparse_or_system(pdf_target):
            return _error_result("compare_pdf", "reparse_or_system_blocked", "invalid_output")
        atomic_write_text(json_target, json.dumps(report_payload, indent=2))
        output_paths.append(json_target)
        if generate_diff_pdf:
            _render_diff_pdf(diff_frames, pdf_target)
            output_paths.append(pdf_target)

    emit_progress(progress_cb, 100, "Done")
    summary = (
        f"Differences detected across {len(set(text_diff_pages) | set(visual_diff_pages))} page(s)."
        if has_differences
        else f"Documents are identical ({count_a} page(s))."
    )
    return ToolResult(
        success=True,
        output_paths=output_paths,
        summary=summary,
        details=report_payload,
    )


def _handle_viewer_preferences(
    inputs: list[Path],
    output: Path | None,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    emit_progress(progress_cb, 25, "Validating viewer preferences")
    config_dict: dict[str, Any] = {
        "schema_version": 1,
        "default_zoom": float(params.get("default_zoom", 1.0)),
        "default_rail_mode": str(params.get("default_rail_mode", "thumbs")),
        "default_view_mode": str(params.get("default_view_mode", "read")),
        "theme_mode": str(params.get("theme_mode", "dark")),
        "continuous_scroll": bool(params.get("continuous_scroll", True)),
        "default_author": str(params.get("default_author") or _DEFAULT_ANON_AUTHOR),
        "page_layout": str(params.get("page_layout", "SinglePage")),
        "page_mode": str(params.get("page_mode", "UseNone")),
        "pdf_viewer_preferences": {
            "HideToolbar": bool(params.get("hide_toolbar", False)),
            "HideMenubar": bool(params.get("hide_menubar", False)),
            "FitWindow": bool(params.get("fit_window", False)),
            "CenterWindow": bool(params.get("center_window", False)),
            "DisplayDocTitle": bool(params.get("display_doc_title", True)),
        },
    }

    if output is not None:
        ok_out, out_path, err_out = validate_output_any_path(output)
        if not ok_out or out_path is None:
            return _error_result("viewer_preferences", err_out, "invalid_output")
    else:
        out_path = default_app_data_dir() / "viewer_preferences.json"

    output_paths: list[Path] = []
    if inputs:
        in_pdf = inputs[0]
        ok_in, err_in = validate_local_pdf_path(in_pdf, check_exists=True)
        if not ok_in:
            return _error_result("viewer_preferences", f"Invalid input PDF ({err_in})", "invalid_inputs")

        try:
            pdf = pikepdf.open(in_pdf)
        except pikepdf.PasswordError:
            return _error_result("viewer_preferences", "Encrypted PDF requires unlocking first", "encrypted_pdf")
        except (pikepdf.PdfError, OSError) as exc:
            return _error_result("viewer_preferences", f"Invalid PDF: {exc}", "invalid_pdf")

        try:
            vp = pdf.Root.get("/ViewerPreferences")
            if not isinstance(vp, pikepdf.Dictionary):
                vp = pikepdf.Dictionary()
                pdf.Root["/ViewerPreferences"] = vp
            for k, v in config_dict["pdf_viewer_preferences"].items():
                vp[f"/{k}"] = bool(v)
            pdf.Root["/PageLayout"] = pikepdf.Name(f"/{config_dict['page_layout']}")
            pdf.Root["/PageMode"] = pikepdf.Name(f"/{config_dict['page_mode']}")

            if out_path.suffix.lower() == ".pdf":
                temp_mgr = get_shared_temp_manager()
                with temp_mgr.task_scope(secure=False) as tid:
                    stage = make_sibling_stage_path(out_path)
                    temp_mgr.register_path(stage, task_id=tid)
                    pdf.save(stage)
                    pdf.close()
                    atomic_replace_file(stage, out_path)
                output_paths.append(out_path)
            else:
                json_target = (out_path / "viewer_preferences.json") if (out_path.exists() and out_path.is_dir()) else out_path
                if is_unsafe_reparse_or_system(json_target):
                    return _error_result("viewer_preferences", "reparse_or_system_blocked", "invalid_output")
                atomic_write_text(json_target, json.dumps(config_dict, indent=2))
                output_paths.append(json_target)
        finally:
            try:
                pdf.close()
            except Exception:
                pass
    else:
        json_target = (out_path / "viewer_preferences.json") if (out_path.exists() and out_path.is_dir()) else out_path
        if is_unsafe_reparse_or_system(json_target):
            return _error_result("viewer_preferences", "reparse_or_system_blocked", "invalid_output")
        atomic_write_text(json_target, json.dumps(config_dict, indent=2))
        output_paths.append(json_target)

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=output_paths,
        summary="Saved viewer preferences configuration.",
        details=config_dict,
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="compare_pdf",
        category="view_check",
        icon="compare",
        min_inputs=2,
        max_inputs=2,
        output_kind="any",
        handler=("pdf_lector.core.tools.compare", "_handle_compare_pdf"),
        params=[
            ToolParamSpec(
                name="dpi",
                kind="int",
                default=100,
                choices=(72, 100, 150, 200, 300),
                min_val=STANDARD_DPI_MIN,
                max_val=STANDARD_DPI_MAX,
                allow_custom=True,
                label="DPI",
            ),
            ToolParamSpec("pixel_threshold", "float", 12, min_val=0.0, max_val=255.0),
            ToolParamSpec("generate_diff_pdf", "bool", False),
        ],
    ),
    ToolDefinition(
        id="viewer_preferences",
        category="view_check",
        icon="palette",
        min_inputs=0,
        max_inputs=1,
        input_validator="none",
        output_kind="any",
        allow_empty_output=True,
        handler=("pdf_lector.core.tools.compare", "_handle_viewer_preferences"),
        params=[
            ToolParamSpec("default_zoom", "float", 1.0, min_val=0.25, max_val=5.0),
            ToolParamSpec("default_rail_mode", "choice", "thumbs", choices=("thumbs", "toc", "both")),
            ToolParamSpec("default_view_mode", "choice", "read", choices=("read", "split", "editor")),
            ToolParamSpec("theme_mode", "choice", "dark", choices=("dark", "light")),
            ToolParamSpec("continuous_scroll", "bool", True),
            ToolParamSpec("default_author", "str", _DEFAULT_ANON_AUTHOR),
            ToolParamSpec(
                "page_layout",
                "choice",
                "SinglePage",
                choices=("SinglePage", "OneColumn", "TwoColumnLeft", "TwoColumnRight", "TwoPageLeft", "TwoPageRight"),
            ),
            ToolParamSpec(
                "page_mode",
                "choice",
                "UseNone",
                choices=("UseNone", "UseOutlines", "UseThumbs", "FullScreen", "UseOC", "UseAttachments"),
            ),
            ToolParamSpec("hide_toolbar", "bool", False),
            ToolParamSpec("hide_menubar", "bool", False),
            ToolParamSpec("fit_window", "bool", False),
            ToolParamSpec("center_window", "bool", False),
            ToolParamSpec("display_doc_title", "bool", True),
        ],
    ),
)


def register_tools(registry: type[ToolRegistry]) -> None:
    """Register all 2 View & Check tools."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn)
