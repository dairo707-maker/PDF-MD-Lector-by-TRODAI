"""Create PDF tools (3 tools): job_application_pdf, capture_screen, import_scanner_camera."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capture_engine import acquire_device_to_pdf, capture_region_to_pdf, save_images_to_pdf
from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    dpi_param_spec,
    emit_progress,
    get_shared_temp_manager,
    make_sibling_stage_path,
    quality_param_spec,
    source_lang_param_spec,
    validate_output_pdf_path,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _handle_job_application_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("job_application_pdf", err_out, "invalid_output")

    dpi = int(params["dpi"])
    quality = int(params["quality"])
    title = str(params.get("title") or "Solicitud de Empleo").strip()
    applicant = str(params.get("applicant_name") or "").strip()
    add_bookmarks = bool(params.get("add_bookmarks", True))

    for p in inputs:
        ext = p.suffix.lower()
        if ext != ".pdf" and ext not in _IMAGE_EXTS:
            return _error_result("job_application_pdf", f"Unsupported dossier file type: {ext}", "unsupported_extension")

    import pikepdf

    temp_mgr = get_shared_temp_manager()
    opened_pdfs: list[Any] = []
    emit_progress(progress_cb, 10, "Assembling job application dossier")

    try:
        with temp_mgr.task_scope(secure=False) as tid:
            tmp_dir = temp_mgr.create_temp_dir(task_id=tid, secure=False)
            merged = pikepdf.Pdf.new()
            outline_items: list[tuple[str, int]] = []
            total = len(inputs)

            for idx, src in enumerate(inputs):
                start_page = len(merged.pages)
                if src.suffix.lower() == ".pdf":
                    pdf_in = pikepdf.open(src)
                    opened_pdfs.append(pdf_in)
                    merged.pages.extend(pdf_in.pages)
                else:
                    img_pdf_path = tmp_dir / f"img_{idx}.pdf"
                    save_images_to_pdf([src], img_pdf_path, dpi=dpi, quality=quality, title=src.stem)
                    pdf_img = pikepdf.open(img_pdf_path)
                    opened_pdfs.append(pdf_img)
                    merged.pages.extend(pdf_img.pages)

                if len(merged.pages) > start_page:
                    outline_items.append((src.stem, start_page))
                emit_progress(progress_cb, 15 + int(((idx + 1) / total) * 75), f"Added {src.name}")

            if len(merged.pages) == 0:
                return _error_result("job_application_pdf", "Dossier has zero pages", "empty_dossier")

            merged.docinfo["/Title"] = title
            if applicant:
                merged.docinfo["/Author"] = applicant

            if add_bookmarks and outline_items:
                with merged.open_outline() as outline:
                    for bm_title, page_idx in outline_items:
                        outline.root.append(pikepdf.OutlineItem(bm_title, page_idx))

            stage = make_sibling_stage_path(out_path, suffix=".pdf")
            temp_mgr.register_path(stage, task_id=tid, secure=False)
            merged.save(stage)
            for h in opened_pdfs:
                try:
                    h.close()
                except Exception:
                    pass
            opened_pdfs.clear()
            merged.close()
            atomic_replace_file(stage, out_path)
    except Exception as exc:
        for h in opened_pdfs:
            try:
                h.close()
            except Exception:
                pass
        return _error_result("job_application_pdf", f"Assembly failed: {exc}", "assembly_failed")

    emit_progress(progress_cb, 100, "Dossier assembled")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Created job application PDF ({len(inputs)} section(s))",
        details={"title": title, "dpi": dpi, "quality": quality, "sections": len(inputs)},
    )


def _handle_capture_screen(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    del inputs
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("capture_screen", err_out, "invalid_output")

    dpi = int(params["dpi"])
    quality = int(params["quality"])
    region = params.get("region")
    screen_index = int(params.get("screen_index", 0))
    screen_grab_fn = params.get("screen_grab_fn")

    emit_progress(progress_cb, 25, "Capturing screen region")
    try:
        saved = capture_region_to_pdf(
            out_path,
            region=region,
            dpi=dpi,
            quality=quality,
            screen_index=screen_index,
            screen_grab_fn=screen_grab_fn,
        )
    except Exception as exc:
        return _error_result("capture_screen", str(exc), "capture_failed")

    emit_progress(progress_cb, 100, "Screen capture saved")
    return ToolResult(
        success=True,
        output_paths=[saved],
        summary=f"Saved screen capture to {saved.name}",
        details={"dpi": dpi, "quality": quality, "region": region},
    )


def _handle_import_scanner_camera(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    del inputs
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("import_scanner_camera", err_out, "invalid_output")

    dpi = int(params["dpi"])
    quality = int(params["quality"])
    source_lang = str(params["source_lang"])
    source_type = str(params.get("source_type") or "auto")
    device_id = str(params.get("device_id") or "")

    emit_progress(progress_cb, 20, "Acquiring image from scanner/camera")
    try:
        saved, details = acquire_device_to_pdf(
            out_path,
            source_type=source_type,
            dpi=dpi,
            quality=quality,
            source_lang=source_lang,
            device_id=device_id,
            device_acquire_fn=params.get("device_acquire_fn"),
            wia_probe_fn=params.get("wia_probe_fn"),
            wia_acquire_fn=params.get("wia_acquire_fn"),
            camera_probe_fn=params.get("camera_probe_fn"),
            camera_capture_fn=params.get("camera_capture_fn"),
        )
    except Exception as exc:
        return _error_result("import_scanner_camera", str(exc), "device_acquire_failed")

    emit_progress(progress_cb, 100, "Scanner/camera import saved")
    return ToolResult(
        success=True,
        output_paths=[saved],
        summary=f"Imported {details.get('page_count', 1)} page(s) to {saved.name}",
        details=details,
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="job_application_pdf",
        category="create",
        params=[
            dpi_param_spec(150),
            quality_param_spec(85),
            ToolParamSpec("title", "str", "Solicitud de Empleo"),
            ToolParamSpec("applicant_name", "str", ""),
            ToolParamSpec("add_bookmarks", "bool", True),
        ],
        handler=_handle_job_application_pdf,
        icon="create",
        min_inputs=1,
        input_validator="none",
        output_kind="file",
    ),
    ToolDefinition(
        id="capture_screen",
        category="create",
        params=[
            dpi_param_spec(150),
            quality_param_spec(85),
            ToolParamSpec("region", "any", None),
            ToolParamSpec("screen_index", "int", 0, min_val=0, max_val=16),
        ],
        handler=_handle_capture_screen,
        icon="viewer",
        min_inputs=0,
        max_inputs=0,
        input_validator="none",
        output_kind="file",
    ),
    ToolDefinition(
        id="import_scanner_camera",
        category="create",
        params=[
            dpi_param_spec(200),
            quality_param_spec(85),
            source_lang_param_spec("auto"),
            ToolParamSpec("source_type", "choice", "auto", choices=("auto", "wia", "camera")),
            ToolParamSpec("device_id", "str", ""),
        ],
        handler=_handle_import_scanner_camera,
        icon="open",
        badge="WIA",
        min_inputs=0,
        max_inputs=0,
        input_validator="none",
        output_kind="file",
    ),
)


def register_tools(registry: type[ToolRegistry] = ToolRegistry) -> None:
    """Register all 3 Create tools in ToolRegistry."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn, overwrite=True)
