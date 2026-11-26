"""Modular headless PDF tool registry, parameter specs, and shared security helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import dataclass, field
import importlib
import importlib.util
import inspect
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, ClassVar, Literal
import uuid

from ..path_guard import (
    _has_control_chars,
    _has_ntfs_ads,
    is_unc_or_network_path,
    validate_local_any_doc,
    validate_local_glossary_path,
    validate_local_media_path,
    validate_local_pdf_path,
)
from ..temp_manager import TempManager, secure_unlink
from ..win32_bridge import is_cloud_only_file, is_reparse_or_system_file

STANDARD_DPI_CHOICES: tuple[int, ...] = (72, 150, 200, 300, 600)
STANDARD_DPI_MIN: int = 36
STANDARD_DPI_MAX: int = 1200
STANDARD_QUALITY_MIN: int = 10
STANDARD_QUALITY_MAX: int = 100
STANDARD_SOURCE_LANGS: tuple[str, ...] = ("auto", "es", "en", "pt", "fr", "de", "it", "multi")

_SHARED_TEMP_MANAGER_LOCK = threading.Lock()
_SHARED_TEMP_MANAGER: TempManager | None = None


def get_shared_temp_manager() -> TempManager:
    """Return process-scoped TempManager instance (lazily initialized)."""
    global _SHARED_TEMP_MANAGER
    if _SHARED_TEMP_MANAGER is None:
        with _SHARED_TEMP_MANAGER_LOCK:
            if _SHARED_TEMP_MANAGER is None:
                _SHARED_TEMP_MANAGER = TempManager(register_atexit=True)
    return _SHARED_TEMP_MANAGER


def emit_progress(progress_cb: Callable[..., Any] | None, pct: int, message: str = "") -> None:
    """Safely invoke a progress callback supporting both 1-arg (pct) and 2-arg (pct, msg)."""
    if progress_cb is None:
        return
    clamped = max(0, min(100, int(pct)))
    try:
        sig = inspect.signature(progress_cb)
        params = list(sig.parameters.values())
        accepts_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        positional_count = sum(
            1
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )
        if accepts_varargs or positional_count >= 2:
            progress_cb(clamped, message)
        elif positional_count == 1:
            progress_cb(clamped)
        else:
            progress_cb()
    except (ValueError, TypeError):
        try:
            progress_cb(clamped, message)
        except TypeError:
            progress_cb(clamped)


def is_unsafe_reparse_or_system(p: Path) -> bool:
    """Check symlink/reparse/system status safely even when `p` does not exist yet."""
    try:
        if p.is_symlink():
            return True
        if p.exists():
            return is_reparse_or_system_file(p)
    except OSError:
        return True
    return False


_is_unsafe_reparse_or_system = is_unsafe_reparse_or_system


def make_sibling_stage_path(target_path: Path, suffix: str | None = None) -> Path:
    """Create a hidden staging file in target_path.parent for same-volume atomic os.replace."""
    import tempfile

    target_path.parent.mkdir(parents=True, exist_ok=True)
    ext = target_path.suffix if suffix is None else suffix
    prefix = f".{target_path.stem}.{os.getpid()}."
    fd, raw_path = tempfile.mkstemp(suffix=f".tmp{ext}", prefix=prefix, dir=str(target_path.parent))
    os.close(fd)
    return Path(raw_path)


def atomic_replace_file(stage_path: Path, target_path: Path, retries: int = 5) -> None:
    """Atomically replace target_path with stage_path using Windows AV-lock retry backoff."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: OSError | None = None
    for attempt in range(max(1, retries)):
        try:
            os.replace(stage_path, target_path)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.06 * (attempt + 1))
        except OSError as exc:
            last_err = exc
            break
    if last_err is not None:
        raise last_err


def validate_local_ext_path(
    raw: str | Path,
    allowed_extensions: set[str] | frozenset[str] | None = None,
    *,
    check_exists: bool = True,
) -> tuple[bool, Path | None, str]:
    """Validate an arbitrary local file path via path_guard primitives."""
    if raw is None:
        return False, None, "empty_path"
    text = str(raw).strip()
    if not text:
        return False, None, "empty_path"
    if _has_control_chars(text):
        return False, None, "invalid_characters"
    if is_unc_or_network_path(text):
        return False, None, "network_or_url_blocked"

    p = Path(text)
    if p.drive and not p.is_absolute():
        return False, None, "invalid_characters"
    if _has_ntfs_ads(text):
        return False, None, "invalid_characters"

    if allowed_extensions is not None:
        exts = {e.lower() for e in allowed_extensions}
        if p.suffix.lower() not in exts:
            return False, None, "unsupported_extension"

    if _is_unsafe_reparse_or_system(p):
        return False, None, "reparse_or_system_blocked"

    if check_exists:
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            return False, None, "file_not_found"
        if not resolved.is_file():
            return False, None, "not_a_file"
        if is_unc_or_network_path(resolved) or _is_unsafe_reparse_or_system(resolved):
            return False, None, "network_or_url_blocked"
        if is_cloud_only_file(resolved):
            return False, None, "cloud_only_blocked"
        return True, resolved, "ok"

    return True, p, "ok"


def validate_output_pdf_path(raw: str | Path) -> tuple[bool, Path | None, str]:
    """Validate an output PDF path (non-existent allowed, rejects UNC/traversal/dir)."""
    ok, reason = validate_local_pdf_path(raw, check_exists=False)
    if not ok:
        return False, None, reason
    p = Path(str(raw).strip())
    if _is_unsafe_reparse_or_system(p):
        return False, None, "reparse_or_system_blocked"
    if p.exists() and p.is_dir():
        return False, None, "output_is_directory"
    return True, p, "ok"


def validate_output_dir_path(raw: str | Path) -> tuple[bool, Path | None, str]:
    """Validate an output directory path (rejects UNC/URL/ADS/existing regular file)."""
    if raw is None:
        return False, None, "empty_path"
    text = str(raw).strip()
    if not text:
        return False, None, "empty_path"
    if _has_control_chars(text):
        return False, None, "invalid_characters"
    if is_unc_or_network_path(text):
        return False, None, "network_or_url_blocked"
    p = Path(text)
    if p.drive and not p.is_absolute():
        return False, None, "invalid_characters"
    if _has_ntfs_ads(text):
        return False, None, "invalid_characters"
    if _is_unsafe_reparse_or_system(p):
        return False, None, "reparse_or_system_blocked"
    if p.exists() and p.is_file():
        return False, None, "output_is_file"
    return True, p, "ok"


def validate_output_any_path(raw: str | Path) -> tuple[bool, Path | None, str]:
    """Validate an output file or directory path against UNC/URL/ADS/reparse rules."""
    if raw is None:
        return False, None, "empty_path"
    text = str(raw).strip()
    if not text:
        return False, None, "empty_path"
    if _has_control_chars(text):
        return False, None, "invalid_characters"
    if is_unc_or_network_path(text):
        return False, None, "network_or_url_blocked"
    p = Path(text)
    if p.drive and not p.is_absolute():
        return False, None, "invalid_characters"
    if _has_ntfs_ads(text):
        return False, None, "invalid_characters"
    if _is_unsafe_reparse_or_system(p):
        return False, None, "reparse_or_system_blocked"
    return True, p, "ok"


@dataclass(frozen=True)
class ToolParamSpec:
    name: str
    kind: Literal["int", "float", "str", "choice", "bool", "list", "dict", "json", "path", "any"]
    default: Any
    choices: tuple[Any, ...] | list[Any] | None = None
    min_val: int | float | None = None
    max_val: int | float | None = None
    label: str | None = None
    description: str | None = None
    allow_custom: bool = False

    def validate(self, value: Any) -> tuple[bool, Any, str]:
        """Coerce and validate a single parameter value."""
        if value is None:
            if self.default is None or self.kind in ("list", "dict", "json", "any"):
                return True, copy.deepcopy(self.default), "ok"
            if self.kind in ("int", "float", "bool", "str", "choice", "path"):
                return True, copy.deepcopy(self.default), "ok"

        if self.kind == "int":
            if isinstance(value, bool):
                return False, value, f"{self.name}: boolean is not a valid int"
            try:
                if isinstance(value, float) and not value.is_integer():
                    return False, value, f"{self.name}: fractional float is not a valid int"
                if isinstance(value, str):
                    stripped = value.strip()
                    if not stripped:
                        return False, value, f"{self.name}: empty int string"
                    norm = int(stripped)
                else:
                    norm = int(value)
            except (TypeError, ValueError):
                return False, value, f"{self.name}: invalid integer {value!r}"
            if self.min_val is not None and norm < self.min_val:
                return False, value, f"{self.name}: {norm} < min {self.min_val}"
            if self.max_val is not None and norm > self.max_val:
                return False, value, f"{self.name}: {norm} > max {self.max_val}"
            if self.choices is not None and not self.allow_custom and norm not in self.choices:
                return False, value, f"{self.name}: {norm} not in {tuple(self.choices)}"
            return True, norm, "ok"

        if self.kind == "float":
            if isinstance(value, bool):
                return False, value, f"{self.name}: boolean is not a valid float"
            try:
                norm_f = float(value)
            except (TypeError, ValueError):
                return False, value, f"{self.name}: invalid float {value!r}"
            if math.isnan(norm_f) or math.isinf(norm_f):
                return False, value, f"{self.name}: non-finite float"
            if self.min_val is not None and norm_f < float(self.min_val):
                return False, value, f"{self.name}: {norm_f} < min {self.min_val}"
            if self.max_val is not None and norm_f > float(self.max_val):
                return False, value, f"{self.name}: {norm_f} > max {self.max_val}"
            if self.choices is not None and not self.allow_custom and norm_f not in self.choices:
                return False, value, f"{self.name}: {norm_f} not in {tuple(self.choices)}"
            return True, norm_f, "ok"

        if self.kind == "bool":
            if isinstance(value, bool):
                return True, value, "ok"
            if isinstance(value, int) and value in (0, 1):
                return True, bool(value), "ok"
            if isinstance(value, str):
                low = value.strip().lower()
                if low in ("true", "1", "yes", "si", "on"):
                    return True, True, "ok"
                if low in ("false", "0", "no", "off"):
                    return True, False, "ok"
            return False, value, f"{self.name}: invalid bool {value!r}"

        if self.kind in ("str", "choice"):
            if not isinstance(value, str):
                return False, value, f"{self.name}: expected str, got {type(value).__name__}"
            if self.choices is not None and not self.allow_custom and value not in self.choices:
                return False, value, f"{self.name}: {value!r} not in {tuple(self.choices)}"
            return True, value, "ok"

        if self.kind == "path":
            if isinstance(value, Path):
                return True, str(value), "ok"
            if isinstance(value, str):
                return True, value, "ok"
            return False, value, f"{self.name}: expected path/str"

        if self.kind == "list":
            if isinstance(value, (list, tuple)):
                return True, list(value), "ok"
            return False, value, f"{self.name}: expected list"

        if self.kind == "dict":
            if isinstance(value, Mapping):
                return True, dict(value), "ok"
            return False, value, f"{self.name}: expected dict"

        if self.kind == "json":
            if isinstance(value, (list, tuple, dict, str, int, float, bool)):
                return True, copy.deepcopy(value), "ok"
            return False, value, f"{self.name}: invalid json value"

        # kind == "any"
        return True, value, "ok"


def dpi_param_spec(default: int = 150) -> ToolParamSpec:
    return ToolParamSpec(
        name="dpi",
        kind="int",
        default=default,
        choices=STANDARD_DPI_CHOICES,
        min_val=STANDARD_DPI_MIN,
        max_val=STANDARD_DPI_MAX,
        label="DPI",
        description="Output resolution in DPI (presets 72-600, custom 36-1200)",
        allow_custom=True,
    )


def quality_param_spec(default: int = 75) -> ToolParamSpec:
    return ToolParamSpec(
        name="quality",
        kind="int",
        default=default,
        min_val=STANDARD_QUALITY_MIN,
        max_val=STANDARD_QUALITY_MAX,
        label="Quality",
        description="Compression or image quality (10-100)",
    )


def source_lang_param_spec(default: str = "auto") -> ToolParamSpec:
    return ToolParamSpec(
        name="source_lang",
        kind="str",
        default=default,
        choices=STANDARD_SOURCE_LANGS,
        label="Source language",
        description="Original document language for OCR/extraction/conversion",
    )


@dataclass
class ToolResult:
    success: bool
    output_paths: list[Path] = field(default_factory=list)
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    id: str
    category: str
    params: list[ToolParamSpec] = field(default_factory=list)
    handler: Callable[..., ToolResult] | tuple[str, str] | None = None
    title_key: str = ""
    desc_key: str = ""
    icon: str = ""
    badge: str = ""
    min_inputs: int = 1
    max_inputs: int | None = None
    input_validator: str = "pdf"  # "pdf" | "any_doc" | "glossary" | "media" | "none"
    output_kind: Literal["file", "dir", "any"] = "file"
    allow_empty_output: bool = False

    def __post_init__(self) -> None:
        if not self.title_key:
            self.title_key = f"tool.{self.id}"
        if not self.desc_key:
            self.desc_key = f"tool.{self.id}.desc"

    def validate_params(self, params: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any], str]:
        """Merge caller params with defaults and validate every declared ToolParamSpec."""
        if params is None:
            raw: dict[str, Any] = {}
        elif isinstance(params, Mapping):
            raw = dict(params)
        else:
            return False, {}, "params must be a dictionary"

        merged: dict[str, Any] = {}
        spec_map = {spec.name: spec for spec in self.params}
        for spec in self.params:
            if spec.name in raw:
                ok, norm, err = spec.validate(raw[spec.name])
                if not ok:
                    return False, {}, err
                merged[spec.name] = norm
            else:
                merged[spec.name] = copy.deepcopy(spec.default)

        for key, val in raw.items():
            if key not in spec_map:
                merged[key] = val

        return True, merged, "ok"

    def _resolve_handler(self) -> Callable[..., ToolResult]:
        if callable(self.handler):
            return self.handler
        if isinstance(self.handler, tuple) and len(self.handler) == 2:
            mod_name, fn_name = self.handler
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            self.handler = fn
            return fn
        raise RuntimeError(f"Tool {self.id!r} has no callable handler configured")

    def _validate_inputs(self, inputs: Sequence[Path | str] | None) -> tuple[bool, list[Path], str]:
        if inputs is None:
            raw_list: list[Path | str] = []
        elif isinstance(inputs, (str, Path)):
            raw_list = [inputs]
        else:
            raw_list = list(inputs)

        if len(raw_list) < self.min_inputs:
            return False, [], f"Tool '{self.id}' requires at least {self.min_inputs} input(s)"
        if self.max_inputs is not None and len(raw_list) > self.max_inputs:
            return False, [], f"Tool '{self.id}' accepts at most {self.max_inputs} input(s)"

        validated: list[Path] = []
        for item in raw_list:
            if item is None or not str(item).strip():
                return False, [], "empty_path"
            if _has_control_chars(str(item)):
                return False, [], "invalid_characters"
            if is_unc_or_network_path(item):
                return False, [], "network_or_url_blocked"

            if self.input_validator == "pdf":
                ok, reason = validate_local_pdf_path(item, check_exists=True)
                if not ok:
                    return False, [], f"Invalid input PDF ({reason}): {item}"
                p = Path(str(item).strip()).resolve()
            elif self.input_validator == "any_doc":
                ok, reason = validate_local_any_doc(item, check_exists=True)
                if not ok:
                    return False, [], f"Invalid input document ({reason}): {item}"
                p = Path(str(item).strip()).resolve()
            elif self.input_validator == "glossary":
                ok, reason = validate_local_glossary_path(item, check_exists=True)
                if not ok:
                    return False, [], f"Invalid input glossary ({reason}): {item}"
                p = Path(str(item).strip()).resolve()
            elif self.input_validator == "media":
                ok, reason = validate_local_media_path(item, check_exists=True)
                if not ok:
                    return False, [], f"Invalid input media ({reason}): {item}"
                p = Path(str(item).strip()).resolve()
            else:
                ok, p_resolved, reason = validate_local_ext_path(item, allowed_extensions=None, check_exists=True)
                if not ok or p_resolved is None:
                    return False, [], f"Invalid input file ({reason}): {item}"
                p = p_resolved

            if _is_unsafe_reparse_or_system(p):
                return False, [], f"Reparse/system file blocked: {item}"
            validated.append(p)

        return True, validated, "ok"

    def _validate_output(self, output: Path | str | None) -> tuple[bool, Path | None, str]:
        if output is None:
            if self.allow_empty_output or (self.output_kind == "any" and self.min_inputs == 0):
                return True, None, "ok"
            return False, None, "empty_output_path"

        if self.output_kind == "dir":
            return validate_output_dir_path(output)
        if self.output_kind == "file":
            ok, out_p, reason = validate_output_any_path(output)
            if not ok or out_p is None:
                return False, None, reason
            if out_p.exists() and out_p.is_dir():
                return False, None, "output_is_directory"
            return True, out_p, "ok"
        return validate_output_any_path(output)

    def run(
        self,
        inputs: Sequence[Path | str] | None,
        output: Path | str | None,
        params: dict[str, Any] | None = None,
        progress_cb: Callable[..., Any] | None = None,
    ) -> ToolResult:
        """Validate inputs, output, and params, execute handler safely, and return ToolResult."""
        try:
            ok_p, merged_params, err_p = self.validate_params(params)
            if not ok_p:
                return ToolResult(
                    success=False,
                    output_paths=[],
                    summary=f"Invalid parameters for {self.id}: {err_p}",
                    details={"error": "invalid_params", "reason": err_p},
                )

            ok_in, in_paths, err_in = self._validate_inputs(inputs)
            if not ok_in:
                return ToolResult(
                    success=False,
                    output_paths=[],
                    summary=f"Invalid inputs for {self.id}: {err_in}",
                    details={"error": "invalid_inputs", "reason": err_in},
                )

            ok_out, out_path, err_out = self._validate_output(output)
            if not ok_out:
                return ToolResult(
                    success=False,
                    output_paths=[],
                    summary=f"Invalid output for {self.id}: {err_out}",
                    details={"error": "invalid_output", "reason": err_out},
                )

            handler_fn = self._resolve_handler()
            result = handler_fn(in_paths, out_path, merged_params, progress_cb=progress_cb)
            if not isinstance(result, ToolResult):
                return ToolResult(
                    success=False,
                    output_paths=[],
                    summary=f"Tool '{self.id}' returned unexpected result type",
                    details={"error": "invalid_tool_result"},
                )
            return result
        except Exception as exc:
            return ToolResult(
                success=False,
                output_paths=[],
                summary=f"Tool '{self.id}' failed: {exc}",
                details={"error": type(exc).__name__, "reason": str(exc)},
            )


class ToolRegistry:
    """Thread-safe registry for all PDF/MD Lector tools with lazy M2-B discovery."""

    _lock: ClassVar[threading.RLock] = threading.RLock()
    _tools: ClassVar[dict[str, ToolDefinition]] = {}
    _loaded_modules: ClassVar[set[str]] = set()
    _M2A_MODULES: ClassVar[tuple[str, ...]] = (
        "pdf_lector.core.tools.organize",
        "pdf_lector.core.tools.optimize",
        "pdf_lector.core.tools.security",
        "pdf_lector.core.tools.compare",
    )
    _M2B_MODULES: ClassVar[tuple[str, ...]] = (
        "pdf_lector.core.tools.convert",
        "pdf_lector.core.tools.create",
        "pdf_lector.core.tools.invoices",
    )

    @classmethod
    def _load_module(cls, mod_name: str) -> None:
        if mod_name in cls._loaded_modules:
            return
        mod = importlib.import_module(mod_name)
        reg_fn = getattr(mod, "register_tools", None)
        if callable(reg_fn):
            reg_fn(cls)
        else:
            defs = getattr(mod, "TOOL_DEFINITIONS", ())
            for defn in defs:
                cls.register_tool(defn)
        cls._loaded_modules.add(mod_name)

    @classmethod
    def _ensure_loaded(cls) -> None:
        with cls._lock:
            for mod_name in cls._M2A_MODULES:
                if mod_name not in cls._loaded_modules:
                    cls._load_module(mod_name)
            for mod_name in cls._M2B_MODULES:
                if mod_name not in cls._loaded_modules:
                    if importlib.util.find_spec(mod_name) is not None:
                        cls._load_module(mod_name)

    @classmethod
    def register(cls, definition: ToolDefinition, *, overwrite: bool = True) -> ToolDefinition:
        return cls.register_tool(definition, overwrite=overwrite)

    @classmethod
    def register_tool(cls, definition: ToolDefinition, *, overwrite: bool = True) -> ToolDefinition:
        with cls._lock:
            if not overwrite and definition.id in cls._tools:
                return cls._tools[definition.id]
            cls._tools[definition.id] = definition
            return definition

    @classmethod
    def get(cls, tool_id: str) -> ToolDefinition:
        cls._ensure_loaded()
        with cls._lock:
            if tool_id not in cls._tools:
                raise KeyError(f"Unknown tool_id: {tool_id!r}")
            return cls._tools[tool_id]

    @classmethod
    def has_tool(cls, tool_id: str) -> bool:
        cls._ensure_loaded()
        with cls._lock:
            return tool_id in cls._tools

    @classmethod
    def list_ids(cls) -> list[str]:
        cls._ensure_loaded()
        with cls._lock:
            return list(cls._tools.keys())

    @classmethod
    def list_tools(cls, category: str | None = None) -> list[ToolDefinition]:
        cls._ensure_loaded()
        with cls._lock:
            items = list(cls._tools.values())
            if category is not None:
                items = [d for d in items if d.category == category]
            return items

    @classmethod
    def run(
        cls,
        tool_id: str,
        inputs: Sequence[Path | str] | None,
        output: Path | str | None,
        params: dict[str, Any] | None = None,
        progress_cb: Callable[..., Any] | None = None,
    ) -> ToolResult:
        defn = cls.get(tool_id)
        return defn.run(inputs, output, params=params, progress_cb=progress_cb)


def register_tool(definition: ToolDefinition, *, overwrite: bool = True) -> ToolDefinition:
    """Convenience module-level alias for ToolRegistry.register_tool."""
    return ToolRegistry.register_tool(definition, overwrite=overwrite)


__all__ = [
    "STANDARD_DPI_CHOICES",
    "STANDARD_DPI_MAX",
    "STANDARD_DPI_MIN",
    "STANDARD_QUALITY_MAX",
    "STANDARD_QUALITY_MIN",
    "STANDARD_SOURCE_LANGS",
    "ToolDefinition",
    "ToolParamSpec",
    "ToolRegistry",
    "ToolResult",
    "atomic_replace_file",
    "dpi_param_spec",
    "emit_progress",
    "get_shared_temp_manager",
    "is_unsafe_reparse_or_system",
    "make_sibling_stage_path",
    "quality_param_spec",
    "register_tool",
    "source_lang_param_spec",
    "validate_local_ext_path",
    "validate_output_any_path",
    "validate_output_dir_path",
    "validate_output_pdf_path",
]
