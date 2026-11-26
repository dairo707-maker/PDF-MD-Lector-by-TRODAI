"""Theme profile model, presets, atomic persistence, and .trodai-theme.json portability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from ..core.path_guard import validate_local_media_path

VALID_MODES = ("dark", "light")
VALID_MOTION_LEVELS = ("static", "subtle", "modern")
VALID_MOTION_PRESETS = ("none", "polygonal_mesh", "fluid_aurora", "arch_grid")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        tmp_path = Path(tmp_name)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def get_theme_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector" / "theme_config.json"


@dataclass
class ThemeProfile:
    schema_version: int = 1
    name: str = "TRODAI Corporativo"
    base_mode: str = "dark"
    accent_override: str = ""
    motion_level: str = "static"
    motion_preset: str = "none"
    banner_path: str = ""
    apply_to_hub_banner: bool = True
    apply_to_viewer_backdrop: bool = False
    opacity: float = 0.55
    blur_radius: int = 4
    auto_scrim: bool = True
    fps_cap: int = 60
    pause_on_battery_saver: bool = True
    language: str = "es"

    def sanitized(self, *, check_media_exists: bool = False) -> ThemeProfile:
        mode = self.base_mode if self.base_mode in VALID_MODES else "dark"
        m_level = self.motion_level if self.motion_level in VALID_MOTION_LEVELS else "static"
        m_preset = self.motion_preset if self.motion_preset in VALID_MOTION_PRESETS else "none"
        fps = 30 if int(self.fps_cap) <= 30 else 60
        opacity = max(0.10, min(0.85, float(self.opacity)))
        blur = max(0, min(24, int(self.blur_radius)))

        accent = self.accent_override.strip()
        if accent:
            if not (len(accent) == 7 and accent.startswith("#")):
                accent = ""
            else:
                try:
                    int(accent[1:], 16)
                except ValueError:
                    accent = ""

        clean_banner = ""
        if self.banner_path and str(self.banner_path).strip():
            ok, _ = validate_local_media_path(str(self.banner_path).strip(), check_exists=check_media_exists)
            if ok:
                clean_banner = str(self.banner_path).strip()

        lang = self.language if self.language in ("es", "en", "pt") else "es"

        return ThemeProfile(
            schema_version=1,
            name=(self.name or "TRODAI Personalizado").strip()[:64],
            base_mode=mode,
            accent_override=accent,
            motion_level=m_level,
            motion_preset=m_preset,
            banner_path=clean_banner,
            apply_to_hub_banner=bool(self.apply_to_hub_banner),
            apply_to_viewer_backdrop=bool(self.apply_to_viewer_backdrop),
            opacity=round(opacity, 2),
            blur_radius=blur,
            auto_scrim=bool(self.auto_scrim),
            fps_cap=fps,
            pause_on_battery_saver=bool(self.pause_on_battery_saver),
            language=lang,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.sanitized(check_media_exists=False))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, check_media_exists: bool = False) -> ThemeProfile:
        fields = {
            "schema_version": int(raw.get("schema_version", 1)),
            "name": str(raw.get("name", "TRODAI Corporativo")),
            "base_mode": str(raw.get("base_mode", "dark")),
            "accent_override": str(raw.get("accent_override", "")),
            "motion_level": str(raw.get("motion_level", "static")),
            "motion_preset": str(raw.get("motion_preset", "none")),
            "banner_path": str(raw.get("banner_path", "")),
            "apply_to_hub_banner": bool(raw.get("apply_to_hub_banner", True)),
            "apply_to_viewer_backdrop": bool(raw.get("apply_to_viewer_backdrop", False)),
            "opacity": float(raw.get("opacity", 0.55)),
            "blur_radius": int(raw.get("blur_radius", 4)),
            "auto_scrim": bool(raw.get("auto_scrim", True)),
            "fps_cap": int(raw.get("fps_cap", 60)),
            "pause_on_battery_saver": bool(raw.get("pause_on_battery_saver", True)),
            "language": str(raw.get("language", "es")),
        }
        return cls(**fields).sanitized(check_media_exists=check_media_exists)


BUILTIN_PRESETS: dict[str, ThemeProfile] = {
    "trodai_corporate": ThemeProfile(
        name="TRODAI Corporativo",
        base_mode="dark",
        motion_level="static",
        motion_preset="none",
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=False,
        opacity=0.45,
        blur_radius=0,
        auto_scrim=True,
    ),
    "trodai_mesh": ThemeProfile(
        name="Malla Poligonal TRODAI",
        base_mode="dark",
        accent_override="#0EA5E9",
        motion_level="modern",
        motion_preset="polygonal_mesh",
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=True,
        opacity=0.65,
        blur_radius=0,
        auto_scrim=True,
    ),
    "editorial_aurora": ThemeProfile(
        name="Aurora Editorial",
        base_mode="dark",
        accent_override="#3B82F6",
        motion_level="modern",
        motion_preset="fluid_aurora",
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=True,
        opacity=0.62,
        blur_radius=6,
        auto_scrim=True,
    ),
    "arch_grid": ThemeProfile(
        name="Rejilla Arquitectonica",
        base_mode="light",
        accent_override="#2563EB",
        motion_level="modern",
        motion_preset="arch_grid",
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=True,
        opacity=0.58,
        blur_radius=0,
        auto_scrim=True,
    ),
    "custom_banner": ThemeProfile(
        name="Banner Personalizado",
        base_mode="dark",
        motion_level="subtle",
        motion_preset="polygonal_mesh",
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=True,
        opacity=0.72,
        blur_radius=4,
        auto_scrim=True,
    ),
}


def default_profile() -> ThemeProfile:
    return ThemeProfile(**asdict(BUILTIN_PRESETS["trodai_corporate"]))


def get_preset(preset_id: str, *, preserve_language: str = "es") -> ThemeProfile:
    template = BUILTIN_PRESETS.get(preset_id, BUILTIN_PRESETS["trodai_corporate"])
    data = asdict(template)
    data["language"] = preserve_language
    return ThemeProfile(**data).sanitized(check_media_exists=False)


def save_theme_profile(profile: ThemeProfile, path: Path | None = None) -> Path:
    target = path or get_theme_config_path()
    payload = json.dumps(profile.to_dict(), indent=2, ensure_ascii=False)
    _atomic_write_text(target, payload)
    return target


def load_theme_profile(path: Path | None = None) -> ThemeProfile:
    target = path or get_theme_config_path()
    if not target.exists():
        return default_profile()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return ThemeProfile.from_dict(raw, check_media_exists=True)
    except Exception:
        pass
    return default_profile()


def export_theme_package(profile: ThemeProfile, target_path: Path) -> Path:
    """Export theme profile to a portable .trodai-theme.json package."""
    target = Path(target_path)
    payload = json.dumps(profile.to_dict(), indent=2, ensure_ascii=False)
    _atomic_write_text(target, payload)
    return target


def import_theme_package(source_path: Path) -> tuple[ThemeProfile, str | None]:
    """Import .trodai-theme.json package safely, validating any referenced local media path."""
    raw = json.loads(Path(source_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid theme package format")

    warning: str | None = None
    raw_banner = str(raw.get("banner_path", "")).strip()
    if raw_banner:
        ok, reason = validate_local_media_path(raw_banner, check_exists=True)
        if not ok:
            warning = reason

    profile = ThemeProfile.from_dict(raw, check_media_exists=True)
    return profile, warning
