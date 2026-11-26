"""Organize and Edit PDF tools (20 tools) with strict path_guard and TempManager isolation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import io
import math
from pathlib import Path
import re
from typing import Any

from ..path_guard import validate_local_media_path, validate_local_pdf_path
from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    emit_progress,
    get_shared_temp_manager,
    is_unsafe_reparse_or_system,
    make_sibling_stage_path,
    validate_output_dir_path,
    validate_output_pdf_path,
)

_DEFAULT_ANON_AUTHOR = "An\u00f3nimo"

_PAGE_SIZE_PRESETS: dict[str, tuple[float, float]] = {
    "A4": (595.28, 841.89),
    "LETTER": (612.0, 792.0),
    "LEGAL": (612.0, 1008.0),
    "OFICIO": (612.0, 935.43),
    "A3": (841.89, 1190.55),
    "A5": (419.53, 595.28),
    "TABLOID": (792.0, 1224.0),
}

_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _parse_hex_color(raw: Any, default: tuple[float, float, float] = (0.04, 0.06, 0.11)) -> tuple[float, float, float]:
    if not isinstance(raw, str) or not _HEX_COLOR_RE.match(raw.strip()):
        return default
    h = raw.strip().lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _open_pikepdf(path: Path, password: str = ""):
    import pikepdf

    pw = str(password or "")
    if pw:
        return pikepdf.open(path, password=pw)
    return pikepdf.open(path)


def _save_pikepdf_atomic(
    pdf_obj: Any,
    out_path: Path,
    *,
    close_handles: Sequence[Any] = (),
    linearize: bool = False,
    secure: bool = False,
) -> None:
    temp_mgr = get_shared_temp_manager()
    with temp_mgr.task_scope(secure=secure) as tid:
        stage = make_sibling_stage_path(out_path)
        temp_mgr.register_path(stage, task_id=tid, secure=secure)
        pdf_obj.save(stage, linearize=linearize)
        for handle in close_handles:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        try:
            pdf_obj.close()
        except Exception:
            pass
        atomic_replace_file(stage, out_path)


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _parse_page_list(
    spec: Any,
    total_pages: int,
    *,
    default_all: bool = False,
    allow_duplicates: bool = False,
) -> list[int]:
    if total_pages <= 0:
        raise ValueError("Document has zero pages")

    if spec is None or (isinstance(spec, str) and spec.strip().lower() in ("", "all")):
        if default_all:
            return list(range(total_pages))
        raise ValueError("Empty page specification")

    indices: list[int] = []
    if isinstance(spec, (list, tuple)):
        if not spec:
            if default_all:
                return list(range(total_pages))
            raise ValueError("Empty page list")
        for item in spec:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"Invalid page number: {item!r}")
            if item < 1 or item > total_pages:
                raise ValueError(f"Page {item} out of range 1..{total_pages}")
            indices.append(item - 1)
    elif isinstance(spec, str):
        tokens = [tok.strip() for tok in spec.split(",") if tok.strip()]
        if not tokens:
            raise ValueError("Empty page range string")
        for tok in tokens:
            if "-" in tok:
                parts = tok.split("-")
                if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                    raise ValueError(f"Invalid range token: {tok!r}")
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                except ValueError as exc:
                    raise ValueError(f"Non-numeric range token: {tok!r}") from exc
                if start < 1 or end > total_pages or start > end:
                    raise ValueError(f"Invalid page range {tok!r} for {total_pages} pages")
                for p in range(start, end + 1):
                    indices.append(p - 1)
            else:
                try:
                    num = int(tok)
                except ValueError as exc:
                    raise ValueError(f"Invalid page number {tok!r}") from exc
                if num < 1 or num > total_pages:
                    raise ValueError(f"Page {num} out of range 1..{total_pages}")
                indices.append(num - 1)
    else:
        raise ValueError(f"Unsupported page spec type: {type(spec).__name__}")

    if allow_duplicates:
        return indices
    seen: set[int] = set()
    deduped: list[int] = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            deduped.append(idx)
    return deduped


def _parse_split_ranges(ranges: Any, total_pages: int) -> list[list[int]]:
    if isinstance(ranges, str):
        segments = [seg.strip() for seg in ranges.split(",")]
        if not segments or any(not seg for seg in segments):
            raise ValueError("Empty segment in split ranges")
        groups: list[list[int]] = []
        for seg in segments:
            groups.append(_parse_page_list(seg, total_pages, default_all=False, allow_duplicates=False))
        return groups
    if isinstance(ranges, (list, tuple)):
        groups = []
        for item in ranges:
            groups.append(_parse_page_list(item, total_pages, default_all=False, allow_duplicates=False))
        if not groups:
            raise ValueError("Empty split ranges list")
        return groups
    raise ValueError("Unsupported split ranges format")


def _resolve_outline_page_idx(src_pdf: Any, item: Any, page_obj_map: dict[tuple[int, int], int]) -> int | None:
    try:
        dest = getattr(item, "destination", None)
        if dest is not None and len(dest) > 0:
            target = dest[0]
            og = getattr(target, "objgen", None)
            if og in page_obj_map:
                return page_obj_map[og]
    except Exception:
        pass
    return None


def _copy_outline_items(
    src_pdf: Any,
    items: list[Any],
    offset: int,
    page_obj_map: dict[tuple[int, int], int],
    outline_cls: Any,
) -> list[Any]:
    copied: list[Any] = []
    for item in items:
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            continue
        local_idx = _resolve_outline_page_idx(src_pdf, item, page_obj_map)
        target_idx = (offset + local_idx) if local_idx is not None else offset
        new_item = outline_cls(title, target_idx)
        children = getattr(item, "children", None)
        if children:
            new_item.children.extend(
                _copy_outline_items(src_pdf, list(children), offset, page_obj_map, outline_cls)
            )
        copied.append(new_item)
    return copied


def _handle_merge_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("merge_pdf", err_out, "invalid_output")

    preserve_bookmarks = bool(params.get("preserve_bookmarks", True))
    passwords = params.get("passwords")
    default_pw = str(params.get("password") or "")

    pdf_out = pikepdf.Pdf.new()
    collected_outlines: list[Any] = []
    opened_sources: list[Any] = []
    try:
        total_inputs = len(inputs)
        for idx, in_path in enumerate(inputs):
            emit_progress(progress_cb, int(10 + 70 * (idx / max(1, total_inputs))), f"Merging {in_path.name}")
            pw = default_pw
            if isinstance(passwords, (list, tuple)) and idx < len(passwords):
                pw = str(passwords[idx] or "")
            elif isinstance(passwords, dict):
                pw = str(passwords.get(str(in_path), passwords.get(in_path.name, default_pw)) or "")

            try:
                src = _open_pikepdf(in_path, pw)
            except pikepdf.PasswordError:
                return _error_result("merge_pdf", f"Encrypted PDF requires password: {in_path.name}", "encrypted_pdf")
            except (pikepdf.PdfError, OSError) as exc:
                return _error_result("merge_pdf", f"Invalid PDF {in_path.name}: {exc}", "invalid_pdf")

            opened_sources.append(src)
            if len(src.pages) == 0:
                return _error_result("merge_pdf", f"PDF has zero pages: {in_path.name}", "empty_pdf")

            offset = len(pdf_out.pages)
            page_obj_map: dict[tuple[int, int], int] = {
                getattr(p.obj, "objgen", (0, 0)): pidx for pidx, p in enumerate(src.pages)
            }
            pdf_out.pages.extend(src.pages)

            if preserve_bookmarks:
                try:
                    with src.open_outline() as ol:
                        if ol.root:
                            collected_outlines.extend(
                                _copy_outline_items(src, list(ol.root), offset, page_obj_map, pikepdf.OutlineItem)
                            )
                except Exception:
                    pass

        if preserve_bookmarks and collected_outlines:
            with pdf_out.open_outline() as out_ol:
                out_ol.root.extend(collected_outlines)

        emit_progress(progress_cb, 90, "Saving merged PDF")
        page_count = len(pdf_out.pages)
        _save_pikepdf_atomic(pdf_out, out_path, close_handles=opened_sources)
    finally:
        for src in opened_sources:
            try:
                src.close()
            except Exception:
                pass
        try:
            pdf_out.close()
        except Exception:
            pass

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Merged {len(inputs)} PDF(s) into {page_count} page(s).",
        details={"input_count": len(inputs), "page_count": page_count},
    )


def _handle_split_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_dir, out_dir, err_dir = validate_output_dir_path(output)
    if not ok_dir or out_dir is None:
        return _error_result("split_pdf", err_dir, "invalid_output_dir")

    in_path = inputs[0]
    pw = str(params.get("password") or "")
    ranges_raw = params.get("ranges")
    mode = str(params.get("mode") or "ranges").strip().lower()
    chunk_size = int(params.get("chunk_size", 1))

    try:
        src = _open_pikepdf(in_path, pw)
    except pikepdf.PasswordError:
        return _error_result("split_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("split_pdf", f"Corrupt or unreadable PDF: {exc}", "invalid_pdf")

    output_paths: list[Path] = []
    try:
        total_pages = len(src.pages)
        if total_pages == 0:
            return _error_result("split_pdf", "Input PDF has zero pages", "empty_pdf")

        has_ranges = False
        if ranges_raw is not None:
            if isinstance(ranges_raw, (list, tuple)):
                has_ranges = len(ranges_raw) > 0
            elif isinstance(ranges_raw, str):
                has_ranges = ranges_raw.strip() != ""
            else:
                has_ranges = str(ranges_raw).strip() != ""

        if has_ranges:
            groups = _parse_split_ranges(ranges_raw, total_pages)
        elif mode == "single_pages":
            groups = [[i] for i in range(total_pages)]
        elif mode in ("every_n", "ranges"):
            if chunk_size < 1:
                return _error_result("split_pdf", "chunk_size must be >= 1", "invalid_params")
            groups = [list(range(i, min(i + chunk_size, total_pages))) for i in range(0, total_pages, chunk_size)]
        else:
            return _error_result("split_pdf", f"Unknown split mode: {mode}", "invalid_params")

        out_dir.mkdir(parents=True, exist_ok=True)
        for idx, page_indices in enumerate(groups, start=1):
            emit_progress(progress_cb, int(10 + 85 * (idx / max(1, len(groups)))), f"Writing split part {idx}")
            part_path = out_dir / f"{in_path.stem}_part{idx:03d}.pdf"
            if is_unsafe_reparse_or_system(part_path):
                return _error_result("split_pdf", "reparse_or_system_blocked", "invalid_output_dir")
            sub = pikepdf.Pdf.new()
            try:
                for pidx in page_indices:
                    sub.pages.append(src.pages[pidx])
                _save_pikepdf_atomic(sub, part_path)
            finally:
                sub.close()
            output_paths.append(part_path)
    except ValueError as exc:
        return _error_result("split_pdf", str(exc), "invalid_ranges")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=output_paths,
        summary=f"Split {in_path.name} into {len(output_paths)} PDF file(s).",
        details={"parts": len(output_paths)},
    )


def _handle_rotate_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("rotate_pdf", err_out, "invalid_output")

    angle = int(params.get("angle", 90))
    if angle % 90 != 0:
        return _error_result("rotate_pdf", f"Angle must be a multiple of 90, got {angle}", "invalid_angle")
    relative = bool(params.get("relative", True))
    pw = str(params.get("password") or "")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("rotate_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("rotate_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        indices = _parse_page_list(params.get("pages"), total, default_all=True, allow_duplicates=False)
        emit_progress(progress_cb, 40, "Rotating pages")
        for idx in indices:
            src.pages[idx].rotate(angle, relative=relative)
        emit_progress(progress_cb, 85, "Saving PDF")
        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("rotate_pdf", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Rotated {len(indices)} page(s) by {angle} degrees.",
        details={"rotated_pages": [i + 1 for i in indices], "angle": angle},
    )


def _handle_delete_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("delete_pages", err_out, "invalid_output")

    pw = str(params.get("password") or "")
    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("delete_pages", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("delete_pages", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        indices = _parse_page_list(params.get("pages"), total, default_all=False, allow_duplicates=False)
        to_delete = set(indices)
        if len(to_delete) >= total:
            return _error_result("delete_pages", "Cannot delete all pages from a PDF", "cannot_delete_all_pages")
        emit_progress(progress_cb, 50, "Deleting pages")
        for idx in sorted(to_delete, reverse=True):
            del src.pages[idx]
        remaining = len(src.pages)
        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("delete_pages", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Deleted {len(to_delete)} page(s); {remaining} page(s) remaining.",
        details={"deleted_count": len(to_delete), "remaining_pages": remaining},
    )


def _handle_delete_blank_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    import pypdfium2 as pdfium

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("delete_blank_pages", err_out, "invalid_output")

    threshold = float(params.get("threshold", 0.0005))
    white_level = int(params.get("white_level", 250))
    if threshold < 0.0 or threshold > 0.5:
        return _error_result("delete_blank_pages", "threshold must be in [0.0, 0.5]", "invalid_params")
    pw = str(params.get("password") or "")

    in_path = inputs[0]
    blank_indices: list[int] = []
    doc = None
    try:
        doc = pdfium.PdfDocument(str(in_path), password=pw or None)
        total = len(doc)
        if total == 0:
            return _error_result("delete_blank_pages", "PDF has zero pages", "empty_pdf")
        for idx in range(total):
            emit_progress(progress_cb, int(10 + 60 * ((idx + 1) / total)), f"Inspecting page {idx + 1}")
            page = doc[idx]
            tp = None
            bitmap = None
            try:
                tp = page.get_textpage()
                text = (tp.get_text_range() or "").strip()
                if text:
                    continue
                bitmap = page.render(scale=0.5)
                pil_img = bitmap.to_pil().convert("L")
                hist = pil_img.histogram()
                non_white = sum(hist[:white_level])
                total_px = max(1, pil_img.width * pil_img.height)
                ratio = non_white / total_px
                if ratio <= threshold:
                    blank_indices.append(idx)
            finally:
                if bitmap is not None:
                    bitmap.close()
                if tp is not None:
                    tp.close()
                page.close()
    except Exception as exc:
        return _error_result("delete_blank_pages", f"Failed to inspect PDF pages: {exc}", "invalid_pdf")
    finally:
        if doc is not None:
            doc.close()

    if len(blank_indices) >= total:
        return _error_result("delete_blank_pages", "All pages in PDF are blank", "all_pages_blank")

    try:
        src = _open_pikepdf(in_path, pw)
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("delete_blank_pages", f"Failed to open PDF: {exc}", "invalid_pdf")

    try:
        for idx in sorted(blank_indices, reverse=True):
            del src.pages[idx]
        remaining = len(src.pages)
        emit_progress(progress_cb, 90, "Saving PDF")
        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Removed {len(blank_indices)} blank page(s); {remaining} page(s) retained.",
        details={"removed_pages": [i + 1 for i in blank_indices], "remaining_pages": remaining},
    )


def _handle_extract_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("extract_pages", err_out, "invalid_output")

    pw = str(params.get("password") or "")
    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("extract_pages", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("extract_pages", f"Invalid PDF: {exc}", "invalid_pdf")

    out_pdf = pikepdf.Pdf.new()
    try:
        indices = _parse_page_list(params.get("pages"), len(src.pages), default_all=False, allow_duplicates=False)
        emit_progress(progress_cb, 50, "Extracting pages")
        for idx in indices:
            out_pdf.pages.append(src.pages[idx])
        _save_pikepdf_atomic(out_pdf, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("extract_pages", str(exc), "invalid_pages")
    finally:
        out_pdf.close()
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Extracted {len(indices)} page(s).",
        details={"extracted_pages": [i + 1 for i in indices]},
    )


def _handle_reorder_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("reorder_pages", err_out, "invalid_output")

    pw = str(params.get("password") or "")
    strict_perm = bool(params.get("strict_permutation", True))
    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("reorder_pages", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("reorder_pages", f"Invalid PDF: {exc}", "invalid_pdf")

    out_pdf = pikepdf.Pdf.new()
    try:
        total = len(src.pages)
        indices = _parse_page_list(
            params.get("order"),
            total,
            default_all=False,
            allow_duplicates=True,
        )
        if strict_perm:
            if len(indices) != total or len(set(indices)) != total:
                return _error_result(
                    "reorder_pages",
                    f"Order must be a valid permutation of pages 1..{total}",
                    "invalid_permutation",
                )
        emit_progress(progress_cb, 50, "Reordering pages")
        for idx in indices:
            out_pdf.pages.append(src.pages[idx])
        _save_pikepdf_atomic(out_pdf, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("reorder_pages", str(exc), "invalid_order")
    finally:
        out_pdf.close()
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Reordered {len(indices)} page(s).",
        details={"order": [i + 1 for i in indices]},
    )


def _collect_page_images(container: Any, visited_forms: set[tuple[int, int]]) -> list[Any]:
    import pikepdf

    found: list[Any] = []
    res = container.get("/Resources")
    if not isinstance(res, pikepdf.Dictionary):
        return found
    xobjs = res.get("/XObject")
    if not isinstance(xobjs, pikepdf.Dictionary):
        return found
    for _, raw_obj in xobjs.items():
        if not isinstance(raw_obj, pikepdf.Stream):
            continue
        subtype = str(raw_obj.get("/Subtype", ""))
        if subtype == "/Image":
            found.append(raw_obj)
        elif subtype == "/Form":
            og = getattr(raw_obj, "objgen", (0, 0))
            if og != (0, 0):
                if og in visited_forms:
                    continue
                visited_forms.add(og)
            found.extend(_collect_page_images(raw_obj, visited_forms))
    return found


def _handle_extract_images(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_dir, out_dir, err_dir = validate_output_dir_path(output)
    if not ok_dir or out_dir is None:
        return _error_result("extract_images", err_dir, "invalid_output_dir")

    in_path = inputs[0]
    pw = str(params.get("password") or "")
    deduplicate = bool(params.get("deduplicate", True))

    try:
        src = _open_pikepdf(in_path, pw)
    except pikepdf.PasswordError:
        return _error_result("extract_images", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("extract_images", f"Invalid PDF: {exc}", "invalid_pdf")

    output_paths: list[Path] = []
    temp_mgr = get_shared_temp_manager()
    try:
        total = len(src.pages)
        page_indices = _parse_page_list(params.get("pages"), total, default_all=True, allow_duplicates=False)
        out_dir.mkdir(parents=True, exist_ok=True)
        seen_objgen: set[tuple[int, int]] = set()
        img_counter = 0

        with temp_mgr.task_scope(secure=False) as tid:
            tmp_dir = temp_mgr.create_temp_dir(prefix="ximg_", task_id=tid)
            for p_pos, pidx in enumerate(page_indices):
                emit_progress(
                    progress_cb,
                    int(10 + 85 * ((p_pos + 1) / max(1, len(page_indices)))),
                    f"Scanning page {pidx + 1}",
                )
                page = src.pages[pidx]
                raw_images: list[Any] = []
                if hasattr(page, "get_images"):
                    try:
                        raw_images = [val for _, val in page.get_images(recursive=True).items()]
                    except Exception:
                        raw_images = _collect_page_images(page.obj, set())
                else:
                    raw_images = _collect_page_images(page.obj, set())

                for raw_obj in raw_images:
                    og = getattr(raw_obj, "objgen", (0, 0))
                    if deduplicate and og != (0, 0):
                        if og in seen_objgen:
                            continue
                        seen_objgen.add(og)

                    img_counter += 1
                    pimg = pikepdf.PdfImage(raw_obj)
                    prefix = tmp_dir / f"p{pidx + 1}_img{img_counter}"
                    extracted_file: Path | None = None
                    try:
                        ext_name = pimg.extract_to(fileprefix=str(prefix))
                        cand = Path(ext_name)
                        if cand.exists():
                            extracted_file = cand
                    except Exception:
                        extracted_file = None

                    if extracted_file is None:
                        try:
                            pil_im = pimg.as_pil_image()
                            fallback_png = tmp_dir / f"p{pidx + 1}_img{img_counter}.png"
                            pil_im.save(fallback_png, format="PNG")
                            extracted_file = fallback_png
                        except Exception:
                            continue

                    ext = extracted_file.suffix or ".png"
                    final_path = out_dir / f"page_{pidx + 1:03d}_img_{img_counter:03d}{ext}"
                    if is_unsafe_reparse_or_system(final_path):
                        return _error_result("extract_images", "reparse_or_system_blocked", "invalid_output_dir")
                    stage = make_sibling_stage_path(final_path)
                    temp_mgr.register_path(stage, task_id=tid)
                    stage.write_bytes(extracted_file.read_bytes())
                    atomic_replace_file(stage, final_path)
                    output_paths.append(final_path)
    except ValueError as exc:
        return _error_result("extract_images", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=output_paths,
        summary=f"Extracted {len(output_paths)} image(s)." if output_paths else "No embedded images found.",
        details={"count": len(output_paths)},
    )


_WINDOWS_RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def _sanitize_attachment_filename(raw_name: str, index: int) -> str:
    cleaned = str(raw_name or "").replace("\\", "/").replace("\x00", "").strip()
    base = Path(cleaned).name.strip()
    if not base or base in (".", "..") or ":" in base:
        return f"attachment_{index:03d}.bin"
    # Remove ASCII control chars, Windows-disallowed filename chars, and trailing dots/spaces
    safe = "".join("_" if ch in '<>"|?*' else ch for ch in base if ord(ch) >= 0x20).rstrip(" .")
    if not safe or safe in (".", ".."):
        return f"attachment_{index:03d}.bin"
    stem_head = safe.split(".", 1)[0].strip().upper()
    if stem_head in _WINDOWS_RESERVED_DEVICE_NAMES:
        safe = f"_{safe}"
    return safe


def _handle_extract_attachments(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_dir, out_dir, err_dir = validate_output_dir_path(output)
    if not ok_dir or out_dir is None:
        return _error_result("extract_attachments", err_dir, "invalid_output_dir")

    in_path = inputs[0]
    pw = str(params.get("password") or "")
    overwrite = bool(params.get("overwrite", True))

    try:
        src = _open_pikepdf(in_path, pw)
    except pikepdf.PasswordError:
        return _error_result("extract_attachments", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("extract_attachments", f"Invalid PDF: {exc}", "invalid_pdf")

    output_paths: list[Path] = []
    temp_mgr = get_shared_temp_manager()
    try:
        attachments = list(src.attachments.items())
        if not attachments:
            emit_progress(progress_cb, 100, "No attachments")
            return ToolResult(
                success=True,
                output_paths=[],
                summary="No embedded attachments found.",
                details={"count": 0},
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        out_root = out_dir.resolve()
        used_names: set[str] = set()
        with temp_mgr.task_scope(secure=False) as tid:
            for idx, (key, spec) in enumerate(attachments, start=1):
                emit_progress(progress_cb, int(10 + 85 * (idx / len(attachments))), f"Extracting attachment {idx}")
                raw_name = getattr(spec, "filename", None) or key
                safe_name = _sanitize_attachment_filename(str(raw_name), idx)

                target = (out_root / safe_name).resolve()
                if not target.is_relative_to(out_root) or target == out_root:
                    safe_name = f"attachment_{idx:03d}.bin"
                    target = (out_root / safe_name).resolve()

                if safe_name.lower() in used_names or (not overwrite and target.exists()):
                    stem = Path(safe_name).stem
                    suf = Path(safe_name).suffix
                    counter = 1
                    while True:
                        cand_name = f"{stem}_{counter}{suf}"
                        cand_path = (out_root / cand_name).resolve()
                        if cand_name.lower() not in used_names and (overwrite or not cand_path.exists()):
                            safe_name = cand_name
                            target = cand_path
                            break
                        counter += 1
                used_names.add(safe_name.lower())
                if is_unsafe_reparse_or_system(target):
                    return _error_result("extract_attachments", "reparse_or_system_blocked", "invalid_output_dir")

                embedded_file = spec.get_file()
                if embedded_file is None:
                    continue
                payload = embedded_file.read_bytes()
                stage = make_sibling_stage_path(target)
                temp_mgr.register_path(stage, task_id=tid)
                stage.write_bytes(payload)
                atomic_replace_file(stage, target)
                output_paths.append(target)
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=output_paths,
        summary=f"Extracted {len(output_paths)} attachment(s).",
        details={"count": len(output_paths)},
    )


def _handle_nup_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("nup_pages", err_out, "invalid_output")

    nup = int(params.get("nup", 4))
    grid_map = {2: (1, 2), 4: (2, 2), 6: (2, 3), 9: (3, 3)}
    if nup not in grid_map:
        return _error_result("nup_pages", f"nup must be one of (2, 4, 6, 9), got {nup}", "invalid_nup")

    margin = float(params.get("margin", 12.0))
    gap = float(params.get("gap", 6.0))
    if margin < 0.0 or gap < 0.0:
        return _error_result("nup_pages", "margin and gap must be >= 0", "invalid_params")

    pw = str(params.get("password") or "")
    preset = str(params.get("page_size") or "Letter").strip().upper()

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("nup_pages", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("nup_pages", f"Invalid PDF: {exc}", "invalid_pdf")

    out_pdf = pikepdf.Pdf.new()
    try:
        total = len(src.pages)
        if total == 0:
            return _error_result("nup_pages", "PDF has zero pages", "empty_pdf")

        if preset == "SAME":
            mb = [float(v) for v in src.pages[0].MediaBox]
            sheet_w = max(72.0, mb[2] - mb[0])
            sheet_h = max(72.0, mb[3] - mb[1])
        elif preset in _PAGE_SIZE_PRESETS:
            sheet_w, sheet_h = _PAGE_SIZE_PRESETS[preset]
        else:
            return _error_result("nup_pages", f"Unknown page_size preset: {preset}", "invalid_params")

        cols, rows = grid_map[nup]
        usable_w = sheet_w - 2.0 * margin - gap * (cols - 1)
        usable_h = sheet_h - 2.0 * margin - gap * (rows - 1)
        if usable_w <= 10.0 or usable_h <= 10.0:
            return _error_result("nup_pages", "Margin/gap leaves insufficient cell area", "invalid_params")
        cell_w = usable_w / cols
        cell_h = usable_h / rows

        for batch_start in range(0, total, nup):
            emit_progress(progress_cb, int(10 + 75 * ((batch_start + 1) / total)), "Composing N-up sheet")
            sheet = out_pdf.add_blank_page(page_size=(sheet_w, sheet_h))
            for slot in range(nup):
                src_idx = batch_start + slot
                if src_idx >= total:
                    break
                col = slot % cols
                row = slot // cols
                x0 = margin + col * (cell_w + gap)
                y1 = sheet_h - margin - row * (cell_h + gap)
                y0 = y1 - cell_h
                x1 = x0 + cell_w
                sheet.add_overlay(
                    src.pages[src_idx],
                    pikepdf.Rectangle(x0, y0, x1, y1),
                    shrink=True,
                    expand=True,
                )

        sheet_count = len(out_pdf.pages)
        emit_progress(progress_cb, 90, "Saving N-up PDF")
        _save_pikepdf_atomic(out_pdf, out_path, close_handles=(src,))
    finally:
        out_pdf.close()
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Created {sheet_count} {nup}-up sheet(s) from {total} page(s).",
        details={"nup": nup, "input_pages": total, "output_pages": sheet_count},
    )


def _build_outline_tree(items: list[dict[str, Any]], total_pages: int, outline_cls: Any) -> list[Any]:
    result: list[Any] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("Each bookmark entry must be a dict")
        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValueError("Bookmark title cannot be empty")
        if "page" in raw and raw["page"] is not None:
            p = raw["page"]
            if isinstance(p, bool) or not isinstance(p, int):
                raise ValueError(f"Invalid bookmark page: {p!r}")
            if p < 1 or p > total_pages:
                raise ValueError(f"Bookmark page {p} out of range 1..{total_pages}")
            page_idx = p - 1
        elif "page_index" in raw and raw["page_index"] is not None:
            pi = raw["page_index"]
            if isinstance(pi, bool) or not isinstance(pi, int):
                raise ValueError(f"Invalid bookmark page_index: {pi!r}")
            if pi < 0 or pi >= total_pages:
                raise ValueError(f"Bookmark page_index {pi} out of range 0..{total_pages - 1}")
            page_idx = pi
        else:
            raise ValueError("Bookmark entry must specify 'page' (1-based) or 'page_index' (0-based)")

        node = outline_cls(title, page_idx)
        children = raw.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise ValueError("Bookmark children must be a list")
            node.children.extend(_build_outline_tree(children, total_pages, outline_cls))
        result.append(node)
    return result


def _handle_edit_bookmarks(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("edit_bookmarks", err_out, "invalid_output")

    bookmarks = params.get("bookmarks")
    if not isinstance(bookmarks, list):
        return _error_result("edit_bookmarks", "bookmarks must be a list", "invalid_params")
    mode = str(params.get("mode") or "replace").strip().lower()
    if mode not in ("replace", "append"):
        return _error_result("edit_bookmarks", "mode must be 'replace' or 'append'", "invalid_params")
    pw = str(params.get("password") or "")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("edit_bookmarks", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("edit_bookmarks", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        nodes = _build_outline_tree(bookmarks, total, pikepdf.OutlineItem)
        emit_progress(progress_cb, 50, "Updating bookmarks")
        with src.open_outline() as ol:
            if mode == "replace":
                ol.root.clear()
            ol.root.extend(nodes)
        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("edit_bookmarks", str(exc), "invalid_bookmarks")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Updated PDF bookmarks ({len(nodes)} top-level item(s)).",
        details={"bookmark_count": len(nodes), "mode": mode},
    )


def _handle_edit_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    from reportlab.pdfgen import canvas

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("edit_pdf", err_out, "invalid_output")

    pw = str(params.get("password") or "")
    edits_raw = params.get("edits")
    if edits_raw is None:
        text = str(params.get("text") or "")
        if not text:
            return _error_result("edit_pdf", "No text or edits provided", "empty_edits")
        raw_top_page = params.get("page", 1)
        if isinstance(raw_top_page, bool) or not isinstance(raw_top_page, int):
            return _error_result("edit_pdf", f"Invalid page: {raw_top_page!r}", "invalid_pages")
        edits_list = [
            {
                "page": raw_top_page,
                "text": text,
                "x": float(params.get("x", 72.0)),
                "y": float(params.get("y", 700.0)),
                "font_size": float(params.get("font_size", 12.0)),
                "color": str(params.get("color", "#0A0F1D")),
                "font_name": str(params.get("font_name", "Helvetica")),
            }
        ]
    else:
        if not isinstance(edits_raw, list) or not edits_raw:
            return _error_result("edit_pdf", "edits must be a non-empty list", "empty_edits")
        edits_list = list(edits_raw)

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("edit_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("edit_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        by_page: dict[int, list[dict[str, Any]]] = {}
        for item in edits_list:
            if not isinstance(item, dict):
                return _error_result("edit_pdf", "Each edit must be a dict", "invalid_params")
            if "page_index" in item and item["page_index"] is not None:
                raw_pi = item["page_index"]
                if isinstance(raw_pi, bool) or not isinstance(raw_pi, int):
                    return _error_result("edit_pdf", f"Invalid page_index: {raw_pi!r}", "invalid_pages")
                pidx = raw_pi
            elif "page" in item and item["page"] is not None:
                raw_p = item["page"]
                if isinstance(raw_p, bool) or not isinstance(raw_p, int):
                    return _error_result("edit_pdf", f"Invalid page: {raw_p!r}", "invalid_pages")
                pidx = raw_p - 1
            else:
                pidx = 0
            if pidx < 0 or pidx >= total:
                return _error_result("edit_pdf", f"Page index {pidx} out of range", "invalid_pages")
            fsize = float(item.get("font_size", 12.0))
            if fsize <= 0.0:
                return _error_result("edit_pdf", "font_size must be > 0", "invalid_params")
            img_path = item.get("image_path")
            if img_path:
                ok_img, reason_img = validate_local_media_path(img_path, check_exists=True)
                if not ok_img:
                    return _error_result("edit_pdf", f"Invalid edit image ({reason_img})", "invalid_image")
                img_p = Path(str(img_path).strip())
                if is_unsafe_reparse_or_system(img_p) or is_unsafe_reparse_or_system(img_p.resolve()):
                    return _error_result("edit_pdf", "Invalid edit image (reparse_or_system_blocked)", "invalid_image")
            by_page.setdefault(pidx, []).append(item)

        for idx_done, (pidx, page_edits) in enumerate(by_page.items(), start=1):
            emit_progress(progress_cb, int(15 + 70 * (idx_done / len(by_page))), f"Editing page {pidx + 1}")
            page = src.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            w = max(1.0, mb[2] - mb[0])
            h = max(1.0, mb[3] - mb[1])

            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            for ed in page_edits:
                r, g, b = _parse_hex_color(ed.get("color", "#0A0F1D"))
                c.setFillColorRGB(r, g, b)
                c.setStrokeColorRGB(r, g, b)
                if ed.get("rect"):
                    rx0, ry0, rx1, ry1 = [float(v) for v in ed["rect"]]
                    c.rect(rx0, ry0, rx1 - rx0, ry1 - ry0, stroke=1, fill=int(bool(ed.get("fill", False))))
                if ed.get("image_path"):
                    ix = float(ed.get("x", 72.0))
                    iy = float(ed.get("y", 72.0))
                    iw = float(ed.get("width", 120.0))
                    ih = float(ed.get("height", 120.0))
                    c.drawImage(str(ed["image_path"]), ix, iy, width=iw, height=ih, mask="auto")
                txt = str(ed.get("text") or "")
                if txt:
                    font_name = str(ed.get("font_name") or "Helvetica")
                    fsize = float(ed.get("font_size", 12.0))
                    try:
                        c.setFont(font_name, fsize)
                    except Exception:
                        c.setFont("Helvetica", fsize)
                    c.drawString(float(ed.get("x", 72.0)), float(ed.get("y", 700.0)), txt)
            c.showPage()
            c.save()
            buf.seek(0)
            with pikepdf.open(buf) as ov_pdf:
                page.add_overlay(ov_pdf.pages[0], pikepdf.Rectangle(0, 0, w, h))

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Applied {len(edits_list)} edit(s) across {len(by_page)} page(s).",
        details={"edits_applied": len(edits_list), "pages_modified": len(by_page)},
    )


def _handle_add_watermark(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    from reportlab.pdfgen import canvas

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("add_watermark", err_out, "invalid_output")

    text = str(params.get("text") or "").strip()
    image_path = params.get("image_path")
    if not text and not image_path:
        return _error_result("add_watermark", "Watermark requires text or image_path", "empty_watermark")

    opacity = float(params.get("opacity", 0.25))
    if opacity <= 0.0 or opacity > 1.0:
        return _error_result("add_watermark", "opacity must be in (0.0, 1.0]", "invalid_params")
    angle = float(params.get("angle", 35.0))
    font_size = float(params.get("font_size", 42.0))
    if font_size <= 0.0:
        return _error_result("add_watermark", "font_size must be > 0", "invalid_params")
    color = _parse_hex_color(params.get("color", "#DC2626"), default=(0.86, 0.15, 0.15))
    pw = str(params.get("password") or "")

    if image_path:
        ok_img, reason_img = validate_local_media_path(image_path, check_exists=True)
        if not ok_img:
            return _error_result("add_watermark", f"Invalid watermark image ({reason_img})", "invalid_image")
        img_p = Path(str(image_path).strip())
        if is_unsafe_reparse_or_system(img_p) or is_unsafe_reparse_or_system(img_p.resolve()):
            return _error_result("add_watermark", "Invalid watermark image (reparse_or_system_blocked)", "invalid_image")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("add_watermark", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("add_watermark", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        indices = _parse_page_list(params.get("pages"), total, default_all=True, allow_duplicates=False)
        for pos, pidx in enumerate(indices, start=1):
            emit_progress(progress_cb, int(10 + 75 * (pos / max(1, len(indices)))), f"Watermarking page {pidx + 1}")
            page = src.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            w = max(1.0, mb[2] - mb[0])
            h = max(1.0, mb[3] - mb[1])

            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.saveState()
            c.translate(w / 2.0, h / 2.0)
            c.rotate(angle)
            c.setFillAlpha(opacity)
            if image_path:
                wm_w = min(w * 0.5, 240.0)
                wm_h = min(h * 0.5, 240.0)
                c.drawImage(str(image_path), -wm_w / 2.0, -wm_h / 2.0, width=wm_w, height=wm_h, mask="auto")
            if text:
                c.setFillColorRGB(color[0], color[1], color[2], alpha=opacity)
                c.setFont("Helvetica-Bold", font_size)
                c.drawCentredString(0, 0, text)
            c.restoreState()
            c.showPage()
            c.save()
            buf.seek(0)
            with pikepdf.open(buf) as wm_pdf:
                page.add_overlay(wm_pdf.pages[0], pikepdf.Rectangle(0, 0, w, h))

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("add_watermark", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Applied watermark to {len(indices)} page(s).",
        details={"pages_watermarked": len(indices), "text": text},
    )


def _handle_add_page_numbers(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    from reportlab.pdfgen import canvas

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("add_page_numbers", err_out, "invalid_output")

    fmt = str(params.get("format") or "{n}/{total}")
    position = str(params.get("position") or "bottom_right").strip().lower()
    valid_positions = {"bottom_left", "bottom_center", "bottom_right", "top_left", "top_center", "top_right"}
    if position not in valid_positions:
        return _error_result("add_page_numbers", f"Invalid position: {position}", "invalid_params")

    start_number = int(params.get("start_number", 1))
    font_size = float(params.get("font_size", 10.0))
    margin = float(params.get("margin", 36.0))
    if font_size <= 0.0 or margin < 0.0:
        return _error_result("add_page_numbers", "Invalid font_size or margin", "invalid_params")
    color = _parse_hex_color(params.get("color", "#334155"), default=(0.2, 0.25, 0.33))
    pw = str(params.get("password") or "")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("add_page_numbers", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("add_page_numbers", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        indices = _parse_page_list(params.get("pages"), total, default_all=True, allow_duplicates=False)
        for pos, pidx in enumerate(indices, start=1):
            emit_progress(progress_cb, int(10 + 75 * (pos / max(1, len(indices)))), f"Numbering page {pidx + 1}")
            try:
                label = fmt.format(n=start_number + pidx, total=total, page=pidx + 1, pages=total)
            except (KeyError, ValueError, IndexError) as exc:
                return _error_result("add_page_numbers", f"Invalid format string: {exc}", "invalid_format")

            page = src.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            w = max(1.0, mb[2] - mb[0])
            h = max(1.0, mb[3] - mb[1])

            y = margin if position.startswith("bottom_") else max(font_size, h - margin)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            c.setFillColorRGB(*color)
            c.setFont("Helvetica", font_size)
            if position.endswith("_left"):
                c.drawString(margin, y, label)
            elif position.endswith("_center"):
                c.drawCentredString(w / 2.0, y, label)
            else:
                c.drawRightString(max(margin, w - margin), y, label)
            c.showPage()
            c.save()
            buf.seek(0)
            with pikepdf.open(buf) as num_pdf:
                page.add_overlay(num_pdf.pages[0], pikepdf.Rectangle(0, 0, w, h))

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("add_page_numbers", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Added page numbers to {len(indices)} page(s).",
        details={"pages_numbered": len(indices), "position": position},
    )


def _handle_overlay_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("overlay_pdf", err_out, "invalid_output")

    mode = str(params.get("mode") or "over").strip().lower()
    if mode not in ("over", "under", "foreground", "background"):
        return _error_result("overlay_pdf", f"Invalid overlay mode: {mode}", "invalid_params")
    repeat_overlay = bool(params.get("repeat_overlay", True))
    pw = str(params.get("password") or "")

    base_path = inputs[0]
    if len(inputs) >= 2:
        overlay_path = inputs[1]
    else:
        raw_ov = params.get("overlay_path")
        ok_ov, reason_ov = validate_local_pdf_path(raw_ov or "", check_exists=True)
        if not ok_ov:
            return _error_result("overlay_pdf", f"Invalid overlay PDF ({reason_ov})", "invalid_overlay")
        raw_ov_p = Path(str(raw_ov).strip())
        if is_unsafe_reparse_or_system(raw_ov_p):
            return _error_result("overlay_pdf", "Invalid overlay PDF (reparse_or_system_blocked)", "invalid_overlay")
        overlay_path = raw_ov_p.resolve()
        if is_unsafe_reparse_or_system(overlay_path):
            return _error_result("overlay_pdf", "Invalid overlay PDF (reparse_or_system_blocked)", "invalid_overlay")

    try:
        base_pdf = _open_pikepdf(base_path, pw)
    except pikepdf.PasswordError:
        return _error_result("overlay_pdf", "Encrypted base PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("overlay_pdf", f"Invalid base PDF: {exc}", "invalid_pdf")

    try:
        ov_pdf = _open_pikepdf(overlay_path, str(params.get("overlay_password") or ""))
    except pikepdf.PasswordError:
        base_pdf.close()
        return _error_result("overlay_pdf", "Encrypted overlay PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        base_pdf.close()
        return _error_result("overlay_pdf", f"Invalid overlay PDF: {exc}", "invalid_pdf")

    try:
        ov_count = len(ov_pdf.pages)
        if ov_count == 0:
            return _error_result("overlay_pdf", "Overlay PDF has zero pages", "empty_overlay")
        indices = _parse_page_list(params.get("pages"), len(base_pdf.pages), default_all=True, allow_duplicates=False)
        for pos, pidx in enumerate(indices, start=1):
            emit_progress(progress_cb, int(10 + 75 * (pos / max(1, len(indices)))), f"Overlaying page {pidx + 1}")
            ov_idx = (pidx % ov_count) if repeat_overlay else pidx
            if ov_idx >= ov_count:
                continue
            page = base_pdf.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            rect = pikepdf.Rectangle(0, 0, max(1.0, mb[2] - mb[0]), max(1.0, mb[3] - mb[1]))
            if mode in ("under", "background"):
                page.add_underlay(ov_pdf.pages[ov_idx], rect)
            else:
                page.add_overlay(ov_pdf.pages[ov_idx], rect)
        _save_pikepdf_atomic(base_pdf, out_path, close_handles=(ov_pdf, base_pdf))
    except ValueError as exc:
        return _error_result("overlay_pdf", str(exc), "invalid_pages")
    finally:
        ov_pdf.close()
        base_pdf.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Overlay applied to {len(indices)} page(s) in '{mode}' mode.",
        details={"pages_overlaid": len(indices), "mode": mode},
    )


def _pdf_timestamp(raw: str | None = None) -> str:
    if raw and raw.startswith("D:"):
        return raw
    dt = datetime.now(timezone.utc)
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.strftime("D:%Y%m%d%H%M%SZ")


def _handle_annotate_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("annotate_pdf", err_out, "invalid_output")

    annotations = params.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        return _error_result("annotate_pdf", "annotations must be a non-empty list", "invalid_params")

    default_author = str(params.get("default_author") or "").strip() or _DEFAULT_ANON_AUTHOR
    pw = str(params.get("password") or "")
    subtype_map = {
        "highlight": "/Highlight",
        "text": "/Text",
        "ink": "/Ink",
        "freetext": "/FreeText",
        "popup": "/Popup",
    }

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("annotate_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("annotate_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        applied = 0
        for idx, item in enumerate(annotations, start=1):
            emit_progress(progress_cb, int(10 + 75 * (idx / len(annotations))), f"Adding annotation {idx}")
            if not isinstance(item, dict):
                return _error_result("annotate_pdf", "Each annotation must be a dict", "invalid_params")
            kind = str(item.get("kind") or "highlight").strip().lower()
            if kind not in subtype_map:
                return _error_result("annotate_pdf", f"Unsupported annotation kind: {kind}", "invalid_params")

            if "page_index" in item and item["page_index"] is not None:
                pidx = item["page_index"]
                if isinstance(pidx, bool) or not isinstance(pidx, int):
                    return _error_result("annotate_pdf", f"Invalid page_index: {pidx!r}", "invalid_pages")
            elif "page" in item and item["page"] is not None:
                p_one = item["page"]
                if isinstance(p_one, bool) or not isinstance(p_one, int):
                    return _error_result("annotate_pdf", f"Invalid page: {p_one!r}", "invalid_pages")
                pidx = p_one - 1
            else:
                pidx = 0

            if pidx < 0 or pidx >= total:
                return _error_result("annotate_pdf", f"page_index {pidx} out of range 0..{total - 1}", "invalid_pages")

            raw_points = item.get("points") or item.get("points_pdf") or []
            raw_rect = item.get("rect") or item.get("rect_pdf")
            if raw_rect is None and kind == "ink" and raw_points:
                flat_pts = [pt for stroke in raw_points for pt in stroke]
                if not flat_pts:
                    return _error_result("annotate_pdf", "Ink annotation has empty points", "invalid_params")
                xs = [float(pt[0]) for pt in flat_pts]
                ys = [float(pt[1]) for pt in flat_pts]
                raw_rect = [min(xs), min(ys), max(xs) + 1.0, max(ys) + 1.0]

            if not isinstance(raw_rect, (list, tuple)) or len(raw_rect) != 4:
                return _error_result("annotate_pdf", "Annotation requires 4-element rect", "invalid_params")
            x0, y0, x1, y1 = [float(v) for v in raw_rect]
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                return _error_result("annotate_pdf", "Non-finite annotation rect", "invalid_params")
            rx0, rx1 = min(x0, x1), max(x0, x1)
            ry0, ry1 = min(y0, y1), max(y0, y1)
            if kind in ("highlight", "freetext") and (rx1 - rx0 <= 0.0 or ry1 - ry0 <= 0.0):
                return _error_result("annotate_pdf", "Annotation rect must have positive area", "invalid_params")
            if kind == "text" and (rx1 == rx0 or ry1 == ry0):
                rx1 = rx0 + 24.0
                ry1 = ry0 + 24.0

            author_val = str(item.get("author") or "").strip() or default_author
            content_val = str(item.get("content") or "")
            ts_val = _pdf_timestamp(item.get("timestamp") or item.get("timestamp_iso"))
            color_rgb = _parse_hex_color(item.get("color", "#FEF08A"), default=(1.0, 0.94, 0.54))

            page = src.pages[pidx]
            annot = pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name(subtype_map[kind]),
                Rect=pikepdf.Array([rx0, ry0, rx1, ry1]),
                C=pikepdf.Array([color_rgb[0], color_rgb[1], color_rgb[2]]),
                T=pikepdf.String(author_val),
                M=pikepdf.String(ts_val),
                Contents=pikepdf.String(content_val),
                F=4,
                P=page.obj,
            )
            if kind == "highlight":
                annot["/QuadPoints"] = pikepdf.Array([rx0, ry1, rx1, ry1, rx0, ry0, rx1, ry0])
                annot["/CA"] = 0.4
            elif kind == "ink":
                ink_list = pikepdf.Array()
                for stroke in raw_points:
                    arr = pikepdf.Array()
                    for pt in stroke:
                        arr.append(float(pt[0]))
                        arr.append(float(pt[1]))
                    ink_list.append(arr)
                if len(ink_list) == 0:
                    ink_list.append(pikepdf.Array([rx0, ry0, rx1, ry1]))
                annot["/InkList"] = ink_list
                annot["/BS"] = pikepdf.Dictionary(W=float(item.get("width", 2.0)), S=pikepdf.Name("/S"))
            elif kind == "freetext":
                annot["/DA"] = pikepdf.String("/Helv 11 Tf 0 0 0 rg")

            indirect_annot = src.make_indirect(annot)
            if "/Annots" not in page or not isinstance(page["/Annots"], pikepdf.Array):
                page["/Annots"] = pikepdf.Array()
            page["/Annots"].append(indirect_annot)
            applied += 1

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Embedded {applied} ISO 32000 annotation(s).",
        details={"annotations_added": applied},
    )


def _handle_crop_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("crop_pdf", err_out, "invalid_output")

    margin = params.get("margin")
    left = float(params.get("left", 0.0))
    right = float(params.get("right", 0.0))
    top = float(params.get("top", 0.0))
    bottom = float(params.get("bottom", 0.0))

    if isinstance(margin, (int, float)) and not isinstance(margin, bool):
        m = float(margin)
        left = right = top = bottom = m
    elif isinstance(margin, dict):
        left = float(margin.get("left", left))
        right = float(margin.get("right", right))
        top = float(margin.get("top", top))
        bottom = float(margin.get("bottom", bottom))

    if min(left, right, top, bottom) < 0.0:
        return _error_result("crop_pdf", "Crop margins cannot be negative", "invalid_params")

    box = params.get("box")
    clip_mediabox = bool(params.get("clip_mediabox", False))
    pw = str(params.get("password") or "")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("crop_pdf", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("crop_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        indices = _parse_page_list(params.get("pages"), len(src.pages), default_all=True, allow_duplicates=False)
        for pos, pidx in enumerate(indices, start=1):
            emit_progress(progress_cb, int(10 + 75 * (pos / max(1, len(indices)))), f"Cropping page {pidx + 1}")
            page = src.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            if box is not None:
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    return _error_result("crop_pdf", "box must be a 4-element list", "invalid_params")
                nx0, ny0, nx1, ny1 = [float(v) for v in box]
                if nx0 < mb[0] or ny0 < mb[1] or nx1 > mb[2] or ny1 > mb[3]:
                    return _error_result("crop_pdf", "Crop box exceeds MediaBox bounds", "invalid_crop_box")
            else:
                nx0 = mb[0] + left
                ny0 = mb[1] + bottom
                nx1 = mb[2] - right
                ny1 = mb[3] - top

            if nx0 >= nx1 or ny0 >= ny1:
                return _error_result("crop_pdf", "Crop margins exceed page dimensions", "overcrop")

            arr = pikepdf.Array([nx0, ny0, nx1, ny1])
            page.CropBox = arr
            if clip_mediabox:
                page.MediaBox = pikepdf.Array([nx0, ny0, nx1, ny1])

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("crop_pdf", str(exc), "invalid_pages")
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Cropped {len(indices)} page(s).",
        details={"pages_cropped": len(indices)},
    )


def _handle_edit_metadata(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("edit_metadata", err_out, "invalid_output")

    extra_meta = params.get("metadata")
    if extra_meta is not None and not isinstance(extra_meta, dict):
        return _error_result("edit_metadata", "metadata must be a dictionary", "invalid_params")

    pw = str(params.get("password") or "")
    clear_xmp = bool(params.get("clear_xmp", False))

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("edit_metadata", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("edit_metadata", f"Invalid PDF: {exc}", "invalid_pdf")

    updated_keys: list[str] = []
    try:
        emit_progress(progress_cb, 40, "Updating metadata")
        key_map = {
            "title": "/Title",
            "author": "/Author",
            "subject": "/Subject",
            "keywords": "/Keywords",
            "creator": "/Creator",
            "producer": "/Producer",
        }
        for param_key, pdf_key in key_map.items():
            val = params.get(param_key)
            if val is not None:
                src.docinfo[pdf_key] = str(val)
                updated_keys.append(pdf_key)

        if extra_meta:
            for k, v in extra_meta.items():
                norm_k = k if str(k).startswith("/") else f"/{k}"
                if v is None:
                    if norm_k in src.docinfo:
                        del src.docinfo[norm_k]
                else:
                    src.docinfo[norm_k] = str(v)
                    updated_keys.append(norm_k)

        if clear_xmp and "/Metadata" in src.Root:
            del src.Root["/Metadata"]

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Updated {len(updated_keys)} metadata field(s).",
        details={"updated_keys": updated_keys, "cleared_xmp": clear_xmp},
    )


def _handle_resize_pages(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("resize_pages", err_out, "invalid_output")

    preset = str(params.get("preset") or "A4").strip().upper()
    if preset == "CUSTOM":
        w_pt = params.get("width_pt")
        h_pt = params.get("height_pt")
        if w_pt is None or h_pt is None or float(w_pt) <= 0.0 or float(h_pt) <= 0.0:
            return _error_result("resize_pages", "Custom size requires positive width_pt and height_pt", "invalid_params")
        tw, th = float(w_pt), float(h_pt)
    elif preset in _PAGE_SIZE_PRESETS:
        tw, th = _PAGE_SIZE_PRESETS[preset]
    else:
        return _error_result("resize_pages", f"Unknown page size preset: {preset}", "invalid_params")

    scale_content = bool(params.get("scale_content", True))
    pw = str(params.get("password") or "")

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("resize_pages", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("resize_pages", f"Invalid PDF: {exc}", "invalid_pdf")

    out_pdf = pikepdf.Pdf.new()
    try:
        total = len(src.pages)
        target_set = set(_parse_page_list(params.get("pages"), total, default_all=True, allow_duplicates=False))
        for pidx in range(total):
            emit_progress(progress_cb, int(10 + 75 * ((pidx + 1) / max(1, total))), f"Resizing page {pidx + 1}")
            if pidx not in target_set:
                out_pdf.pages.append(src.pages[pidx])
                continue
            if scale_content:
                np = out_pdf.add_blank_page(page_size=(tw, th))
                np.add_overlay(src.pages[pidx], pikepdf.Rectangle(0, 0, tw, th), shrink=True, expand=True)
            else:
                out_pdf.pages.append(src.pages[pidx])
                out_pdf.pages[-1].MediaBox = pikepdf.Array([0.0, 0.0, tw, th])
                if "/CropBox" in out_pdf.pages[-1]:
                    out_pdf.pages[-1].CropBox = pikepdf.Array([0.0, 0.0, tw, th])

        _save_pikepdf_atomic(out_pdf, out_path, close_handles=(src,))
    except ValueError as exc:
        return _error_result("resize_pages", str(exc), "invalid_pages")
    finally:
        out_pdf.close()
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Resized {len(target_set)} page(s) to {preset} ({tw:.2f} x {th:.2f} pt).",
        details={"preset": preset, "width_pt": tw, "height_pt": th, "pages_resized": len(target_set)},
    )


def _handle_make_fillable(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    from reportlab.pdfgen import canvas

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("make_fillable", err_out, "invalid_output")

    fields = params.get("fields")
    if not isinstance(fields, list) or not fields:
        return _error_result("make_fillable", "fields must be a non-empty list", "invalid_params")

    pw = str(params.get("password") or "")
    valid_kinds = {"text", "checkbox", "choice", "dropdown", "radio"}

    try:
        src = _open_pikepdf(inputs[0], pw)
    except pikepdf.PasswordError:
        return _error_result("make_fillable", "Encrypted PDF requires password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("make_fillable", f"Invalid PDF: {exc}", "invalid_pdf")

    try:
        total = len(src.pages)
        by_page: dict[int, list[dict[str, Any]]] = {}
        for f in fields:
            if not isinstance(f, dict):
                return _error_result("make_fillable", "Each field must be a dict", "invalid_params")
            fname = str(f.get("name") or "").strip()
            if not fname:
                return _error_result("make_fillable", "Field name cannot be empty", "invalid_params")
            fkind = str(f.get("kind") or "text").strip().lower()
            if fkind not in valid_kinds:
                return _error_result("make_fillable", f"Unsupported field kind: {fkind}", "invalid_params")
            if "page_index" in f and f["page_index"] is not None:
                raw_pi = f["page_index"]
                if isinstance(raw_pi, bool) or not isinstance(raw_pi, int):
                    return _error_result("make_fillable", f"Invalid page_index: {raw_pi!r}", "invalid_pages")
                pidx = raw_pi
            elif "page" in f and f["page"] is not None:
                raw_p = f["page"]
                if isinstance(raw_p, bool) or not isinstance(raw_p, int):
                    return _error_result("make_fillable", f"Invalid page: {raw_p!r}", "invalid_pages")
                pidx = raw_p - 1
            else:
                pidx = 0
            if pidx < 0 or pidx >= total:
                return _error_result("make_fillable", f"Field page index {pidx} out of range", "invalid_pages")
            rect = f.get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                return _error_result("make_fillable", "Field requires 4-element rect", "invalid_params")
            x0, y0, x1, y1 = [float(v) for v in rect]
            if x1 <= x0 or y1 <= y0:
                return _error_result("make_fillable", "Field rect must satisfy x1 > x0 and y1 > y0", "invalid_rect")
            by_page.setdefault(pidx, []).append(f)

        if "/AcroForm" not in src.Root or not isinstance(src.Root["/AcroForm"], pikepdf.Dictionary):
            src.Root["/AcroForm"] = src.make_indirect(
                pikepdf.Dictionary(Fields=pikepdf.Array(), NeedAppearances=True)
            )
        acro_root = src.Root["/AcroForm"]
        if "/Fields" not in acro_root or not isinstance(acro_root["/Fields"], pikepdf.Array):
            acro_root["/Fields"] = pikepdf.Array()
        acro_root["/NeedAppearances"] = True

        added_count = 0
        page_order = sorted(by_page.keys())
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        form = c.acroForm
        for step_idx, pidx in enumerate(page_order, start=1):
            emit_progress(progress_cb, int(10 + 75 * (step_idx / len(page_order))), f"Adding fields on page {pidx + 1}")
            page = src.pages[pidx]
            mb = [float(v) for v in page.MediaBox]
            w = max(1.0, mb[2] - mb[0])
            h = max(1.0, mb[3] - mb[1])
            c.setPageSize((w, h))

            for fld in by_page[pidx]:
                fname = str(fld["name"]).strip()
                fkind = str(fld.get("kind") or "text").strip().lower()
                x0, y0, x1, y1 = [float(v) for v in fld["rect"]]
                fw = x1 - x0
                fh = y1 - y0
                tooltip = str(fld.get("tooltip") or fname)
                if fkind == "text":
                    form.textfield(
                        name=fname,
                        tooltip=tooltip,
                        x=x0,
                        y=y0,
                        width=fw,
                        height=fh,
                        value=str(fld.get("value") or ""),
                        borderWidth=1,
                    )
                elif fkind == "checkbox":
                    form.checkbox(
                        name=fname,
                        tooltip=tooltip,
                        x=x0,
                        y=y0,
                        size=min(fw, fh),
                        checked=bool(fld.get("checked", False)),
                        borderWidth=1,
                    )
                elif fkind in ("choice", "dropdown"):
                    opts = [str(o) for o in (fld.get("options") or ["Option 1", "Option 2"])]
                    form.choice(
                        name=fname,
                        tooltip=tooltip,
                        x=x0,
                        y=y0,
                        width=fw,
                        height=fh,
                        options=opts,
                        value=str(fld.get("value") or opts[0]),
                        borderWidth=1,
                    )
                elif fkind == "radio":
                    form.radio(
                        name=fname,
                        tooltip=tooltip,
                        value=str(fld.get("value") or "Option1"),
                        x=x0,
                        y=y0,
                        size=min(fw, fh),
                        selected=bool(fld.get("checked", False)),
                    )
            c.showPage()

        c.save()
        buf.seek(0)

        with pikepdf.open(buf) as rl_pdf:
            if "/AcroForm" in rl_pdf.Root and "/DR" in rl_pdf.Root["/AcroForm"] and "/DR" not in acro_root:
                dr_obj = rl_pdf.Root["/AcroForm"]["/DR"]
                if not dr_obj.is_indirect:
                    dr_obj = rl_pdf.make_indirect(dr_obj)
                acro_root["/DR"] = src.copy_foreign(dr_obj)
            if "/AcroForm" in rl_pdf.Root and "/DA" in rl_pdf.Root["/AcroForm"] and "/DA" not in acro_root:
                acro_root["/DA"] = pikepdf.String(str(rl_pdf.Root["/AcroForm"]["/DA"]))

            for rl_idx, pidx in enumerate(page_order):
                dst_page = src.pages[pidx]
                if "/Annots" not in dst_page or not isinstance(dst_page["/Annots"], pikepdf.Array):
                    dst_page["/Annots"] = pikepdf.Array()
                for raw_annot in rl_pdf.pages[rl_idx].get("/Annots", []):
                    if "/P" in raw_annot:
                        del raw_annot["/P"]
                    raw_annot["/__trodai_pidx"] = pidx

            rl_fields = rl_pdf.Root["/AcroForm"].get("/Fields", []) if "/AcroForm" in rl_pdf.Root else []
            for raw_field in rl_fields:
                ind_field = raw_field if raw_field.is_indirect else rl_pdf.make_indirect(raw_field)
                copied_field = src.copy_foreign(ind_field)
                if "/Kids" in copied_field and isinstance(copied_field["/Kids"], pikepdf.Array):
                    field_name_str = str(copied_field.get("/T", ""))
                    existing_parent = None
                    if field_name_str:
                        for prev_f in acro_root["/Fields"]:
                            if (
                                isinstance(prev_f, pikepdf.Dictionary)
                                and str(prev_f.get("/T", "")) == field_name_str
                                and "/Kids" in prev_f
                                and isinstance(prev_f["/Kids"], pikepdf.Array)
                            ):
                                existing_parent = prev_f
                                break
                    if existing_parent is not None:
                        if "/V" in copied_field and str(copied_field["/V"]) != "/Off":
                            existing_parent["/V"] = copied_field["/V"]
                        for kid in copied_field["/Kids"]:
                            target_pidx = int(kid.get("/__trodai_pidx", page_order[0]))
                            if "/__trodai_pidx" in kid:
                                del kid["/__trodai_pidx"]
                            target_page = src.pages[target_pidx]
                            kid["/Parent"] = existing_parent
                            kid["/P"] = target_page.obj
                            existing_parent["/Kids"].append(kid)
                            target_page["/Annots"].append(kid)
                            added_count += 1
                    else:
                        acro_root["/Fields"].append(copied_field)
                        for kid in copied_field["/Kids"]:
                            target_pidx = int(kid.get("/__trodai_pidx", page_order[0]))
                            if "/__trodai_pidx" in kid:
                                del kid["/__trodai_pidx"]
                            target_page = src.pages[target_pidx]
                            kid["/P"] = target_page.obj
                            target_page["/Annots"].append(kid)
                            added_count += 1
                else:
                    target_pidx = int(copied_field.get("/__trodai_pidx", page_order[0]))
                    if "/__trodai_pidx" in copied_field:
                        del copied_field["/__trodai_pidx"]
                    target_page = src.pages[target_pidx]
                    copied_field["/P"] = target_page.obj
                    target_page["/Annots"].append(copied_field)
                    acro_root["/Fields"].append(copied_field)
                    added_count += 1

        _save_pikepdf_atomic(src, out_path, close_handles=(src,))
    finally:
        src.close()

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Added {added_count} interactive AcroForm field(s).",
        details={"fields_added": added_count},
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="merge_pdf",
        category="organize",
        icon="merge",
        min_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_merge_pdf"),
        params=[ToolParamSpec("preserve_bookmarks", "bool", True, label="Preserve bookmarks")],
    ),
    ToolDefinition(
        id="split_pdf",
        category="organize",
        icon="split",
        min_inputs=1,
        max_inputs=1,
        output_kind="dir",
        handler=("pdf_lector.core.tools.organize", "_handle_split_pdf"),
        params=[
            ToolParamSpec("ranges", "any", "", label="Page ranges"),
            ToolParamSpec("mode", "choice", "ranges", choices=("ranges", "every_n", "single_pages")),
            ToolParamSpec("chunk_size", "int", 1, min_val=1, max_val=10000),
        ],
    ),
    ToolDefinition(
        id="rotate_pdf",
        category="organize",
        icon="rotate",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_rotate_pdf"),
        params=[
            ToolParamSpec("angle", "int", 90, choices=(-270, -180, -90, 0, 90, 180, 270, 360)),
            ToolParamSpec("pages", "any", None),
            ToolParamSpec("relative", "bool", True),
        ],
    ),
    ToolDefinition(
        id="delete_pages",
        category="organize",
        icon="edit",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_delete_pages"),
        params=[ToolParamSpec("pages", "any", "")],
    ),
    ToolDefinition(
        id="delete_blank_pages",
        category="organize",
        icon="edit",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_delete_blank_pages"),
        params=[
            ToolParamSpec("threshold", "float", 0.0005, min_val=0.0, max_val=0.5),
            ToolParamSpec("white_level", "int", 250, min_val=150, max_val=255),
        ],
    ),
    ToolDefinition(
        id="extract_pages",
        category="organize",
        icon="export",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_extract_pages"),
        params=[ToolParamSpec("pages", "any", "")],
    ),
    ToolDefinition(
        id="reorder_pages",
        category="organize",
        icon="merge",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_reorder_pages"),
        params=[
            ToolParamSpec("order", "any", ""),
            ToolParamSpec("strict_permutation", "bool", True),
        ],
    ),
    ToolDefinition(
        id="extract_images",
        category="organize",
        icon="image",
        min_inputs=1,
        max_inputs=1,
        output_kind="dir",
        handler=("pdf_lector.core.tools.organize", "_handle_extract_images"),
        params=[
            ToolParamSpec("pages", "any", None),
            ToolParamSpec("deduplicate", "bool", True),
        ],
    ),
    ToolDefinition(
        id="extract_attachments",
        category="organize",
        icon="open",
        min_inputs=1,
        max_inputs=1,
        output_kind="dir",
        handler=("pdf_lector.core.tools.organize", "_handle_extract_attachments"),
        params=[ToolParamSpec("overwrite", "bool", True)],
    ),
    ToolDefinition(
        id="nup_pages",
        category="organize",
        icon="compare",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_nup_pages"),
        params=[
            ToolParamSpec("nup", "int", 4, min_val=0, max_val=16),
            ToolParamSpec("page_size", "str", "Letter"),
            ToolParamSpec("margin", "float", 12.0, min_val=0.0, max_val=200.0),
            ToolParamSpec("gap", "float", 6.0, min_val=0.0, max_val=100.0),
        ],
    ),
    ToolDefinition(
        id="edit_bookmarks",
        category="organize",
        icon="viewer",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_edit_bookmarks"),
        params=[
            ToolParamSpec("bookmarks", "list", []),
            ToolParamSpec("mode", "choice", "replace", choices=("replace", "append")),
        ],
    ),
    ToolDefinition(
        id="edit_pdf",
        category="organize",
        icon="edit",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_edit_pdf"),
        params=[
            ToolParamSpec("page", "int", 1, min_val=1),
            ToolParamSpec("text", "str", ""),
            ToolParamSpec("x", "float", 72.0),
            ToolParamSpec("y", "float", 700.0),
            ToolParamSpec("font_size", "float", 12.0, min_val=1.0, max_val=256.0),
            ToolParamSpec("color", "str", "#0A0F1D"),
            ToolParamSpec("font_name", "str", "Helvetica"),
            ToolParamSpec("edits", "any", None),
        ],
    ),
    ToolDefinition(
        id="add_watermark",
        category="organize",
        icon="shield",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_add_watermark"),
        params=[
            ToolParamSpec("text", "str", "CONFIDENCIAL"),
            ToolParamSpec("image_path", "any", None),
            ToolParamSpec("opacity", "float", 0.25, min_val=0.0, max_val=1.0),
            ToolParamSpec("angle", "float", 35.0),
            ToolParamSpec("font_size", "float", 42.0, min_val=4.0, max_val=256.0),
            ToolParamSpec("color", "str", "#DC2626"),
            ToolParamSpec("pages", "any", None),
        ],
    ),
    ToolDefinition(
        id="add_page_numbers",
        category="organize",
        icon="edit",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_add_page_numbers"),
        params=[
            ToolParamSpec("format", "str", "{n}/{total}"),
            ToolParamSpec("position", "str", "bottom_right"),
            ToolParamSpec("start_number", "int", 1, min_val=0),
            ToolParamSpec("font_size", "float", 10.0, min_val=4.0, max_val=72.0),
            ToolParamSpec("margin", "float", 36.0, min_val=0.0, max_val=200.0),
            ToolParamSpec("color", "str", "#334155"),
            ToolParamSpec("pages", "any", None),
        ],
    ),
    ToolDefinition(
        id="overlay_pdf",
        category="organize",
        icon="merge",
        min_inputs=1,
        max_inputs=2,
        handler=("pdf_lector.core.tools.organize", "_handle_overlay_pdf"),
        params=[
            ToolParamSpec("overlay_path", "any", None),
            ToolParamSpec("mode", "str", "over"),
            ToolParamSpec("repeat_overlay", "bool", True),
            ToolParamSpec("pages", "any", None),
        ],
    ),
    ToolDefinition(
        id="annotate_pdf",
        category="organize",
        icon="sign",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_annotate_pdf"),
        params=[
            ToolParamSpec("annotations", "list", []),
            ToolParamSpec("default_author", "str", _DEFAULT_ANON_AUTHOR),
        ],
    ),
    ToolDefinition(
        id="crop_pdf",
        category="organize",
        icon="fit_width",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_crop_pdf"),
        params=[
            ToolParamSpec("margin", "any", None),
            ToolParamSpec("left", "float", 0.0),
            ToolParamSpec("right", "float", 0.0),
            ToolParamSpec("top", "float", 0.0),
            ToolParamSpec("bottom", "float", 0.0),
            ToolParamSpec("box", "any", None),
            ToolParamSpec("clip_mediabox", "bool", False),
            ToolParamSpec("pages", "any", None),
        ],
    ),
    ToolDefinition(
        id="edit_metadata",
        category="organize",
        icon="info",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_edit_metadata"),
        params=[
            ToolParamSpec("title", "str", None),
            ToolParamSpec("author", "str", None),
            ToolParamSpec("subject", "str", None),
            ToolParamSpec("keywords", "str", None),
            ToolParamSpec("creator", "str", None),
            ToolParamSpec("producer", "str", None),
            ToolParamSpec("metadata", "any", None),
            ToolParamSpec("clear_xmp", "bool", False),
        ],
    ),
    ToolDefinition(
        id="resize_pages",
        category="organize",
        icon="fit_width",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_resize_pages"),
        params=[
            ToolParamSpec("preset", "str", "A4"),
            ToolParamSpec("width_pt", "float", None),
            ToolParamSpec("height_pt", "float", None),
            ToolParamSpec("scale_content", "bool", True),
            ToolParamSpec("pages", "any", None),
        ],
    ),
    ToolDefinition(
        id="make_fillable",
        category="organize",
        icon="create",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.organize", "_handle_make_fillable"),
        params=[ToolParamSpec("fields", "list", [])],
    ),
)


def register_tools(registry: type[ToolRegistry]) -> None:
    """Register all 20 Organize & Edit tools."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn)
