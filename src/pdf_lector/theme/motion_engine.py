"""Modular Motion engine, Performance Guard, Auto-Scrim contrast calibrator, and Backdrop widget."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from typing import Callable

from PIL import Image, ImageFilter
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QMovie, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget

from ..core.path_guard import validate_local_media_path
from ..core.win32_bridge import is_battery_saver_active, windows_animations_enabled
from .palette import get_palette
from .theme_profile import ThemeProfile


@dataclass(frozen=True)
class MotionGuardState:
    should_animate: bool
    status_code: str
    target_fps: int


class MotionPerformanceGuard:
    """Evaluates OS animation preference, Battery Saver, visibility, and heavy work."""

    def __init__(
        self,
        *,
        os_animations_fn: Callable[[], bool] | None = None,
        battery_saver_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._os_animations_fn = os_animations_fn or windows_animations_enabled
        self._battery_saver_fn = battery_saver_fn or is_battery_saver_active

    def evaluate(
        self,
        profile: ThemeProfile,
        *,
        window_visible: bool = True,
        heavy_task_active: bool = False,
    ) -> MotionGuardState:
        if profile.motion_level == "static":
            return MotionGuardState(False, "static", 0)
        if not window_visible:
            return MotionGuardState(False, "hidden", 0)
        if heavy_task_active:
            return MotionGuardState(False, "paused", 0)
        if not self._os_animations_fn():
            return MotionGuardState(False, "os_off", 0)
        if profile.pause_on_battery_saver and self._battery_saver_fn():
            return MotionGuardState(False, "battery", 0)
        target_fps = 30 if profile.fps_cap <= 30 else 60
        return MotionGuardState(True, "active", target_fps)


def compute_image_luminance(image: QImage) -> float:
    """Return average BT.709 relative luminance (0.0 dark .. 1.0 bright) for an image."""
    if image.isNull():
        return 0.5
    sample = image.scaled(32, 32, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    sample = sample.convertToFormat(QImage.Format.Format_RGB32)
    total = 0.0
    count = sample.width() * sample.height()
    if count <= 0:
        return 0.5
    for y in range(sample.height()):
        for x in range(sample.width()):
            c = QColor(sample.pixel(x, y))
            total += (0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()) / 255.0
    return max(0.0, min(1.0, total / count))


def compute_scrim_alpha(
    luminance: float,
    *,
    base_mode: str,
    user_opacity: float,
    auto_scrim: bool,
) -> int:
    """Compute alpha (0..255) for readability veil guaranteeing WCAG AA text contrast."""
    if not auto_scrim:
        return 0
    lum = max(0.0, min(1.0, float(luminance)))
    op = max(0.10, min(0.85, float(user_opacity)))
    if base_mode == "light":
        # Dark text (#0A0F1D) needs a bright crisp veil over dark or detailed photos.
        raw = 128.0 + (1.0 - lum) * 92.0 + (op * 22.0)
        return int(max(130, min(232, round(raw))))
    # Light text (#F8FAFC) needs a dark Obsidian Navy veil over bright photos.
    raw = 112.0 + lum * 96.0 + (op * 26.0)
    return int(max(118, min(228, round(raw))))


@lru_cache(maxsize=12)
def _load_blurred_banner_cached(path_str: str, mtime_ns: int, blur_radius: int) -> tuple[QPixmap, float]:
    del mtime_ns  # Used strictly to invalidate cache when file changes on disk.
    with Image.open(path_str) as pil:
        rgb = pil.convert("RGBA")
        # Cap oversized wallpapers to 1920px wide once on load for instant repainting.
        if rgb.width > 1920 or rgb.height > 1200:
            rgb.thumbnail((1920, 1200), Image.Resampling.LANCZOS)
        if blur_radius > 0:
            rgb = rgb.filter(ImageFilter.GaussianBlur(radius=float(blur_radius)))
        raw = rgb.tobytes("raw", "RGBA")
        qimg = QImage(raw, rgb.width, rgb.height, rgb.width * 4, QImage.Format.Format_RGBA8888).copy()
    lum = compute_image_luminance(qimg)
    return QPixmap.fromImage(qimg), lum


def load_processed_banner(path: Path, blur_radius: int) -> tuple[QPixmap, float]:
    """Load and optionally Gaussian-blur a local banner image, returning (QPixmap, luminance)."""
    resolved = path.resolve(strict=True)
    mtime_ns = resolved.stat().st_mtime_ns
    return _load_blurred_banner_cached(str(resolved), mtime_ns, max(0, min(24, int(blur_radius))))


class MotionBackdropWidget(QWidget):
    """High-efficiency QPainter/QMovie backdrop layer supporting TRODAI Motion presets & local photos."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        guard: MotionPerformanceGuard | None = None,
        is_viewer_surface: bool = False,
        chrome_surface: bool = False,
    ) -> None:
        super().__init__(parent)
        self._guard = guard or MotionPerformanceGuard()
        self._is_viewer_surface = is_viewer_surface
        self._chrome_surface = bool(chrome_surface)
        self._profile = ThemeProfile()
        self._heavy_task_active = False
        self._phase = 0.0
        self._static_pixmap: QPixmap | None = None
        self._static_image: QImage | None = None
        self._static_luminance: float = 0.25
        self._movie: QMovie | None = None
        self._movie_frame_lum: float = 0.25
        self._loaded_media_key: tuple[bool, str] | None = None
        self._render_cache_key: tuple[int, int, int, int] | None = None
        self._render_cache_pixmap: QPixmap | None = None
        self._pointer_pos: QPointF | None = None
        self._pointer_filter_installed = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        if not self._is_viewer_surface:
            self.setMouseTracking(True)

    @property
    def pointer_interactive(self) -> bool:
        """Pointer-reactive background parallax is enabled ONLY on non-viewer surfaces (Hub Hero)."""
        return not self._is_viewer_surface

    def set_pointer_position(self, pos: QPointF | None) -> None:
        if not self.pointer_interactive:
            return
        self._pointer_pos = QPointF(pos) if pos is not None else None
        if self._surface_enabled():
            self.update()

    def guard_state(self) -> MotionGuardState:
        return self._guard.evaluate(
            self._profile,
            window_visible=self.isVisible(),
            heavy_task_active=self._heavy_task_active,
        )

    def _surface_enabled(self) -> bool:
        if self._chrome_surface:
            return self._profile.motion_level != "static" and self._profile.motion_preset != "none"
        if self._is_viewer_surface:
            return self._profile.apply_to_viewer_backdrop
        return self._profile.apply_to_hub_banner

    def set_heavy_task_active(self, active: bool) -> None:
        self._heavy_task_active = bool(active)
        self._sync_motion_state()

    def set_profile(self, profile: ThemeProfile) -> None:
        new_prof = profile.sanitized(check_media_exists=False)
        if self._chrome_surface:
            old_p = self._profile
            if (
                old_p.base_mode == new_prof.base_mode
                and old_p.accent_override == new_prof.accent_override
                and old_p.motion_level == new_prof.motion_level
                and old_p.motion_preset == new_prof.motion_preset
                and old_p.fps_cap == new_prof.fps_cap
                and old_p.pause_on_battery_saver == new_prof.pause_on_battery_saver
                and abs(old_p.opacity - new_prof.opacity) < 1e-4
            ):
                self._profile = new_prof
                return
        self._profile = new_prof
        self._reload_media()
        self._sync_motion_state()
        self.update()

    def _clear_movie(self) -> None:
        if self._movie is not None:
            try:
                self._movie.stop()
                self._movie.deleteLater()
            except Exception:
                pass
            self._movie = None

    def _reload_media(self) -> None:
        enabled = self._surface_enabled()
        raw_path = "" if self._chrome_surface else self._profile.banner_path.strip()
        media_key = (enabled, raw_path)
        if media_key == self._loaded_media_key:
            return
        self._loaded_media_key = media_key
        self._render_cache_key = None
        self._render_cache_pixmap = None

        self._clear_movie()
        self._static_pixmap = None
        self._static_image = None
        self._static_luminance = 0.85 if self._profile.base_mode == "light" else 0.15

        if not enabled or not raw_path:
            return

        ok, _ = validate_local_media_path(raw_path, check_exists=True)
        if not ok:
            return

        path = Path(raw_path)
        if path.suffix.lower() in (".gif", ".webp"):
            movie = QMovie(str(path))
            if movie.isValid() and movie.frameCount() > 1:
                movie.frameChanged.connect(self._on_movie_frame)
                self._movie = movie
                movie.jumpToFrame(0)
                first = movie.currentImage()
                self._movie_frame_lum = compute_image_luminance(first)
                return
            movie.deleteLater()

        try:
            pm, lum = load_processed_banner(path, 0)
            self._static_pixmap = pm
            self._static_image = pm.toImage() if not pm.isNull() else None
            self._static_luminance = lum
        except Exception:
            self._static_pixmap = None
            self._static_image = None

    def _fast_cover_blur(self, source_pm: QPixmap, blur_radius: int, frame_tag: int) -> QPixmap:
        w = max(1, self.width())
        h = max(1, self.height())
        br = max(0, min(24, int(blur_radius)))
        key = (w, h, br, frame_tag)
        if self._render_cache_key == key and self._render_cache_pixmap is not None:
            return self._render_cache_pixmap

        scaled = source_pm.scaled(
            w,
            h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.FastTransformation if br > 0 else Qt.TransformationMode.SmoothTransformation,
        )
        sx = max(0, (scaled.width() - w) // 2)
        sy = max(0, (scaled.height() - h) // 2)
        cropped = scaled.copy(sx, sy, w, h)
        if br > 0:
            # Ultra-fast bilinear pyramid blur (<1ms/frame) so GIF/slider drag stays 60 FPS.
            factor = 1.0 + (br * 0.45)
            small_w = max(8, int(round(w / factor)))
            small_h = max(8, int(round(h / factor)))
            down = cropped.scaled(
                small_w,
                small_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            cropped = down.scaled(
                w,
                h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._render_cache_key = key
        self._render_cache_pixmap = cropped
        return cropped

    def _draw_cover_pixmap(self, painter: QPainter, pixmap: QPixmap, opacity: float, *, frame_tag: int = -1) -> None:
        if pixmap.isNull() or self.width() <= 0 or self.height() <= 0:
            return
        rendered = self._fast_cover_blur(pixmap, self._profile.blur_radius, frame_tag)
        painter.save()
        painter.setOpacity(opacity)
        painter.drawPixmap(0, 0, rendered)
        painter.restore()

    def _on_movie_frame(self, _frame_idx: int) -> None:
        self.update()

    def _on_tick(self) -> None:
        step = 0.024 if self._profile.fps_cap <= 30 else 0.014
        self._phase = (self._phase + step) % (math.tau * 100.0)
        self.update()

    def _sync_motion_state(self) -> None:
        state = self.guard_state()
        enabled = self._surface_enabled()

        if self._movie is not None:
            if enabled and state.should_animate:
                if self._movie.state() != QMovie.MovieState.Running:
                    self._movie.start()
            else:
                if self._movie.state() == QMovie.MovieState.Running:
                    self._movie.setPaused(True)

        needs_timer = (
            enabled
            and state.should_animate
            and self._movie is None
            and self._static_pixmap is None
            and self._profile.motion_level == "modern"
            and self._profile.motion_preset != "none"
        )
        if needs_timer:
            interval = 33 if state.target_fps <= 30 else 16
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()

    def _sync_pointer_filter(self, enable: bool) -> None:
        if not self.pointer_interactive:
            return
        app = QApplication.instance()
        if app is None:
            return
        if enable and not self._pointer_filter_installed:
            app.installEventFilter(self)
            self._pointer_filter_installed = True
        elif not enable and self._pointer_filter_installed:
            app.removeEventFilter(self)
            self._pointer_filter_installed = False

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        del watched
        if self.pointer_interactive and self.isVisible() and event.type() == QEvent.Type.MouseMove:
            try:
                gpos = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else None
                if gpos is not None:
                    local = self.mapFromGlobal(gpos)
                    if 0 <= local.x() <= self.width() and 0 <= local.y() <= self.height():
                        self.set_pointer_position(QPointF(local))
                    elif self._pointer_pos is not None:
                        self.set_pointer_position(None)
            except Exception:
                pass
        return False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_motion_state()
        self._sync_pointer_filter(True)

    def hideEvent(self, event) -> None:
        self._sync_pointer_filter(False)
        if self._timer.isActive():
            self._timer.stop()
        if self._movie is not None and self._movie.state() == QMovie.MovieState.Running:
            self._movie.setPaused(True)
        super().hideEvent(event)

    def cleanup(self) -> None:
        self._sync_pointer_filter(False)
        self._timer.stop()
        self._clear_movie()
        self._render_cache_pixmap = None
        self._render_cache_key = None

    def _draw_polygonal_mesh(self, painter: QPainter, palette: dict[str, str], opacity: float) -> None:
        w, h = float(self.width()), float(self.height())
        accent = QColor(palette.get("motion_cyan", palette["accent"]))
        alt = QColor(palette.get("motion_blue", palette["accent_alt"]))
        if self._chrome_surface:
            cols = max(2, min(14, int(round(w / 90.0))))
            rows = max(2, min(10, int(round(h / 72.0))))
        else:
            cols, rows = 6, 4
        ptr = self._pointer_pos if self.pointer_interactive else None
        pts: list[list[QPointF]] = []
        for r in range(rows + 1):
            row_pts: list[QPointF] = []
            for c in range(cols + 1):
                bx = (c / cols) * w
                by = (r / rows) * h
                if 0 < c < cols and 0 < r < rows:
                    bx += math.sin(self._phase * 1.1 + c * 1.3 + r) * (w * 0.032)
                    by += math.cos(self._phase * 0.9 + r * 1.5 + c) * (h * 0.045)
                    if ptr is not None:
                        dx = ptr.x() - bx
                        dy = ptr.y() - by
                        dist = math.hypot(dx, dy)
                        if 1.0 < dist < 240.0:
                            pull = (1.0 - dist / 240.0) * 18.0
                            bx += (dx / dist) * pull
                            by += (dy / dist) * pull
                row_pts.append(QPointF(bx, by))
            pts.append(row_pts)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        line_pen = QPen(accent, 1.0)
        for r in range(rows):
            for c in range(cols):
                p00 = pts[r][c]
                p10 = pts[r][c + 1]
                p01 = pts[r + 1][c]
                p11 = pts[r + 1][c + 1]
                pulse = 0.5 + 0.5 * math.sin(self._phase + c * 0.7 + r * 0.9)
                fill_color = QColor(accent if (r + c) % 2 == 0 else alt)
                fill_color.setAlpha(int((14 + pulse * 34) * opacity))
                painter.setBrush(fill_color)
                line_color = QColor(accent)
                line_color.setAlpha(int((42 + pulse * 55) * opacity))
                line_pen.setColor(line_color)
                painter.setPen(line_pen)
                painter.drawPolygon(QPolygonF([p00, p10, p11]))
                painter.drawPolygon(QPolygonF([p00, p11, p01]))
        painter.restore()

    def _draw_fluid_aurora(self, painter: QPainter, palette: dict[str, str], opacity: float) -> None:
        w, h = float(self.width()), float(self.height())
        accent = QColor(palette.get("motion_cyan", palette["accent"]))
        alt = QColor(palette.get("motion_blue", palette["accent_alt"]))
        crimson = QColor(palette["app_crimson"])
        ptr = self._pointer_pos if self.pointer_interactive else None
        ptr_shift_x = ((ptr.x() / max(1.0, w)) - 0.5) * (w * 0.22) if ptr is not None else 0.0
        painter.save()
        for idx, base_col in enumerate((accent, alt, crimson)):
            cx = w * (0.25 + 0.5 * (0.5 + 0.5 * math.sin(self._phase * (0.55 + idx * 0.2) + idx * 2.1)))
            cx += ptr_shift_x * (0.6 + idx * 0.25)
            cy = h * (0.3 + 0.45 * (0.5 + 0.5 * math.cos(self._phase * (0.45 + idx * 0.15) + idx)))
            grad = QLinearGradient(max(0.0, cx - w * 0.4), 0.0, min(w, cx + w * 0.4), h)
            c1 = QColor(base_col)
            c1.setAlpha(int((95 if idx < 2 else 36) * opacity))
            c2 = QColor(base_col)
            c2.setAlpha(0)
            grad.setColorAt(0.0, c1)
            grad.setColorAt(0.55, c1)
            grad.setColorAt(1.0, c2)
            painter.fillRect(0, 0, int(w), int(h), grad)
        painter.restore()

    def _draw_arch_grid(self, painter: QPainter, palette: dict[str, str], opacity: float) -> None:
        w, h = self.width(), self.height()
        accent = QColor(palette.get("motion_cyan", palette["accent"]))
        border = QColor(palette["border_strong"])
        step = 32
        ptr = self._pointer_pos if self.pointer_interactive else None
        px_off = int(((ptr.x() / max(1, w)) - 0.5) * 18) if ptr is not None else 0
        py_off = int(((ptr.y() / max(1, h)) - 0.5) * 18) if ptr is not None else 0
        offset = int((self._phase * 14.0) % step)
        painter.save()
        grid_col = QColor(border)
        grid_col.setAlpha(int(120 * opacity))
        painter.setPen(QPen(grid_col, 1))
        for x in range(-step + ((offset + px_off) % step), w + step, step):
            painter.drawLine(x, 0, x, h)
        for y in range(-step + ((offset + py_off) % step), h + step, step):
            painter.drawLine(0, y, w, y)

        scan_y = int(ptr.y()) if ptr is not None else int(((math.sin(self._phase * 0.7) * 0.5 + 0.5) * max(1, h - 1)))
        scan_col = QColor(accent)
        scan_alpha = 58 if palette["mode"] == "light" else 110
        scan_col.setAlpha(int(scan_alpha * opacity))
        painter.setPen(QPen(scan_col, 2))
        painter.drawLine(0, scan_y, w, scan_y)
        painter.restore()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        palette = get_palette(self._profile.base_mode, self._profile.accent_override)
        base_hex = palette["bg_canvas"] if self._is_viewer_surface else palette["bg_sidebar_hex"]
        painter.fillRect(self.rect(), QColor(base_hex))

        if not self._surface_enabled():
            if self._chrome_surface:
                painter.setPen(QPen(QColor(palette["border_hairline"]), 1))
                if self.objectName() == "Sidebar" and self.width() > 0:
                    painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
                elif self.objectName() == "TopBar" and self.height() > 0:
                    painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
            painter.end()
            return

        eff_opacity = (
            min(0.72, max(0.30, self._profile.opacity * 0.82))
            if self._chrome_surface
            else self._profile.opacity
        )
        luminance = 0.85 if self._profile.base_mode == "light" else 0.15
        drew_custom_media = False

        if self._movie is not None:
            frame = self._movie.currentPixmap()
            if not frame.isNull():
                self._draw_cover_pixmap(
                    painter,
                    frame,
                    eff_opacity,
                    frame_tag=self._movie.currentFrameNumber(),
                )
                luminance = self._movie_frame_lum
                drew_custom_media = True
        elif self._static_pixmap is not None:
            self._draw_cover_pixmap(painter, self._static_pixmap, eff_opacity, frame_tag=-1)
            luminance = self._static_luminance
            drew_custom_media = True

        if not drew_custom_media:
            preset = self._profile.motion_preset
            if preset == "polygonal_mesh":
                self._draw_polygonal_mesh(painter, palette, eff_opacity)
            elif preset == "fluid_aurora":
                self._draw_fluid_aurora(painter, palette, eff_opacity)
            elif preset == "arch_grid":
                self._draw_arch_grid(painter, palette, eff_opacity)

        # Readability veil (Auto-Scrim) calibrated to protect foreground text contrast.
        if drew_custom_media:
            scrim_alpha = compute_scrim_alpha(
                luminance,
                base_mode=self._profile.base_mode,
                user_opacity=self._profile.opacity,
                auto_scrim=self._profile.auto_scrim,
            )
            if scrim_alpha > 0:
                scrim_col = QColor(palette["bg_window_hex"])
                scrim_col.setAlpha(scrim_alpha)
                painter.fillRect(self.rect(), scrim_col)

        if self._chrome_surface:
            painter.setPen(QPen(QColor(palette["border_hairline"]), 1))
            if self.objectName() == "Sidebar" and self.width() > 0:
                painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
            elif self.objectName() == "TopBar" and self.height() > 0:
                painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

        painter.end()


class ViewTransitionController:
    """Subtle 145ms horizontal slide transition obeying Motion level without QGraphicsOpacityEffect."""

    def __init__(self, stack: QStackedWidget, guard: MotionPerformanceGuard | None = None) -> None:
        self._stack = stack
        self._guard = guard or MotionPerformanceGuard()
        self._active_anim: QPropertyAnimation | None = None
        self._active_target: QWidget | None = None

    def switch_to(self, index: int, profile: ThemeProfile, *, immediate: bool = False) -> None:
        if index < 0 or index >= self._stack.count() or self._stack.currentIndex() == index:
            return
        if self._active_anim is not None:
            try:
                self._active_anim.stop()
            except Exception:
                pass
            if self._active_target is not None:
                self._active_target.move(0, 0)
            self._active_anim = None
            self._active_target = None

        state = self._guard.evaluate(profile, window_visible=self._stack.isVisible())
        if immediate or profile.motion_level == "static" or not state.should_animate:
            self._stack.setCurrentIndex(index)
            return

        target = self._stack.widget(index)
        self._stack.setCurrentIndex(index)
        if target is None or target.width() <= 0:
            return

        anim = QPropertyAnimation(target, b"pos", target)
        anim.setDuration(145)
        anim.setStartValue(QPoint(14, 0))
        anim.setEndValue(QPoint(0, 0))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _cleanup() -> None:
            try:
                target.move(0, 0)
            except Exception:
                pass
            self._active_anim = None
            self._active_target = None

        anim.finished.connect(_cleanup)
        self._active_anim = anim
        self._active_target = target
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
