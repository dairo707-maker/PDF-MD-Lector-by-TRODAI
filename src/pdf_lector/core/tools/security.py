"""Security and Privacy PDF tools (5 tools) wiring M1 destructive redaction, metadata sanitization, AES-256, and pyHanko PKCS#12."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    emit_progress,
    get_shared_temp_manager,
    make_sibling_stage_path,
    validate_local_ext_path,
    validate_output_pdf_path,
)


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _handle_protect_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("protect_pdf", err_out, "invalid_output")

    user_pw = str(params.get("user_password") or "")
    owner_pw = str(params.get("owner_password") or "")
    if not user_pw and not owner_pw:
        return _error_result(
            "protect_pdf",
            "At least one password (user_password or owner_password) is required",
            "empty_passwords",
        )
    # Prevent pikepdf passwordless-open bypass when user_password is set and owner_password is empty.
    if user_pw and not owner_pw:
        owner_pw = user_pw

    allow_print = bool(params.get("allow_print", True))
    allow_copy = bool(params.get("allow_copy", True))
    allow_modify = bool(params.get("allow_modify", False))
    allow_annotate = bool(params.get("allow_annotate", True))
    encrypt_metadata = bool(params.get("encrypt_metadata", True))
    input_pw = str(params.get("input_password") or "")

    perms = pikepdf.Permissions(
        accessibility=True,
        extract=allow_copy,
        modify_annotation=allow_annotate,
        modify_assembly=allow_modify,
        modify_form=allow_annotate or allow_modify,
        modify_other=allow_modify,
        print_lowres=allow_print,
        print_highres=allow_print,
    )

    in_path = inputs[0]
    try:
        pdf = pikepdf.open(in_path, password=input_pw) if input_pw else pikepdf.open(in_path)
    except pikepdf.PasswordError:
        return _error_result("protect_pdf", "Input PDF is already encrypted and requires input_password", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("protect_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    temp_mgr = get_shared_temp_manager()
    try:
        emit_progress(progress_cb, 45, "Encrypting PDF with AES-256")
        with temp_mgr.task_scope(secure=True) as tid:
            stage = make_sibling_stage_path(out_path)
            temp_mgr.register_path(stage, task_id=tid, secure=True)
            pdf.save(
                stage,
                encryption=pikepdf.Encryption(
                    owner=owner_pw,
                    user=user_pw,
                    R=6,
                    allow=perms,
                    aes=True,
                    metadata=encrypt_metadata,
                ),
            )
            pdf.close()
            atomic_replace_file(stage, out_path)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary="Protected PDF with AES-256 (R=6) encryption.",
        details={
            "algorithm": "AES-256",
            "revision": 6,
            "has_user_password": bool(user_pw),
            "permissions": {
                "allow_print": allow_print,
                "allow_copy": allow_copy,
                "allow_modify": allow_modify,
                "allow_annotate": allow_annotate,
            },
        },
    )


def _handle_unlock_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("unlock_pdf", err_out, "invalid_output")

    pw = str(params.get("password") or "")
    in_path = inputs[0]

    try:
        pdf = pikepdf.open(in_path, password=pw) if pw else pikepdf.open(in_path)
    except pikepdf.PasswordError:
        return _error_result("unlock_pdf", "Invalid PDF password", "invalid_password")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("unlock_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    temp_mgr = get_shared_temp_manager()
    try:
        was_encrypted = bool(pdf.is_encrypted)
        emit_progress(progress_cb, 50, "Removing encryption")
        with temp_mgr.task_scope(secure=True) as tid:
            stage = make_sibling_stage_path(out_path)
            temp_mgr.register_path(stage, task_id=tid, secure=True)
            pdf.save(stage, encryption=False)
            pdf.close()
            atomic_replace_file(stage, out_path)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary="Unlocked PDF successfully.",
        details={"was_encrypted": was_encrypted, "unlocked": True},
    )


def _collect_field_names(
    fields_arr: Any,
    names: set[str],
    seen: set[tuple[int, int]],
    pikepdf_mod: Any,
) -> None:
    if not isinstance(fields_arr, pikepdf_mod.Array):
        return
    for fld in fields_arr:
        if not isinstance(fld, pikepdf_mod.Dictionary):
            continue
        og = getattr(fld, "objgen", (0, 0))
        if og != (0, 0):
            if og in seen:
                continue
            seen.add(og)
        if "/T" in fld:
            names.add(str(fld["/T"]))
        kids = fld.get("/Kids")
        if isinstance(kids, pikepdf_mod.Array):
            _collect_field_names(kids, names, seen, pikepdf_mod)


def _existing_field_names(pdf_path: Path) -> tuple[int, set[str]]:
    import pikepdf

    names: set[str] = set()
    seen: set[tuple[int, int]] = set()
    with pikepdf.open(pdf_path) as pdf:
        num_pages = len(pdf.pages)
        acro = pdf.Root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            _collect_field_names(acro.get("/Fields"), names, seen, pikepdf)
        for page in pdf.pages:
            annots = page.get("/Annots")
            if isinstance(annots, pikepdf.Array):
                _collect_field_names(annots, names, seen, pikepdf)
    return num_pages, names


def _handle_sign_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pikepdf
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign import fields as sig_fields
    from pyhanko.sign import signers, validation
    from pyhanko_certvalidator import ValidationContext

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("sign_pdf", err_out, "invalid_output")

    raw_p12 = params.get("p12_path") or params.get("cert_path") or ""
    ok_p12, p12_path, err_p12 = validate_local_ext_path(raw_p12, {".p12", ".pfx"}, check_exists=True)
    if not ok_p12 or p12_path is None:
        return _error_result("sign_pdf", f"Invalid PKCS#12 certificate ({err_p12})", "invalid_p12_path")

    pw = str(params.get("password") or params.get("passphrase") or "")
    pass_bytes: bytes | None = pw.encode("utf-8") if pw else None

    emit_progress(progress_cb, 15, "Loading PKCS#12 signer")
    try:
        signer = signers.SimpleSigner.load_pkcs12(str(p12_path), passphrase=pass_bytes)
    except Exception as exc:
        return _error_result("sign_pdf", f"Failed to load PKCS#12 certificate: {exc}", "invalid_pkcs12")

    if signer is None:
        return _error_result(
            "sign_pdf",
            "Failed to load PKCS#12 certificate or invalid passphrase",
            "invalid_pkcs12_or_password",
        )

    in_path = inputs[0]
    try:
        num_pages, existing_names = _existing_field_names(in_path)
    except pikepdf.PasswordError:
        return _error_result("sign_pdf", "Input PDF is encrypted; unlock before signing", "encrypted_pdf")
    except (pikepdf.PdfError, OSError) as exc:
        return _error_result("sign_pdf", f"Invalid PDF: {exc}", "invalid_pdf")

    if num_pages <= 0:
        return _error_result("sign_pdf", "Input PDF has zero pages", "empty_pdf")

    raw_page = params.get("page", 1)
    if isinstance(raw_page, bool) or not isinstance(raw_page, int):
        return _error_result("sign_pdf", f"Invalid signature page: {raw_page!r}", "invalid_pages")
    page_one = raw_page
    if page_one < 1 or page_one > num_pages:
        return _error_result("sign_pdf", f"Signature page {page_one} out of range 1..{num_pages}", "invalid_pages")
    page_idx = page_one - 1

    requested_name = str(params.get("field_name") or "").strip() or "Signature1"
    fname = requested_name
    counter = 1
    while fname in existing_names:
        fname = f"{requested_name}_{counter}"
        counter += 1

    visible = bool(params.get("visual") or params.get("visible", False))
    raw_box = params.get("rect") if params.get("rect") is not None else params.get("box")
    box_tuple: tuple[float, float, float, float] | None = None
    if raw_box is not None:
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            return _error_result("sign_pdf", "Signature rect/box must have 4 coordinates", "invalid_params")
        bx0, by0, bx1, by1 = [float(v) for v in raw_box]
        if not all(math.isfinite(v) for v in (bx0, by0, bx1, by1)):
            return _error_result("sign_pdf", "Signature rect/box contains non-finite coordinates", "invalid_params")
        box_tuple = (bx0, by0, bx1, by1)
    elif visible:
        box_tuple = (36.0, 36.0, 220.0, 86.0)

    reason = str(params.get("reason") or "Signed with PDF/MD Lector by TRODAI")
    location = str(params.get("location") or "")
    contact_info = str(params.get("contact_info") or "")

    temp_mgr = get_shared_temp_manager()
    with temp_mgr.task_scope(secure=True) as tid:
        stage_out = make_sibling_stage_path(out_path)
        temp_mgr.register_path(stage_out, task_id=tid, secure=True)

        source_for_signing = in_path
        emit_progress(progress_cb, 45, "Signing PDF")
        try:
            with open(source_for_signing, "rb") as inf:
                try:
                    writer = IncrementalPdfFileWriter(inf, strict=False)
                except Exception:
                    # Normalize hybrid-xref or quirky PDFs before incremental signing
                    norm_pdf_path = temp_mgr.create_temp_file(suffix=".pdf", prefix="sign_norm_", task_id=tid, secure=True)
                    with pikepdf.open(in_path) as norm:
                        norm.save(norm_pdf_path, object_stream_mode=pikepdf.ObjectStreamMode.disable)
                    source_for_signing = norm_pdf_path
                    inf.close()
                    inf = open(source_for_signing, "rb")
                    writer = IncrementalPdfFileWriter(inf, strict=False)

                try:
                    sig_fields.append_signature_field(
                        writer,
                        sig_field_spec=sig_fields.SigFieldSpec(
                            sig_field_name=fname,
                            on_page=page_idx,
                            box=box_tuple,
                        ),
                    )
                    meta = signers.PdfSignatureMetadata(
                        field_name=fname,
                        reason=reason or None,
                        location=location or None,
                        contact_info=contact_info or None,
                        md_algorithm="sha256",
                    )
                    with open(stage_out, "wb") as outf:
                        signers.sign_pdf(writer, meta, signer=signer, output=outf)
                finally:
                    if not inf.closed:
                        inf.close()
        except Exception as exc:
            return _error_result("sign_pdf", f"Signing failed: {exc}", "signing_error")

        emit_progress(progress_cb, 80, "Verifying embedded signature")
        try:
            raw_signed = stage_out.read_bytes()
            if b"/ByteRange" not in raw_signed:
                return _error_result("sign_pdf", "Signed PDF missing /ByteRange", "signature_verification_failed")
            vc = ValidationContext(trust_roots=[signer.signing_cert])
            with open(stage_out, "rb") as vf:
                reader = PdfFileReader(vf, strict=False)
                if not reader.embedded_signatures:
                    return _error_result("sign_pdf", "No embedded signature found after signing", "signature_verification_failed")
                status = validation.validate_pdf_signature(reader.embedded_signatures[-1], signer_validation_context=vc)
                intact = bool(status.intact)
                valid = bool(status.valid)
                if not intact or not valid:
                    return _error_result("sign_pdf", "Signature integrity check failed", "signature_verification_failed")
        except Exception as exc:
            return _error_result("sign_pdf", f"Signature verification error: {exc}", "signature_verification_failed")

        atomic_replace_file(stage_out, out_path)

    subject_str = ""
    try:
        subject_str = signer.signing_cert.subject.human_friendly
    except Exception:
        pass

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Signed PDF with PKCS#12 field '{fname}'.",
        details={
            "field_name": fname,
            "intact": intact,
            "valid": valid,
            "signer_subject": subject_str,
            "page": page_one,
        },
    )


def _handle_redact_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    import pypdfium2 as pdfium

    from ..redaction import (
        RedactionOptions,
        RedactionRegion,
        redact_pdf_regions,
        verify_redaction_clean,
    )

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("redact_pdf", err_out, "invalid_output")

    in_path = inputs[0]
    targets_raw = params.get("targets") or []
    search_terms_raw = params.get("search_terms") or []
    if not isinstance(targets_raw, list) or not isinstance(search_terms_raw, list):
        return _error_result("redact_pdf", "targets and search_terms must be lists", "invalid_params")

    match_case = bool(params.get("match_case", False))
    match_whole_word = bool(params.get("match_whole_word", False))
    strip_meta = bool(params.get("strip_metadata_on_save", False))
    raw_color = params.get("fill_color", (0.0, 0.0, 0.0))
    try:
        fill_color = (float(raw_color[0]), float(raw_color[1]), float(raw_color[2]))
    except Exception:
        fill_color = (0.0, 0.0, 0.0)

    clean_terms = [str(t) for t in search_terms_raw if str(t)]
    regions: list[RedactionRegion] = []
    regions_by_page: dict[int, list[tuple[float, float, float, float]]] = {}

    emit_progress(progress_cb, 15, "Validating redaction targets")
    doc = None
    try:
        doc = pdfium.PdfDocument(str(in_path))
        num_pages = len(doc)
        if num_pages <= 0:
            return _error_result("redact_pdf", "Input PDF has zero pages", "empty_pdf")

        for item in targets_raw:
            if not isinstance(item, dict):
                return _error_result("redact_pdf", "Each redaction target must be a dict", "invalid_params")
            if "page_index" in item and item["page_index"] is not None:
                pidx = item["page_index"]
                if isinstance(pidx, bool) or not isinstance(pidx, int):
                    return _error_result("redact_pdf", f"Invalid page_index: {pidx!r}", "invalid_pages")
            elif "page" in item and item["page"] is not None:
                p_one = item["page"]
                if isinstance(p_one, bool) or not isinstance(p_one, int):
                    return _error_result("redact_pdf", f"Invalid page: {p_one!r}", "invalid_pages")
                pidx = p_one - 1
            else:
                return _error_result("redact_pdf", "Target missing page_index", "invalid_pages")

            if pidx < 0 or pidx >= num_pages:
                return _error_result("redact_pdf", f"page_index {pidx} out of range 0..{num_pages - 1}", "invalid_pages")

            rect_raw = item.get("rect")
            if not isinstance(rect_raw, (list, tuple)) or len(rect_raw) != 4:
                return _error_result("redact_pdf", "Target rect must have 4 coordinates", "invalid_rect")
            x0, y0, x1, y1 = [float(v) for v in rect_raw]
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                return _error_result("redact_pdf", "Target rect contains non-finite values", "invalid_rect")
            if abs(x1 - x0) <= 0.0 or abs(y1 - y0) <= 0.0:
                return _error_result("redact_pdf", "Target rect must have positive width and height", "invalid_rect")

            norm_rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            space = str(item.get("coords_space") or "pdf")
            if space not in ("pdf", "display"):
                space = "pdf"
            regions.append(RedactionRegion(page_index=pidx, rect=norm_rect, coords_space=space))  # type: ignore[arg-type]
            if space == "pdf":
                regions_by_page.setdefault(pidx, []).append(norm_rect)

        if clean_terms:
            emit_progress(progress_cb, 35, "Searching text terms for redaction")
            for pidx in range(num_pages):
                page = doc[pidx]
                tp = None
                try:
                    tp = page.get_textpage()
                    for term in clean_terms:
                        searcher = tp.search(
                            term,
                            index=0,
                            match_case=match_case,
                            match_whole_word=match_whole_word,
                        )
                        while True:
                            hit = searcher.get_next()
                            if hit is None:
                                break
                            char_idx, count = hit
                            n_rects = tp.count_rects(char_idx, count)
                            if n_rects > 0:
                                for r_idx in range(n_rects):
                                    rx0, ry0, rx1, ry1 = [float(v) for v in tp.get_rect(r_idx)]
                                    nr = (min(rx0, rx1), min(ry0, ry1), max(rx0, rx1), max(ry0, ry1))
                                    regions.append(RedactionRegion(pidx, nr, "pdf"))
                                    regions_by_page.setdefault(pidx, []).append(nr)
                            elif count > 0:
                                boxes = [tp.get_charbox(char_idx + off) for off in range(count)]
                                nr = (
                                    min(b[0] for b in boxes),
                                    min(b[1] for b in boxes),
                                    max(b[2] for b in boxes),
                                    max(b[3] for b in boxes),
                                )
                                regions.append(RedactionRegion(pidx, nr, "pdf"))
                                regions_by_page.setdefault(pidx, []).append(nr)
                finally:
                    if tp is not None:
                        tp.close()
                    page.close()
    except Exception as exc:
        return _error_result("redact_pdf", f"Failed to inspect PDF for redaction: {exc}", "invalid_pdf")
    finally:
        if doc is not None:
            doc.close()

    if not regions:
        return _error_result("redact_pdf", "No valid redaction targets or matching search terms", "empty_targets")

    emit_progress(progress_cb, 65, "Applying destructive redaction")
    opts = RedactionOptions(
        fill_color=fill_color,
        remove_annotations=True,
        strip_metadata_on_save=strip_meta,
    )
    res = redact_pdf_regions(in_path, out_path, regions, options=opts)

    emit_progress(progress_cb, 90, "Verifying destructive removal")
    clean_ok, findings = verify_redaction_clean(
        res.output_path,
        clean_terms,
        regions_by_page=regions_by_page if regions_by_page else None,
    )
    if not clean_ok:
        return ToolResult(
            success=False,
            output_paths=[],
            summary=f"Redaction verification failed: {findings}",
            details={"error": "redaction_verification_failed", "findings": findings},
        )

    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[res.output_path],
        summary=f"Destructively redacted {res.regions_applied} region(s) across {res.pages_modified} page(s).",
        details={
            "pages_modified": res.pages_modified,
            "regions_applied": res.regions_applied,
            "removed_text_ops": res.removed_text_ops,
            "removed_vector_ops": res.removed_vector_ops,
            "removed_image_ops": res.removed_image_ops,
            "removed_annots": res.removed_annots,
            "verified_clean": True,
        },
    )


def _handle_strip_metadata(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    from ..metadata_sanitizer import SanitizeOptions, sanitize_pdf_metadata

    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("strip_metadata", err_out, "invalid_output")

    opts = SanitizeOptions(
        strip_info=bool(params.get("strip_info", True)),
        strip_xmp=bool(params.get("strip_xmp", True)),
        strip_piece_info=bool(params.get("strip_piece_info", True)),
        strip_hidden_app_data=bool(params.get("strip_hidden_app_data", True)),
        strip_struct_tree=bool(params.get("strip_struct_tree", False)),
        strip_embedded_files=bool(params.get("strip_embedded_files", False)),
    )

    emit_progress(progress_cb, 40, "Sanitizing PDF metadata")
    res = sanitize_pdf_metadata(inputs[0], out_path, options=opts)
    emit_progress(progress_cb, 100, "Done")
    return ToolResult(
        success=True,
        output_paths=[res.output_path],
        summary=f"Sanitized metadata ({len(res.removed_info_keys)} Info keys, {res.removed_xmp_streams} XMP stream(s)).",
        details={
            "removed_info_keys": res.removed_info_keys,
            "removed_xmp_streams": res.removed_xmp_streams,
            "removed_piece_info_count": res.removed_piece_info_count,
            "removed_struct_tree": res.removed_struct_tree,
            "remaining_info_keys": res.remaining_info_keys,
            "remaining_xmp": res.remaining_xmp,
            "remaining_piece_info": res.remaining_piece_info,
        },
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="protect_pdf",
        category="security",
        icon="shield",
        badge="AES-256",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.security", "_handle_protect_pdf"),
        params=[
            ToolParamSpec("user_password", "str", ""),
            ToolParamSpec("owner_password", "str", ""),
            ToolParamSpec("allow_print", "bool", True),
            ToolParamSpec("allow_copy", "bool", True),
            ToolParamSpec("allow_modify", "bool", False),
            ToolParamSpec("allow_annotate", "bool", True),
            ToolParamSpec("encrypt_metadata", "bool", True),
        ],
    ),
    ToolDefinition(
        id="unlock_pdf",
        category="security",
        icon="shield",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.security", "_handle_unlock_pdf"),
        params=[ToolParamSpec("password", "str", "")],
    ),
    ToolDefinition(
        id="sign_pdf",
        category="security",
        icon="sign",
        badge="PKI",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.security", "_handle_sign_pdf"),
        params=[
            ToolParamSpec("p12_path", "path", ""),
            ToolParamSpec("password", "str", ""),
            ToolParamSpec("reason", "str", "Signed with PDF/MD Lector by TRODAI"),
            ToolParamSpec("location", "str", ""),
            ToolParamSpec("contact_info", "str", ""),
            ToolParamSpec("field_name", "str", ""),
            ToolParamSpec("page", "int", 1, min_val=1),
            ToolParamSpec("visual", "bool", False),
            ToolParamSpec("rect", "any", None),
        ],
    ),
    ToolDefinition(
        id="redact_pdf",
        category="security",
        icon="shield",
        badge="REAL",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.security", "_handle_redact_pdf"),
        params=[
            ToolParamSpec("targets", "list", []),
            ToolParamSpec("search_terms", "list", []),
            ToolParamSpec("match_case", "bool", False),
            ToolParamSpec("match_whole_word", "bool", False),
            ToolParamSpec("strip_metadata_on_save", "bool", False),
        ],
    ),
    ToolDefinition(
        id="strip_metadata",
        category="security",
        icon="shield",
        min_inputs=1,
        max_inputs=1,
        handler=("pdf_lector.core.tools.security", "_handle_strip_metadata"),
        params=[
            ToolParamSpec("strip_info", "bool", True),
            ToolParamSpec("strip_xmp", "bool", True),
            ToolParamSpec("strip_piece_info", "bool", True),
            ToolParamSpec("strip_hidden_app_data", "bool", True),
            ToolParamSpec("strip_struct_tree", "bool", False),
            ToolParamSpec("strip_embedded_files", "bool", False),
        ],
    ),
)


def register_tools(registry: type[ToolRegistry]) -> None:
    """Register all 5 Security & Privacy tools."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn)
