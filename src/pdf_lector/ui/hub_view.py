"""Hub of Tools view with dynamic Quick Actions, customizable Hero Banner, Local Insights, and uniform 232x104 ToolCards."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.i18n import HUB_CATEGORIES, t
from ..core.recent_docs import RecentDocEntry, RecentDocumentsStore
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

_HUB_RECENT_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "title": "Abiertos recientemente",
        "badge": "FAVORITOS PRIMERO",
        "clear": "Limpiar recientes",
        "empty": "Aun no has abierto PDFs o Markdown recientes. Abre uno y fijalo como favorito.",
        "pin": "Fijar / Favorito",
        "unpin": "Quitar de favoritos",
        "remove": "Quitar de recientes",
        "open": "Abrir documento",
    },
    "en": {
        "title": "Recently Opened",
        "badge": "PINNED FIRST",
        "clear": "Clear recents",
        "empty": "No recent PDF or Markdown documents yet. Open one and pin your favorites.",
        "pin": "Pin / Favorite",
        "unpin": "Unpin favorite",
        "remove": "Remove from recents",
        "open": "Open document",
    },
    "pt": {
        "title": "Abertos recentemente",
        "badge": "FIXADOS PRIMEIRO",
        "clear": "Limpar recentes",
        "empty": "Nenhum PDF ou Markdown recente ainda. Abra um e fixe seus favoritos.",
        "pin": "Fixar / Favorito",
        "unpin": "Desafixar favorito",
        "remove": "Remover dos recentes",
        "open": "Abrir documento",
    },
}


class HubView(QWidget):
    """Home hub pairing a customizable Hero Banner, Recents & Pinned docs, Quick Actions, and uniform ToolCards."""

    open_pdf_requested = Signal()
    open_studio_requested = Signal()
    tool_activated = Signal(str)
    recent_document_requested = Signal(str)

    def __init__(
        self,
        *,
        insights: LocalUsageInsights | None = None,
        recent_store: RecentDocumentsStore | None = None,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = "es"
        self._mode = "dark"
        self._applied_theme_mode = ""
        self._pending_theme_profile: ThemeProfile | None = None
        self._insights = insights or LocalUsageInsights()
        self._recent_store = recent_store or RecentDocumentsStore()
        self._guard = guard or MotionPerformanceGuard()
        self._quick_action_ids: list[str] = list(DEFAULT_QUICK_ACTION_IDS)
        self._quick_flow: FlowLayout | None = None
        self._quick_box: QWidget | None = None
        self._rec_cards: list[tuple[str, str, ToolCard]] = []
        self._recent_row_refs: list[tuple[RecentDocEntry, QPushButton, QPushButton, StatusTag]] = []
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

        # Recent & Pinned Documents Section (Top of Hub scroll area)
        self.recent_box = QWidget(self.scroll_host)
        self.recent_box.setObjectName("TransparentHost")
        recent_layout = QVBoxLayout(self.recent_box)
        recent_layout.setContentsMargins(0, 0, 0, 0)
        recent_layout.setSpacing(8)

        recent_head = QHBoxLayout()
        recent_head.setSpacing(8)
        self.recent_title = QLabel("", self.recent_box)
        self.recent_title.setFont(QFont(UI_FONT_FAMILY, 12, QFont.Weight.Bold))
        recent_head.addWidget(self.recent_title)

        self.recent_badge = StatusTag("", tone="accent", parent=self.recent_box)
        recent_head.addWidget(self.recent_badge)
        recent_head.addStretch(1)

        self.recent_clear_btn = AdaptiveButton("", self.recent_box, role="GhostAction")
        self.recent_clear_btn.setMinimumHeight(26)
        self.recent_clear_btn.clicked.connect(self._clear_unpinned_recents)
        recent_head.addWidget(self.recent_clear_btn)
        recent_layout.addLayout(recent_head)

        self.recent_empty_lbl = QLabel("", self.recent_box)
        self.recent_empty_lbl.setProperty("tone", "muted")
        self.recent_empty_lbl.setWordWrap(True)
        recent_layout.addWidget(self.recent_empty_lbl)

        self.recent_list_layout = QVBoxLayout()
        self.recent_list_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_list_layout.setSpacing(5)
        recent_layout.addLayout(self.recent_list_layout)

        self.scroll_layout.addWidget(self.recent_box)

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

        self.refresh_recents()
        self.refresh_recommendations()
        self.retranslate("es")

    def _clear_unpinned_recents(self) -> None:
        self._recent_store.clear_unpinned()
        QTimer.singleShot(0, self.refresh_recents)

    def _toggle_recent_pin(self, doc_path: str) -> None:
        self._recent_store.toggle_pin(doc_path)
        QTimer.singleShot(0, self.refresh_recents)

    def _remove_recent_entry(self, doc_path: str) -> None:
        self._recent_store.remove(doc_path)
        QTimer.singleShot(0, self.refresh_recents)

    def refresh_recents(self) -> None:
        if not shiboken6.isValid(self):
            return
        while self.recent_list_layout.count():
            item = self.recent_list_layout.takeAt(0)
            w = item.widget()
            if w is not None and shiboken6.isValid(w):
                w.hide()
                w.deleteLater()
        self._recent_row_refs.clear()

        entries = self._recent_store.list_entries()[:10]
        self.recent_empty_lbl.setVisible(not entries)
        txt = _HUB_RECENT_TEXTS.get(self._lang, _HUB_RECENT_TEXTS["es"])
        p = get_palette(self._mode)

        for entry in entries:
            row = QFrame(self.recent_box)
            row.setObjectName("CardPanel")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(10, 6, 8, 6)
            rl.setSpacing(8)

            type_tag = StatusTag(entry.doc_type, tone="ok" if entry.pinned else "muted", parent=row)
            type_tag.apply_theme(self._mode)
            rl.addWidget(type_tag)

            open_btn = QPushButton(entry.name, row)
            open_btn.setProperty("role", "GhostAction")
            open_btn.setStyleSheet("text-align: left; border: none; padding: 3px 6px; font-weight: 700;")
            open_btn.setToolTip(f"{txt['open']}: {entry.path}")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.clicked.connect(lambda _=False, path_s=entry.path: self.recent_document_requested.emit(path_s))
            rl.addWidget(open_btn, 1)

            meta_parts = [entry.display_timestamp()]
            if entry.page_count > 0:
                meta_parts.append(f"{entry.page_count} p.")
            meta_lbl = QLabel("  ·  ".join(part for part in meta_parts if part), row)
            meta_lbl.setProperty("mono", "true")
            meta_lbl.setProperty("tone", "muted")
            rl.addWidget(meta_lbl)

            pin_btn = QPushButton("", row)
            pin_btn.setProperty("role", "ModeTab")
            pin_btn.setCheckable(True)
            pin_btn.setChecked(entry.pinned)
            pin_btn.setFixedSize(26, 26)
            pin_btn.setStyleSheet("padding: 3px;")
            pin_btn.setToolTip(txt["unpin"] if entry.pinned else txt["pin"])
            pin_btn.setIcon(load_svg_icon("pin", color=p["tag_ok"] if entry.pinned else p["icon_color"], size=13))
            pin_btn.clicked.connect(lambda _=False, path_s=entry.path: self._toggle_recent_pin(path_s))
            rl.addWidget(pin_btn)

            rm_btn = QPushButton("", row)
            rm_btn.setProperty("role", "GhostAction")
            rm_btn.setFixedSize(26, 26)
            rm_btn.setStyleSheet("padding: 3px;")
            rm_btn.setToolTip(txt["remove"])
            rm_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=12))
            rm_btn.clicked.connect(lambda _=False, path_s=entry.path: self._remove_recent_entry(path_s))
            rl.addWidget(rm_btn)

            self.recent_list_layout.addWidget(row)
            self._recent_row_refs.append((entry, pin_btn, rm_btn, type_tag))

    def quick_action_tool_ids(self) -> list[str]:
        """Return the ordered list of 6 dynamic Quick Action tool_ids currently displayed."""
        return list(self._quick_action_ids)

    def _clear_flow_widgets(self, flow: FlowLayout) -> None:
        while flow.count():
            item = flow.takeAt(0)
            w = item.widget()
            if w is not None and shiboken6.isValid(w):
                w.hide()
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
        self.recent_box.setVisible(not query)
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

        rtxt = _HUB_RECENT_TEXTS.get(self._lang, _HUB_RECENT_TEXTS["es"])
        self.recent_title.setText(rtxt["title"])
        self.recent_badge.setText(rtxt["badge"])
        self.recent_clear_btn.setText(rtxt["clear"])
        self.recent_empty_lbl.setText(rtxt["empty"])
        self.refresh_recents()

        for _, title_key, _, cat_title, cat_sub, _, cards in self._category_widgets:
            cat_title.setText(t(title_key, self._lang))
            cat_sub.setText(t(f"{title_key}.sub", self._lang))
            for t_key, d_key, card in cards:
                card.set_content(t(t_key, self._lang), t(d_key, self._lang))

        self.refresh_recommendations()

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._mode = profile.base_mode
        self._pending_theme_profile = None
        self.hero_backdrop.set_profile(profile)
        if self._mode == self._applied_theme_mode:
            return
        self._applied_theme_mode = self._mode
        p = get_palette(self._mode)

        self.hero_eyebrow.setStyleSheet(f"color: {p['text_secondary']};")
        self.hero_open_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
        self.hero_studio_btn.setIcon(load_svg_icon("palette", color=p["icon_color"], size=15))
        self.rec_badge.apply_theme(self._mode)
        self.recent_badge.apply_theme(self._mode)
        for entry, pin_btn, rm_btn, type_tag in self._recent_row_refs:
            if shiboken6.isValid(type_tag):
                type_tag.apply_theme(self._mode)
            if shiboken6.isValid(pin_btn):
                pin_btn.setIcon(load_svg_icon("pin", color=p["tag_ok"] if entry.pinned else p["icon_color"], size=13))
            if shiboken6.isValid(rm_btn):
                rm_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=12))

        for _, _, card in self._rec_cards:
            card.apply_theme(self._mode)
        for _, _, _, _, _, _, cards in self._category_widgets:
            for _, _, card in cards:
                card.apply_theme(self._mode)

    def defer_profile(self, profile: ThemeProfile) -> None:
        """Record a pending profile while offscreen without iterating 58+ cards immediately."""
        self._mode = profile.base_mode
        self._pending_theme_profile = profile

    def ensure_theme_synced(self) -> None:
        """Flush any deferred theme profile before becoming active."""
        pending = self._pending_theme_profile
        if pending is not None:
            self.apply_profile(pending)

    def all_tool_cards(self) -> list[ToolCard]:
        out: list[ToolCard] = []
        for _, _, _, _, _, _, cards in self._category_widgets:
            for _, _, card in cards:
                out.append(card)
        return out

    def cleanup(self) -> None:
        self.hero_backdrop.cleanup()
