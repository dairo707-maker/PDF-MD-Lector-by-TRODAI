"""Hub of Tools view with dynamic Quick Actions, customizable Hero Banner, Local Insights, and uniform 232x104 ToolCards."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.i18n import HUB_CATEGORIES, t
from ..core.usage_insights import LocalUsageInsights
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from ..theme.theme_profile import ThemeProfile
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag, ToolCard

DEFAULT_QUICK_ACTION_IDS: list[str] = [
    "sign_pdf",
    "merge_pdf",
    "delete_pages",
    "unlock_pdf",
    "convert_from_pdf",
    "compress_pdf",
]

_TOOL_META_BY_ID = {
    entry.tool_id: entry
    for cat in HUB_CATEGORIES
    for entry in cat.tools
}

_HERO_EYEBROW = {
    "es": "SUITE MODULAR PDF + MARKDOWN · TRODAI",
    "en": "MODULAR PDF + MARKDOWN SUITE · TRODAI",
    "pt": "SUITE MODULAR PDF + MARKDOWN · TRODAI",
}


class HubView(QWidget):
    """Home hub pairing a customizable Hero Banner, dynamic Quick Actions, local insights, and uniform grouped ToolCards."""

    open_pdf_requested = Signal()
    open_studio_requested = Signal()
    tool_activated = Signal(str)

    def __init__(
        self,
        *,
        insights: LocalUsageInsights | None = None,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = "es"
        self._mode = "dark"
        self._insights = insights or LocalUsageInsights()
        self._guard = guard or MotionPerformanceGuard()
        self._quick_action_ids: list[str] = list(DEFAULT_QUICK_ACTION_IDS)
        self._quick_flow: FlowLayout | None = None
        self._quick_box: QWidget | None = None
        self._rec_cards: list[tuple[str, str, ToolCard]] = []
        # (cat_id, title_key, cat_box, cat_title, cat_sub, cards_flow, cat_cards)
        self._category_widgets: list[
            tuple[str, str, QWidget, QLabel, QLabel, FlowLayout, list[tuple[str, str, ToolCard]]]
        ] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # Customizable Hero Banner Frame
        self.hero_frame = QFrame(self)
        self.hero_frame.setObjectName("HeroCard")
        self.hero_frame.setMinimumHeight(164)
        hero_wrap = QVBoxLayout(self.hero_frame)
        hero_wrap.setContentsMargins(0, 0, 0, 0)

        self.hero_backdrop = MotionBackdropWidget(self.hero_frame, guard=self._guard, is_viewer_surface=False)
        hero_wrap.addWidget(self.hero_backdrop)

        hero_inner = QVBoxLayout(self.hero_backdrop)
        hero_inner.setContentsMargins(20, 14, 20, 14)
        hero_inner.setSpacing(8)

        self.hero_eyebrow = QLabel("", self.hero_backdrop)
        eyebrow_font = QFont(MONO_FONT_FAMILY, 8)
        eyebrow_font.setWeight(QFont.Weight.Medium)
        self.hero_eyebrow.setFont(eyebrow_font)
        hero_inner.addWidget(self.hero_eyebrow)

        self.hero_title = QLabel("", self.hero_backdrop)
        ht_font = QFont(UI_FONT_FAMILY, 16)
        ht_font.setWeight(QFont.Weight.Bold)
        self.hero_title.setFont(ht_font)
        self.hero_title.setWordWrap(True)
        hero_inner.addWidget(self.hero_title)

        self.hero_subtitle = QLabel("", self.hero_backdrop)
        self.hero_subtitle.setProperty("tone", "secondary")
        hs_font = QFont(UI_FONT_FAMILY, 10)
        hs_font.setWeight(QFont.Weight.Normal)
        self.hero_subtitle.setFont(hs_font)
        self.hero_subtitle.setWordWrap(True)
        hero_inner.addWidget(self.hero_subtitle)

        hero_actions = FlowLayout(h_spacing=8, v_spacing=8)
        self.hero_open_btn = AdaptiveButton("", self.hero_backdrop, role="PrimaryAction")
        self.hero_open_btn.clicked.connect(self.open_pdf_requested.emit)
        hero_actions.addWidget(self.hero_open_btn)

        self.hero_studio_btn = AdaptiveButton("", self.hero_backdrop, role="GhostAction")
        self.hero_studio_btn.clicked.connect(self.open_studio_requested.emit)
        hero_actions.addWidget(self.hero_studio_btn)

        hero_inner.addLayout(hero_actions)
        root.addWidget(self.hero_frame)

        # Inline filter bar
        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self.filter_input = QLineEdit(self)
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.filter_input, 1)

        self.count_lbl = QLabel("", self)
        self.count_lbl.setProperty("mono", "true")
        self.count_lbl.setProperty("tone", "muted")
        filter_row.addWidget(self.count_lbl)
        root.addLayout(filter_row)

        # Scrollable Recommendations + 8 Categories
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.scroll_host = QWidget(self.scroll)
        self.scroll_host.setObjectName("TransparentHost")
        self.scroll_layout = QVBoxLayout(self.scroll_host)
        self.scroll_layout.setContentsMargins(0, 2, 6, 8)
        self.scroll_layout.setSpacing(16)

        # Local Recommendations Block ("Recomendado para ti")
        self.rec_box = QWidget(self.scroll_host)
        self.rec_box.setObjectName("TransparentHost")
        rec_layout = QVBoxLayout(self.rec_box)
        rec_layout.setContentsMargins(0, 0, 0, 0)
        rec_layout.setSpacing(8)

        rec_head = QHBoxLayout()
        rec_head.setSpacing(8)
        self.rec_title = QLabel("", self.rec_box)
        rt_f = QFont(UI_FONT_FAMILY, 12)
        rt_f.setWeight(QFont.Weight.Bold)
        self.rec_title.setFont(rt_f)
        rec_head.addWidget(self.rec_title)

        self.rec_badge = StatusTag("SOLO LOCAL", tone="ok", parent=self.rec_box)
        rec_head.addWidget(self.rec_badge)

        self.rec_sub = QLabel("", self.rec_box)
        self.rec_sub.setProperty("tone", "muted")
        self.rec_sub.setWordWrap(True)
        rec_head.addWidget(self.rec_sub, 1)

        self.rec_toggle_btn = AdaptiveButton("", self.rec_box, role="GhostAction")
        self.rec_toggle_btn.setMinimumHeight(26)
        self.rec_toggle_btn.clicked.connect(self._toggle_insights)
        rec_head.addWidget(self.rec_toggle_btn)

        self.rec_clear_btn = AdaptiveButton("", self.rec_box, role="GhostAction")
        self.rec_clear_btn.setMinimumHeight(26)
        self.rec_clear_btn.clicked.connect(self._clear_insights)
        rec_head.addWidget(self.rec_clear_btn)
        rec_layout.addLayout(rec_head)

        self.rec_flow = FlowLayout(h_spacing=10, v_spacing=10)
        rec_layout.addLayout(self.rec_flow)

        self.rec_paused_lbl = QLabel("", self.rec_box)
        self.rec_paused_lbl.setProperty("tone", "muted")
        self.rec_paused_lbl.setWordWrap(True)
        self.rec_paused_lbl.setVisible(False)
        rec_layout.addWidget(self.rec_paused_lbl)

        self.scroll_layout.addWidget(self.rec_box)

        self.empty_lbl = QLabel("", self.scroll_host)
        self.empty_lbl.setProperty("tone", "muted")
        self.empty_lbl.setWordWrap(True)
        self.empty_lbl.setVisible(False)
        self.scroll_layout.addWidget(self.empty_lbl)

        for cat in HUB_CATEGORIES:
            cat_box = QWidget(self.scroll_host)
            cat_box.setObjectName("TransparentHost")
            cat_layout = QVBoxLayout(cat_box)
            cat_layout.setContentsMargins(0, 0, 0, 0)
            cat_layout.setSpacing(8)

            header_row = QHBoxLayout()
            header_row.setSpacing(10)
            cat_title = QLabel("", cat_box)
            ct_font = QFont(UI_FONT_FAMILY, 12)
            ct_font.setWeight(QFont.Weight.Bold)
            cat_title.setFont(ct_font)
            header_row.addWidget(cat_title)

            cat_sub = QLabel("", cat_box)
            cat_sub.setProperty("tone", "muted")
            cat_sub.setFont(QFont(UI_FONT_FAMILY, 9))
            header_row.addWidget(cat_sub, 1)
            cat_layout.addLayout(header_row)

            cards_flow = FlowLayout(h_spacing=10, v_spacing=10)
            cat_cards: list[tuple[str, str, ToolCard]] = []
            if cat.cat_id == "quick":
                self._quick_flow = cards_flow
                self._quick_box = cat_box
            else:
                for entry in cat.tools:
                    card = ToolCard(
                        entry.tool_id,
                        entry.icon,
                        t(entry.title_key, self._lang),
                        t(entry.desc_key, self._lang),
                        badge=entry.badge,
                        parent=cat_box,
                    )
                    card.clicked.connect(self._on_tool_clicked)
                    cards_flow.addWidget(card)
                    cat_cards.append((entry.title_key, entry.desc_key, card))

            cat_layout.addLayout(cards_flow)
            self.scroll_layout.addWidget(cat_box)
            self._category_widgets.append(
                (cat.cat_id, cat.title_key, cat_box, cat_title, cat_sub, cards_flow, cat_cards)
            )

        self.scroll_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_host)
        root.addWidget(self.scroll, 1)

        self.refresh_recommendations()
        self.retranslate("es")

    def quick_action_tool_ids(self) -> list[str]:
        """Return the ordered list of 6 dynamic Quick Action tool_ids currently displayed."""
        return list(self._quick_action_ids)

    def _clear_flow_widgets(self, flow: FlowLayout) -> None:
        while flow.count():
            item = flow.takeAt(0)
            w = item.widget()
            if w is not None and shiboken6.isValid(w):
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _rebuild_quick_actions(self) -> None:
        if self._quick_flow is None or self._quick_box is None:
            return
        dynamic_ids = self._insights.get_dynamic_quick_actions(
            DEFAULT_QUICK_ACTION_IDS,
            limit=6,
            valid_ids=_TOOL_META_BY_ID.keys(),
        )
        self._quick_action_ids = list(dynamic_ids)

        self._clear_flow_widgets(self._quick_flow)
        new_cards: list[tuple[str, str, ToolCard]] = []
        for tool_id in dynamic_ids:
            meta = _TOOL_META_BY_ID.get(tool_id)
            if meta is None:
                continue
            card = ToolCard(
                meta.tool_id,
                meta.icon,
                t(meta.title_key, self._lang),
                t(meta.desc_key, self._lang),
                badge=meta.badge,
                parent=self._quick_box,
            )
            card.apply_theme(self._mode)
            card.clicked.connect(self._on_tool_clicked)
            self._quick_flow.addWidget(card)
            new_cards.append((meta.title_key, meta.desc_key, card))

        for idx, entry_tuple in enumerate(self._category_widgets):
            if entry_tuple[0] == "quick":
                cat_id, title_key, cat_box, cat_title, cat_sub, cards_flow, _ = entry_tuple
                self._category_widgets[idx] = (
                    cat_id,
                    title_key,
                    cat_box,
                    cat_title,
                    cat_sub,
                    cards_flow,
                    new_cards,
                )
                break

    def _on_tool_clicked(self, tool_id: str) -> None:
        self._insights.record_tool_use(tool_id)
        QTimer.singleShot(0, self._safe_refresh_recommendations)
        self.tool_activated.emit(tool_id)

    def _toggle_insights(self) -> None:
        self._insights.set_enabled(not self._insights.enabled)
        QTimer.singleShot(0, self._safe_refresh_recommendations)

    def _clear_insights(self) -> None:
        self._insights.clear_history()
        self._quick_action_ids = list(DEFAULT_QUICK_ACTION_IDS)
        QTimer.singleShot(0, self._safe_refresh_recommendations)

    def _safe_refresh_recommendations(self) -> None:
        if shiboken6.isValid(self):
            self.refresh_recommendations()

    def refresh_recommendations(self) -> None:
        if not shiboken6.isValid(self):
            return
        self._rebuild_quick_actions()

        self._clear_flow_widgets(self.rec_flow)
        self._rec_cards.clear()

        enabled = self._insights.enabled
        self.rec_paused_lbl.setVisible(not enabled)
        self.rec_toggle_btn.setText(t("rec.pause" if enabled else "rec.resume", self._lang))

        if not enabled:
            self._apply_filter(self.filter_input.text())
            return

        recs = self._insights.get_recommended_tools(limit=4)
        for tool_id, reason_key in recs:
            meta = _TOOL_META_BY_ID.get(tool_id)
            if meta is None:
                continue
            card = ToolCard(
                meta.tool_id,
                meta.icon,
                t(meta.title_key, self._lang),
                t(reason_key, self._lang),
                badge=meta.badge,
                parent=self.rec_box,
            )
            card.apply_theme(self._mode)
            card.clicked.connect(self._on_tool_clicked)
            self.rec_flow.addWidget(card)
            self._rec_cards.append((meta.title_key, reason_key, card))
        self._apply_filter(self.filter_input.text())

    def focus_search(self) -> None:
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def set_search_query(self, query: str) -> None:
        self.set_external_filter(query)

    def set_external_filter(self, query: str) -> None:
        if self.filter_input.text() != query:
            self.filter_input.setText(query)

    def _on_filter_changed(self, raw_query: str) -> None:
        self._insights.record_search(raw_query)
        self._apply_filter(raw_query)

    def _apply_filter(self, raw_query: str) -> None:
        query = raw_query.strip().lower()
        self.rec_box.setVisible(not query)

        visible_tools = 0
        for _, _, cat_box, cat_title, _, _, cards in self._category_widgets:
            cat_matches = bool(query) and query in cat_title.text().lower()
            any_card = False
            for _, _, card in cards:
                haystack = f"{card.title_lbl.text()} {card.desc_lbl.text()} {card.badge_tag.text()}".lower()
                show = not query or cat_matches or (query in haystack)
                card.setVisible(show)
                if show:
                    any_card = True
                    visible_tools += 1
            cat_box.setVisible(any_card)

        self.empty_lbl.setVisible(visible_tools == 0)
        self.count_lbl.setText(t("hub.results_count", self._lang, count=visible_tools))

    def retranslate(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self.hero_eyebrow.setText(_HERO_EYEBROW.get(self._lang, _HERO_EYEBROW["es"]))
        self.hero_title.setText(t("hub.hero.title", self._lang))
        self.hero_subtitle.setText(t("hub.hero.subtitle", self._lang))
        self.hero_open_btn.setText(t("hub.hero.open_btn", self._lang))
        self.hero_studio_btn.setText(t("hub.hero.studio_btn", self._lang))
        self.filter_input.setPlaceholderText(t("hub.search_placeholder", self._lang))
        self.empty_lbl.setText(t("hub.empty_search", self._lang))

        self.rec_title.setText(t("rec.title", self._lang))
        self.rec_sub.setText(t("rec.sub", self._lang))
        self.rec_badge.setText(t("rec.badge", self._lang))
        self.rec_clear_btn.setText(t("rec.clear", self._lang))
        self.rec_paused_lbl.setText(t("rec.paused_msg", self._lang))

        for _, title_key, _, cat_title, cat_sub, _, cards in self._category_widgets:
            cat_title.setText(t(title_key, self._lang))
            cat_sub.setText(t(f"{title_key}.sub", self._lang))
            for t_key, d_key, card in cards:
                card.set_content(t(t_key, self._lang), t(d_key, self._lang))

        self.refresh_recommendations()

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._mode = profile.base_mode
        p = get_palette(self._mode)

        self.hero_backdrop.set_profile(profile)
        self.hero_eyebrow.setStyleSheet(f"color: {p['text_secondary']};")
        self.hero_open_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
        self.hero_studio_btn.setIcon(load_svg_icon("palette", color=p["icon_color"], size=15))
        self.rec_badge.apply_theme(self._mode)

        for _, _, card in self._rec_cards:
            card.apply_theme(self._mode)
        for _, _, _, _, _, _, cards in self._category_widgets:
            for _, _, card in cards:
                card.apply_theme(self._mode)

    def all_tool_cards(self) -> list[ToolCard]:
        out: list[ToolCard] = []
        for _, _, _, _, _, _, cards in self._category_widgets:
            for _, _, card in cards:
                out.append(card)
        return out

    def cleanup(self) -> None:
        self.hero_backdrop.cleanup()
