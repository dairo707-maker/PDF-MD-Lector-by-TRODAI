"""Interactive Theme Studio: presets, LiquidGlass neutral sliding controls, photo/GIF banners, and live preview."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import t
from ..core.path_guard import validate_local_media_path
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from ..theme.palette import UI_FONT_FAMILY, get_palette
from ..theme.theme_profile import (
    ThemeProfile,
    default_profile,
    export_theme_package,
    get_preset,
    import_theme_package,
)
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, SlidingSegmentedControl, StatusTag


class ThemeStudioView(QWidget):
    """Live theme, banner media, and Motion customization panel with neutral sliding controls."""

    profile_changed = Signal(object)

    def __init__(
        self,
        initial_profile: ThemeProfile,
        *,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = initial_profile.language
        self._profile = initial_profile.sanitized(check_media_exists=False)
        self._guard = guard or MotionPerformanceGuard()
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header
        head_row = QHBoxLayout()
        head_col = QVBoxLayout()
        head_col.setSpacing(2)
        self.title_lbl = QLabel("", self)
        t_font = QFont(UI_FONT_FAMILY, 16)
        t_font.setWeight(QFont.Weight.Bold)
        self.title_lbl.setFont(t_font)
        head_col.addWidget(self.title_lbl)

        self.subtitle_lbl = QLabel("", self)
        self.subtitle_lbl.setProperty("tone", "secondary")
        self.subtitle_lbl.setWordWrap(True)
        head_col.addWidget(self.subtitle_lbl)
        head_row.addLayout(head_col, 1)

        self.guard_tag = StatusTag("ACTIVO · 60 FPS", tone="ok", parent=self)
        head_row.addWidget(self.guard_tag, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head_row)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(2)

        # Left Scrollable Controls
        controls_scroll = QScrollArea(splitter)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_host = QWidget(controls_scroll)
        controls_host.setObjectName("TransparentHost")
        controls_col = QVBoxLayout(controls_host)
        controls_col.setContentsMargins(0, 0, 8, 8)
        controls_col.setSpacing(10)

        # Card 1: Presets (no colored accents)
        preset_card = self._make_card(controls_host)
        self.preset_hdr = self._section_label(preset_card)
        preset_card.layout().addWidget(self.preset_hdr)
        preset_flow = FlowLayout(h_spacing=6, v_spacing=6)
        self._preset_btns: dict[str, AdaptiveButton] = {}
        for pid in ("trodai_corporate", "trodai_mesh", "editorial_aurora", "arch_grid", "custom_banner"):
            btn = AdaptiveButton(pid, preset_card, role="GhostAction")
            btn.clicked.connect(lambda _=False, key=pid: self._apply_preset(key))
            preset_flow.addWidget(btn)
            self._preset_btns[pid] = btn
        preset_card.layout().addLayout(preset_flow)
        controls_col.addWidget(preset_card)

        # Card 2: Base Mode & Motion Level via LiquidGlass SlidingSegmentedControls (zero accent boxes)
        base_card = self._make_card(controls_host)
        self.base_hdr = self._section_label(base_card)
        base_card.layout().addWidget(self.base_hdr)

        self.mode_switch = SlidingSegmentedControl(base_card)
        self.mode_switch.selection_changed.connect(lambda m: self._update_profile(base_mode=m))
        base_card.layout().addWidget(self.mode_switch)

        motion_row = QHBoxLayout()
        motion_row.setSpacing(8)
        self.motion_switch = SlidingSegmentedControl(base_card)
        self.motion_switch.selection_changed.connect(self._set_motion_level)
        motion_row.addWidget(self.motion_switch, 3)

        self.fps_switch = SlidingSegmentedControl(base_card)
        self.fps_switch.set_options([("30", "30 FPS"), ("60", "60 FPS")])
        self.fps_switch.selection_changed.connect(lambda v: self._update_profile(fps_cap=int(v)))
        motion_row.addWidget(self.fps_switch, 2)
        base_card.layout().addLayout(motion_row)

        self.battery_chk = QCheckBox(base_card)
        self.battery_chk.toggled.connect(lambda v: self._update_profile(pause_on_battery_saver=bool(v)))
        base_card.layout().addWidget(self.battery_chk)
        controls_col.addWidget(base_card)

        # Card 3: Custom Photo / Animated GIF-WebP Media & Readability Sliders
        media_card = self._make_card(controls_host)
        self.media_hdr = self._section_label(media_card)
        media_card.layout().addWidget(self.media_hdr)

        media_btns = FlowLayout(h_spacing=6, v_spacing=6)
        self.choose_media_btn = AdaptiveButton("", media_card, role="PrimaryAction")
        self.choose_media_btn.clicked.connect(self._choose_media_file)
        media_btns.addWidget(self.choose_media_btn)

        self.clear_media_btn = AdaptiveButton("", media_card, role="GhostAction")
        self.clear_media_btn.clicked.connect(lambda: self._update_profile(banner_path=""))
        media_btns.addWidget(self.clear_media_btn)
        media_card.layout().addLayout(media_btns)

        self.media_path_lbl = QLabel("", media_card)
        self.media_path_lbl.setProperty("mono", "true")
        self.media_path_lbl.setProperty("tone", "muted")
        self.media_path_lbl.setWordWrap(True)
        media_card.layout().addWidget(self.media_path_lbl)

        self.apply_hub_chk = QCheckBox(media_card)
        self.apply_hub_chk.toggled.connect(lambda v: self._update_profile(apply_to_hub_banner=bool(v)))
        media_card.layout().addWidget(self.apply_hub_chk)

        self.apply_viewer_chk = QCheckBox(media_card)
        self.apply_viewer_chk.toggled.connect(lambda v: self._update_profile(apply_to_viewer_backdrop=bool(v)))
        media_card.layout().addWidget(self.apply_viewer_chk)

        op_row = QHBoxLayout()
        self.opacity_lbl = QLabel("", media_card)
        self.opacity_val_lbl = QLabel("55%", media_card)
        self.opacity_val_lbl.setProperty("mono", "true")
        op_row.addWidget(self.opacity_lbl)
        op_row.addStretch(1)
        op_row.addWidget(self.opacity_val_lbl)
        media_card.layout().addLayout(op_row)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, media_card)
        self.opacity_slider.setRange(10, 85)
        self.opacity_slider.valueChanged.connect(lambda v: self._on_visual_slider_changed(opacity=v / 100.0))
        self.opacity_slider.sliderReleased.connect(self._flush_pending_slider_emit)
        media_card.layout().addWidget(self.opacity_slider)

        bl_row = QHBoxLayout()
        self.blur_lbl = QLabel("", media_card)
        self.blur_val_lbl = QLabel("4 px", media_card)
        self.blur_val_lbl.setProperty("mono", "true")
        bl_row.addWidget(self.blur_lbl)
        bl_row.addStretch(1)
        bl_row.addWidget(self.blur_val_lbl)
        media_card.layout().addLayout(bl_row)

        self.blur_slider = QSlider(Qt.Orientation.Horizontal, media_card)
        self.blur_slider.setRange(0, 24)
        self.blur_slider.valueChanged.connect(lambda v: self._on_visual_slider_changed(blur_radius=int(v)))
        self.blur_slider.sliderReleased.connect(self._flush_pending_slider_emit)
        media_card.layout().addWidget(self.blur_slider)

        self._slider_emit_timer = QTimer(self)
        self._slider_emit_timer.setSingleShot(True)
        self._slider_emit_timer.setInterval(45)
        self._slider_emit_timer.timeout.connect(self._flush_pending_slider_emit)
        self._last_synced_mode: str = ""
        self._slider_internal_emit: bool = False

        self.auto_scrim_chk = QCheckBox(media_card)
        self.auto_scrim_chk.toggled.connect(lambda v: self._update_profile(auto_scrim=bool(v)))
        media_card.layout().addWidget(self.auto_scrim_chk)
        controls_col.addWidget(media_card)

        # Card 4: Export, Import & Reset
        port_card = self._make_card(controls_host)
        self.port_hdr = self._section_label(port_card)
        port_card.layout().addWidget(self.port_hdr)

        port_flow = FlowLayout(h_spacing=6, v_spacing=6)
        self.export_btn = AdaptiveButton("", port_card, role="GhostAction")
        self.export_btn.clicked.connect(self._export_theme)
        port_flow.addWidget(self.export_btn)

        self.import_btn = AdaptiveButton("", port_card, role="GhostAction")
        self.import_btn.clicked.connect(self._import_theme)
        port_flow.addWidget(self.import_btn)

        self.reset_btn = AdaptiveButton("", port_card, role="RestoreAction")
        self.reset_btn.clicked.connect(self._reset_default)
        port_flow.addWidget(self.reset_btn)
        port_card.layout().addLayout(port_flow)
        controls_col.addWidget(port_card)
        controls_col.addStretch(1)

        controls_scroll.setWidget(controls_host)
        splitter.addWidget(controls_scroll)

        # Right Synchronized Live Preview Column
        preview_card = self._make_card(splitter)
        preview_layout = preview_card.layout()
        self.preview_hdr = self._section_label(preview_card)
        preview_layout.addWidget(self.preview_hdr)

        self.preview_hub_cap = QLabel("", preview_card)
        self.preview_hub_cap.setProperty("tone", "muted")
        preview_layout.addWidget(self.preview_hub_cap)

        hub_frame = QFrame(preview_card)
        hub_frame.setObjectName("HeroCard")
        hub_frame.setMinimumHeight(140)
        hf_wrap = QVBoxLayout(hub_frame)
        hf_wrap.setContentsMargins(0, 0, 0, 0)
        self.preview_hub_backdrop = MotionBackdropWidget(hub_frame, guard=self._guard, is_viewer_surface=False)
        hf_wrap.addWidget(self.preview_hub_backdrop)
        ph_in = QVBoxLayout(self.preview_hub_backdrop)
        ph_in.setContentsMargins(14, 12, 14, 12)
        ph_in.setSpacing(6)
        self.ph_title = QLabel("PDF Lector · Hub", self.preview_hub_backdrop)
        ph_f = QFont(UI_FONT_FAMILY, 13)
        ph_f.setWeight(QFont.Weight.Bold)
        self.ph_title.setFont(ph_f)
        ph_in.addWidget(self.ph_title)
        self.ph_sub = QLabel("Texto legible AA garantizado sobre banner o Motion", self.preview_hub_backdrop)
        self.ph_sub.setProperty("tone", "secondary")
        self.ph_sub.setWordWrap(True)
        ph_in.addWidget(self.ph_sub)
        ph_in.addStretch(1)
        preview_layout.addWidget(hub_frame)

        self.preview_viewer_cap = QLabel("", preview_card)
        self.preview_viewer_cap.setProperty("tone", "muted")
        preview_layout.addWidget(self.preview_viewer_cap)

        viewer_frame = QFrame(preview_card)
        viewer_frame.setObjectName("ViewerCanvasFrame")
        viewer_frame.setMinimumHeight(175)
        vf_wrap = QVBoxLayout(viewer_frame)
        vf_wrap.setContentsMargins(0, 0, 0, 0)
        self.preview_viewer_backdrop = MotionBackdropWidget(viewer_frame, guard=self._guard, is_viewer_surface=True)
        vf_wrap.addWidget(self.preview_viewer_backdrop)
        pv_in = QVBoxLayout(self.preview_viewer_backdrop)
        pv_in.setContentsMargins(24, 18, 24, 18)
        pv_in.setAlignment(Qt.AlignmentFlag.AlignCenter)

        mock_sheet = QFrame(self.preview_viewer_backdrop)
        mock_sheet.setFixedSize(140, 128)
        mock_sheet.setStyleSheet("background-color: #FFFFFF; border: 1px solid #33415F;")
        ms_l = QVBoxLayout(mock_sheet)
        ms_l.setContentsMargins(12, 12, 12, 12)
        ms_l.setSpacing(6)
        ms_hdr = QLabel("CONTRATO.PDF", mock_sheet)
        ms_hdr.setStyleSheet("color: #0A0F1D; font-weight: 700; font-size: 9px;")
        ms_l.addWidget(ms_hdr)
        for _ in range(4):
            bar = QWidget(mock_sheet)
            bar.setFixedHeight(4)
            bar.setStyleSheet("background-color: #CBD5E1;")
            ms_l.addWidget(bar)
        ms_l.addStretch(1)
        pv_in.addWidget(mock_sheet)
        preview_layout.addWidget(viewer_frame, 1)

        splitter.addWidget(preview_card)
        splitter.setSizes([540, 400])
        root.addWidget(splitter, 1)

        self.retranslate(self._lang)
        self.sync_from_profile(self._profile)

    def _make_card(self, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setObjectName("CardPanel")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        return card

    def _section_label(self, parent: QWidget) -> QLabel:
        lbl = QLabel("", parent)
        f = QFont(UI_FONT_FAMILY, 11)
        f.setWeight(QFont.Weight.Bold)
        lbl.setFont(f)
        return lbl

    def _apply_preset(self, preset_id: str) -> None:
        preset = get_preset(preset_id, preserve_language=self._lang)
        if preset_id == "custom_banner" and self._profile.banner_path:
            preset = replace(preset, banner_path=self._profile.banner_path)
        self._emit_profile(preset)

    def _set_motion_level(self, level: str) -> None:
        preset = self._profile.motion_preset
        if level == "modern" and preset == "none":
            preset = "polygonal_mesh"
        self._update_profile(motion_level=level, motion_preset=preset)

    def _choose_media_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("studio.choose_media", self._lang),
            "",
            "Images / Animated (*.png *.jpg *.jpeg *.webp *.gif)",
        )
        if chosen:
            self.apply_banner_path(chosen, show_dialog=True)

    def apply_banner_path(self, raw_path: str, *, show_dialog: bool = False) -> bool:
        ok, reason = validate_local_media_path(raw_path, check_exists=True)
        if not ok:
            if show_dialog and QApplication.platformName() != "offscreen":
                QMessageBox.warning(
                    self,
                    t("studio.title", self._lang),
                    t("studio.media_rejected", self._lang, reason=reason),
                )
            return False
        next_level = "subtle" if self._profile.motion_level == "static" else self._profile.motion_level
        self._update_profile(banner_path=str(Path(raw_path)), motion_level=next_level, apply_to_hub_banner=True)
        return True

    def _export_theme(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            t("studio.export", self._lang),
            "trodai-custom.trodai-theme.json",
            "TRODAI Theme (*.trodai-theme.json)",
        )
        if chosen:
            export_theme_package(self._profile, Path(chosen))

    def _import_theme(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("studio.import", self._lang),
            "",
            "TRODAI Theme (*.trodai-theme.json *.json)",
        )
        if not chosen:
            return
        try:
            imported, warning = import_theme_package(Path(chosen))
            imported = replace(imported, language=self._lang)
            self._emit_profile(imported)
            if warning and QApplication.platformName() != "offscreen":
                QMessageBox.warning(
                    self,
                    t("studio.title", self._lang),
                    t("studio.media_rejected", self._lang, reason=warning),
                )
        except Exception as exc:
            if QApplication.platformName() != "offscreen":
                QMessageBox.warning(self, t("studio.title", self._lang), str(exc))

    def _reset_default(self) -> None:
        fresh = replace(default_profile(), language=self._lang)
        self._emit_profile(fresh)

    def _on_visual_slider_changed(self, **changes: object) -> None:
        if self._syncing:
            return
        updated = replace(self._profile, **changes).sanitized(check_media_exists=False)
        self._profile = updated
        self.opacity_val_lbl.setText(f"{int(round(updated.opacity * 100))}%")
        self.blur_val_lbl.setText(f"{updated.blur_radius} px")
        self.preview_hub_backdrop.set_profile(updated)
        self.preview_viewer_backdrop.set_profile(updated)
        if self.opacity_slider.isSliderDown() or self.blur_slider.isSliderDown():
            if not self._slider_emit_timer.isActive():
                self._slider_emit_timer.start()
        else:
            self._slider_emit_timer.stop()
            self._slider_internal_emit = True
            try:
                self.profile_changed.emit(updated)
            finally:
                self._slider_internal_emit = False

    def _flush_pending_slider_emit(self) -> None:
        if hasattr(self, "_slider_emit_timer"):
            self._slider_emit_timer.stop()
        self.profile_changed.emit(self._profile)

    def _update_profile(self, **changes: object) -> None:
        if self._syncing:
            return
        updated = replace(self._profile, **changes).sanitized(check_media_exists=False)
        self._emit_profile(updated)

    def _emit_profile(self, profile: ThemeProfile) -> None:
        self._profile = profile
        self.sync_from_profile(profile)
        self.profile_changed.emit(profile)

    def sync_from_profile(self, profile: ThemeProfile) -> None:
        if self._slider_internal_emit and profile == self._profile:
            return
        self._syncing = True
        try:
            self._profile = profile
            p = get_palette(profile.base_mode)
            mode_changed = profile.base_mode != self._last_synced_mode
            self._last_synced_mode = profile.base_mode

            anim_ok = profile.motion_level != "static"
            if mode_changed:
                self.mode_switch.apply_theme(profile.base_mode)
                self.motion_switch.apply_theme(profile.base_mode)
                self.fps_switch.apply_theme(profile.base_mode)
                self.guard_tag.apply_theme(profile.base_mode)
                self.choose_media_btn.setIcon(load_svg_icon("image", color=p["primary_fg"], size=15))
                self.clear_media_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=15))
                self.export_btn.setIcon(load_svg_icon("export", color=p["icon_color"], size=15))
                self.import_btn.setIcon(load_svg_icon("import", color=p["icon_color"], size=15))
                self.reset_btn.setIcon(load_svg_icon("reset", color=p["tag_ok"], size=15))

            self.mode_switch.set_animations_enabled(anim_ok)
            self.mode_switch.set_current(profile.base_mode, animate=True)

            self.motion_switch.set_animations_enabled(anim_ok)
            self.motion_switch.set_current(profile.motion_level, animate=True)

            self.fps_switch.set_animations_enabled(anim_ok)
            self.fps_switch.set_current(str(30 if profile.fps_cap <= 30 else 60), animate=True)

            self.battery_chk.setChecked(profile.pause_on_battery_saver)
            self.apply_hub_chk.setChecked(profile.apply_to_hub_banner)
            self.apply_viewer_chk.setChecked(profile.apply_to_viewer_backdrop)
            self.opacity_slider.setValue(int(round(profile.opacity * 100)))
            self.opacity_val_lbl.setText(f"{int(round(profile.opacity * 100))}%")
            self.blur_slider.setValue(profile.blur_radius)
            self.blur_val_lbl.setText(f"{profile.blur_radius} px")
            self.auto_scrim_chk.setChecked(profile.auto_scrim)

            if profile.banner_path:
                self.media_path_lbl.setText(Path(profile.banner_path).name)
                self.media_path_lbl.setToolTip(profile.banner_path)
            else:
                self.media_path_lbl.setText(t("studio.media_none", self._lang))
                self.media_path_lbl.setToolTip("")

            self.preview_hub_backdrop.set_profile(profile)
            self.preview_viewer_backdrop.set_profile(profile)

            state = self._guard.evaluate(profile, window_visible=True)
            tone_map = {"active": "ok", "static": "muted", "battery": "warn", "os_off": "warn", "hidden": "muted"}
            text = t(f"studio.guard.{state.status_code}", self._lang, fps=state.target_fps)
            self.guard_tag.set_tone(tone_map.get(state.status_code, "muted"), text)
        finally:
            self._syncing = False

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.title_lbl.setText(t("studio.title", lang))
        self.subtitle_lbl.setText(t("studio.subtitle", lang))
        self.preset_hdr.setText(t("studio.section.presets", lang))
        for pid, btn in self._preset_btns.items():
            btn.setText(t(f"studio.preset.{pid}", lang))

        self.base_hdr.setText(t("studio.section.base", lang))
        self.mode_switch.set_options([
            ("dark", t("studio.base.dark", lang)),
            ("light", t("studio.base.light", lang)),
        ])
        self.motion_switch.set_options([
            ("static", t("studio.motion.static", lang)),
            ("subtle", t("studio.motion.subtle", lang)),
            ("modern", t("studio.motion.modern", lang)),
        ])
        self.battery_chk.setText(t("studio.battery_pause", lang))

        self.media_hdr.setText(t("studio.section.media", lang))
        self.choose_media_btn.setText(t("studio.choose_media", lang))
        self.clear_media_btn.setText(t("studio.clear_media", lang))
        self.apply_hub_chk.setText(t("studio.apply_hub", lang))
        self.apply_viewer_chk.setText(t("studio.apply_viewer", lang))
        self.opacity_lbl.setText(t("studio.opacity", lang))
        self.blur_lbl.setText(t("studio.blur", lang))
        self.auto_scrim_chk.setText(t("studio.auto_scrim", lang))

        self.port_hdr.setText(t("studio.section.portability", lang))
        self.export_btn.setText(t("studio.export", lang))
        self.import_btn.setText(t("studio.import", lang))
        self.reset_btn.setText(t("studio.reset", lang))

        self.preview_hdr.setText(t("studio.preview_title", lang))
        self.preview_hub_cap.setText(t("studio.preview_hub", lang))
        self.preview_viewer_cap.setText(t("studio.preview_viewer", lang))
        self.sync_from_profile(self._profile)

    def cleanup(self) -> None:
        self.preview_hub_backdrop.cleanup()
        self.preview_viewer_backdrop.cleanup()
