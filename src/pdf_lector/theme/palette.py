"""TRODAI Neutral Solid Corporate Palette & 0px QSS design system (zero translucent selections)."""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from .. import get_base_dir

DARK_PALETTE: dict[str, str] = {
    "mode": "dark",
    "bg_window": "#0B0F19",
    "bg_window_hex": "#0B0F19",
    "bg_sidebar": "#101624",
    "bg_sidebar_hex": "#101624",
    "bg_glass": "#131B2C",
    "bg_glass_elevated": "#182234",
    "bg_elevated_hex": "#182234",
    "bg_selected_hex": "#212D44",
    "bg_track_hex": "#0D121E",
    "bg_canvas": "#070A12",
    "bg_menu": "#101624",
    "bg_hover": "#182234",
    "bg_active": "#212D44",
    "border_hairline": "#1F2A3E",
    "border_strong": "#334155",
    "border_selected": "#64748B",
    "text_primary": "#F8FAFC",
    "text_secondary": "#CBD5E1",
    "text_muted": "#94A3B8",
    "primary_bg": "#F8FAFC",
    "primary_fg": "#0B0F19",
    "primary_hover": "#E2E8F0",
    "icon_color": "#F8FAFC",
    "accent": "#94A3B8",
    "accent_alt": "#64748B",
    "motion_cyan": "#0EA5E9",
    "motion_blue": "#3B82F6",
    "app_crimson": "#DC2626",
    "tag_ok": "#6EE7B7",
    "tag_warn": "#FCD34D",
    "tag_risk": "#FCA5A5",
}

LIGHT_PALETTE: dict[str, str] = {
    "mode": "light",
    "bg_window": "#F8FAFC",
    "bg_window_hex": "#F8FAFC",
    "bg_sidebar": "#EEF2F6",
    "bg_sidebar_hex": "#EEF2F6",
    "bg_glass": "#FFFFFF",
    "bg_glass_elevated": "#F1F5F9",
    "bg_elevated_hex": "#FFFFFF",
    "bg_selected_hex": "#DDE5F0",
    "bg_track_hex": "#E2E8F0",
    "bg_canvas": "#E2E8F0",
    "bg_menu": "#FFFFFF",
    "bg_hover": "#E6ECF5",
    "bg_active": "#DDE5F0",
    "border_hairline": "#D5DCE6",
    "border_strong": "#AEB8C7",
    "border_selected": "#64748B",
    "text_primary": "#0A0F1D",
    "text_secondary": "#334155",
    "text_muted": "#4A5568",
    "primary_bg": "#0F172A",
    "primary_fg": "#F8FAFC",
    "primary_hover": "#1E293B",
    "icon_color": "#0A0F1D",
    "accent": "#475569",
    "accent_alt": "#64748B",
    "motion_cyan": "#2563EB",
    "motion_blue": "#3B82F6",
    "app_crimson": "#B91C1C",
    "tag_ok": "#166534",
    "tag_warn": "#92400E",
    "tag_risk": "#991B1B",
}

UI_FONT_FAMILY = "Space Grotesk"
MONO_FONT_FAMILY = "JetBrains Mono"


def get_palette(mode: str = "dark", accent_override: str = "") -> dict[str, str]:
    """Return copy of solid dark or light TRODAI neutral palette."""
    del accent_override  # Neutral solid surfaces only (no accent color swatches).
    return dict(LIGHT_PALETTE if mode == "light" else DARK_PALETTE)


def register_embedded_fonts(app: QApplication) -> str:
    """Register bundled Space Grotesk + JetBrains Mono OFL fonts into Qt font database."""
    fonts_dir = get_base_dir() / "assets" / "fonts"
    if fonts_dir.exists():
        for font_file in sorted(fonts_dir.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(font_file))

    families = set(QFontDatabase.families())
    chosen = UI_FONT_FAMILY if UI_FONT_FAMILY in families else "Segoe UI"
    base_font = QFont(chosen, 10)
    base_font.setWeight(QFont.Weight.Normal)
    base_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(base_font)
    return chosen


def build_trodai_qss(mode: str = "dark", accent_override: str = "") -> str:
    """Build global solid stylesheet enforcing 0px corners (6px exception only on TopBar)."""
    p = get_palette(mode, accent_override)
    ok_bg = "#152E28" if mode == "dark" else "#DCFCE7"
    ok_bd = "#2B6A5A" if mode == "dark" else "#86EFAC"
    warn_bg = "#352814" if mode == "dark" else "#FEF3C7"
    warn_bd = "#78561F" if mode == "dark" else "#FCD34D"
    risk_bg = "#3A191E" if mode == "dark" else "#FEE2E2"
    risk_bd = "#7F2933" if mode == "dark" else "#FCA5A5"

    return f"""
    * {{
        border-radius: 0px;
        font-family: "{UI_FONT_FAMILY}", "Segoe UI", sans-serif;
        outline: none;
    }}

    QMainWindow, QDialog {{
        background-color: {p["bg_window"]};
        color: {p["text_primary"]};
    }}

    QWidget#RootCanvas {{
        background-color: {p["bg_window_hex"]};
        border: 1px solid {p["border_hairline"]};
    }}

    QWidget#TransparentHost {{
        background: transparent;
        border: none;
    }}

    QWidget#Sidebar {{
        background-color: {p["bg_sidebar"]};
        border-right: 1px solid {p["border_hairline"]};
    }}

    QWidget#TopBar {{
        background-color: {p["bg_sidebar"]};
        border-bottom: 1px solid {p["border_hairline"]};
    }}

    QWidget#TopBar QPushButton,
    QWidget#TopBar QComboBox,
    QWidget#TopBar QLabel#TopPill,
    QWidget#TopBar QLineEdit {{
        border-radius: 6px;
    }}

    QLabel {{
        color: {p["text_primary"]};
        background: transparent;
    }}

    QLabel[tone="secondary"] {{
        color: {p["text_secondary"]};
    }}

    QLabel[tone="muted"] {{
        color: {p["text_muted"]};
    }}

    QLabel[mono="true"] {{
        font-family: "{MONO_FONT_FAMILY}", "Consolas", monospace;
    }}

    QFrame#CardPanel, QWidget#CardPanel {{
        background-color: {p["bg_glass"]};
        border: 1px solid {p["border_hairline"]};
    }}

    QFrame#HeroCard {{
        background-color: transparent;
        border: 1px solid {p["border_strong"]};
    }}

    QFrame#ViewerCanvasFrame {{
        background-color: {p["bg_canvas"]};
        border: 1px solid {p["border_hairline"]};
    }}

    QFrame#ThumbRail, QFrame#DictPanel {{
        background-color: {p["bg_sidebar"]};
        border-right: 1px solid {p["border_hairline"]};
    }}

    QFrame#DictPanel {{
        border-right: none;
        border-left: 1px solid {p["border_hairline"]};
    }}

    QFrame#ToolCard {{
        background-color: {p["bg_glass"]};
        border: 1px solid {p["border_hairline"]};
    }}

    QFrame#ToolCard:hover {{
        background-color: {p["bg_glass_elevated"]};
        border-color: {p["border_strong"]};
    }}

    QPushButton {{
        background-color: {p["bg_glass_elevated"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
        padding: 7px 14px;
        font-size: 12px;
        font-weight: 500;
    }}

    QPushButton:hover {{
        background-color: {p["bg_hover"]};
        border-color: {p["border_selected"]};
    }}

    QPushButton[role="PrimaryAction"] {{
        background-color: {p["primary_bg"]};
        color: {p["primary_fg"]};
        border: 1px solid {p["primary_bg"]};
        font-weight: 700;
    }}

    QPushButton[role="PrimaryAction"]:hover {{
        background-color: {p["primary_hover"]};
        border-color: {p["border_selected"]};
    }}

    QPushButton[role="GhostAction"] {{
        background-color: {p["bg_glass"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
    }}

    QPushButton[role="GhostAction"]:hover {{
        background-color: {p["bg_hover"]};
        border-color: {p["border_selected"]};
    }}

    QPushButton[role="ModeTab"] {{
        background-color: {p["bg_glass"]};
        color: {p["text_secondary"]};
        border: 1px solid {p["border_hairline"]};
        padding: 7px 12px;
    }}

    QPushButton[role="ModeTab"]:checked {{
        color: {p["text_primary"]};
        background-color: {p["bg_selected_hex"]};
        border: 1px solid {p["border_strong"]};
        font-weight: 700;
    }}

    QPushButton[role="RestoreAction"] {{
        background-color: {ok_bg};
        color: {p["tag_ok"]};
        border: 1px solid {ok_bd};
    }}

    QPushButton[role="MoveAction"] {{
        background-color: {warn_bg};
        color: {p["tag_warn"]};
        border: 1px solid {warn_bd};
    }}

    QPushButton[role="RiskAction"] {{
        background-color: {risk_bg};
        color: {p["tag_risk"]};
        border: 1px solid {risk_bd};
    }}

    QPushButton#TopUtilityBtn {{
        background-color: {p["bg_glass"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_hairline"]};
        padding: 5px 10px;
    }}

    QPushButton#TopUtilityBtn:hover {{
        background-color: {p["bg_hover"]};
        border-color: {p["border_strong"]};
    }}

    QPushButton#TopUtilityBtn:checked {{
        background-color: {p["bg_selected_hex"]};
        border-color: {p["border_selected"]};
    }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background-color: {p["bg_glass_elevated"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
        padding: 6px 10px;
        font-size: 12px;
    }}

    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {p["border_selected"]};
    }}

    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}

    QComboBox QAbstractItemView {{
        background-color: {p["bg_menu"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
        selection-background-color: {p["bg_active"]};
    }}

    QTabWidget::pane {{
        border: none;
        background: transparent;
    }}

    QTabBar {{
        qproperty-drawBase: 0;
        background-color: {p["bg_sidebar"]};
        border-bottom: 1px solid {p["border_hairline"]};
    }}

    QTabBar::tab {{
        background-color: {p["bg_sidebar"]};
        color: {p["text_secondary"]};
        border-right: 1px solid {p["border_hairline"]};
        border-bottom: 2px solid transparent;
        padding: 8px 14px;
        min-width: 120px;
        font-size: 12px;
        font-weight: 500;
    }}

    QTabBar::tab:selected {{
        background-color: {p["bg_glass_elevated"]};
        color: {p["text_primary"]};
        border-bottom: 2px solid {p["border_selected"]};
        font-weight: 700;
    }}

    QTabBar::tab:hover {{
        background-color: {p["bg_hover"]};
    }}

    QCheckBox {{
        color: {p["text_primary"]};
        spacing: 8px;
        font-size: 12px;
    }}

    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
        border: 1px solid {p["border_strong"]};
        background-color: {p["bg_glass_elevated"]};
    }}

    QCheckBox::indicator:checked {{
        background-color: {p["bg_selected_hex"]};
        border-color: {p["text_primary"]};
        image: url("{str(get_base_dir() / "assets" / "icons" / ("check_white.svg" if mode == "dark" else "check_dark.svg")).replace("\\", "/")}");
    }}

    QSlider::groove:horizontal {{
        height: 4px;
        background: {p["border_hairline"]};
    }}

    QSlider::sub-page:horizontal {{
        background: {p["border_selected"]};
    }}

    QSlider::handle:horizontal {{
        background: {p["primary_bg"]};
        border: 1px solid {p["border_strong"]};
        width: 12px;
        height: 14px;
        margin: -5px 0px;
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}

    QScrollBar:vertical {{
        width: 9px;
        background: transparent;
    }}

    QScrollBar::handle:vertical {{
        background: {p["border_strong"]};
        min-height: 28px;
    }}

    QScrollBar::handle:vertical:hover {{
        background: {p["border_selected"]};
    }}

    QScrollBar:horizontal {{
        height: 9px;
        background: transparent;
    }}

    QScrollBar::handle:horizontal {{
        background: {p["border_strong"]};
        min-width: 28px;
    }}

    QScrollBar::add-line, QScrollBar::sub-line,
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: none;
        border: none;
    }}

    QListWidget, QTreeWidget {{
        background-color: {p["bg_sidebar"]};
        border: none;
        color: {p["text_primary"]};
    }}

    QListWidget::item, QTreeWidget::item {{
        padding: 5px;
        border: 1px solid transparent;
    }}

    QListWidget::item:selected, QTreeWidget::item:selected {{
        background-color: {p["bg_selected_hex"]};
        color: {p["text_primary"]};
        border-color: {p["border_strong"]};
    }}

    QToolTip {{
        background-color: {p["bg_menu"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
        padding: 5px 8px;
        font-size: 11px;
    }}

    QMenu {{
        background-color: {p["bg_menu"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
        padding: 4px;
    }}

    QMenu::item {{
        padding: 6px 14px;
        color: {p["text_primary"]};
    }}

    QMenu::item:selected {{
        background-color: {p["bg_selected_hex"]};
        color: {p["text_primary"]};
    }}

    QFrame#AdobeRightRail {{
        background-color: {p["bg_sidebar"]};
        border-left: 1px solid {p["border_hairline"]};
    }}

    QWidget#SidebarRibbon {{
        background-color: {p["bg_sidebar"]};
        border-right: 1px solid {p["border_hairline"]};
    }}

    QFrame#RailPaneHeader {{
        background-color: {p["bg_glass_elevated"]};
        border-bottom: 1px solid {p["border_hairline"]};
    }}

    QFrame#SelectionPopover {{
        background-color: {p["bg_menu"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_strong"]};
    }}

    QPushButton[iconOnly="true"] {{
        padding: 5px;
        min-width: 28px;
        min-height: 28px;
    }}

    QTextBrowser {{
        background-color: {p["bg_glass"]};
        color: {p["text_primary"]};
        border: 1px solid {p["border_hairline"]};
    }}

    QSplitter::handle {{
        background-color: {p["border_hairline"]};
    }}

    QSplitter::handle:hover {{
        background-color: {p["border_selected"]};
    }}
    """
