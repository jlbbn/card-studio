from __future__ import annotations

import logging
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from . import __build__, __version__
from .application import CardStudioApplication as TransactionApplication
from .backends import CardBackend, CardpeekBackend, SunmiBackend, VirtualCardBackend
from .transactions.view import readiness_view, transaction_view
from .config import AppSettings, SettingsStore
from .cardlab_adapter import load_cardlab_status
from .registry_capture import load_registry_status
from .diagnostics import collect_diagnostics, format_diagnostics
from .dry_run import DryRunAdapter
from .hardware_validation import HardwareValidator, validation_summary
from .ledger import LedgerClient, LedgerError, RealLedgerClient
from .model import (
    CardError,
    CardState,
    Operation,
    format_amount,
    parse_amount,
    validate_card_id,
)
from .pcsc import create_pcsc_backend
from .write_exercise import load_write_exercise_status
from .service import CardStudioService
from .startup import initialize_application
from .store import JsonStore

logger = logging.getLogger(__name__)


class CardStudioWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title(f"Swift Ledger Card Studio {__version__}")
        self.set_default_size(1180, 760)
        self.settings_store = SettingsStore()
        self.settings = self.settings_store.load()
        try:
            self.startup = initialize_application(self.settings_store)
        except Exception as error:
            self.startup = {"software_ready": False, "startup_error": str(error), "physical_validation": "PENDING"}
        self.store = JsonStore()
        self.service = CardStudioService(self.store)
        self.transaction_application = TransactionApplication()
        self.current_state = self.store.load_card()
        self.busy_count = 0
        self._build_ui()
        if self.settings.mode != "virtual":
            self.physical_button.set_active(True)
        self._render_state(self.current_state)
        self._render_history()
        self._render_transactions()
        self._sync_widgets()

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(root)

        header = Adw.HeaderBar()

        header_identity = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        header_identity.set_valign(Gtk.Align.CENTER)

        header_logo_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "swift-ledger-logo.png"
        )

        if header_logo_path.exists():
            mini_logo = Gtk.Image.new_from_file(
                str(header_logo_path)
            )
            mini_logo.set_pixel_size(24)
            mini_logo.set_size_request(24, 24)
            mini_logo.set_valign(Gtk.Align.CENTER)
            mini_logo.add_css_class("header-logo")
            header_identity.append(mini_logo)

        title = Gtk.Label(
            label=f"Card Studio {__version__} · build {__build__}"
        )
        title.add_css_class("title")
        header_identity.append(title)

        header.set_title_widget(header_identity)
        self.cardlab_button = Gtk.Button.new_with_label("CardLab")
        self.cardlab_button.connect(
            "clicked",
            lambda _button: self._show_cardlab(),
        )
        header.pack_end(self.cardlab_button)

        self.write_validation_button = Gtk.Button.new_with_label("Write Validation")
        self.write_validation_button.connect(
            "clicked",
            lambda _button: self._show_write_validation(),
        )
        header.pack_end(self.write_validation_button)

        self.registry_button = Gtk.Button.new_with_label("Registry")
        self.registry_button.connect(
            "clicked",
            lambda _button: self._show_registry(),
        )
        header.pack_end(self.registry_button)

        self.l3_readiness_button = Gtk.Button.new_with_label("EMV / L3 Readiness")
        self.l3_readiness_button.connect("clicked", lambda _button: self._show_l3_readiness())
        header.pack_end(self.l3_readiness_button)
        self.hardware_validation_button = Gtk.Button.new_with_label("Hardware Validation")
        self.hardware_validation_button.connect("clicked", lambda _button: self._show_hardware_validation())
        header.pack_end(self.hardware_validation_button)
        self.api_health_button = Gtk.Button.new_with_label("Test API")
        self.api_health_button.connect("clicked", lambda _button: self._check_api())
        header.pack_end(self.api_health_button)
        self.diagnostics_button = Gtk.Button.new_with_label("Diagnostics")
        self.diagnostics_button.connect("clicked", lambda _button: self._show_diagnostics())
        header.pack_end(self.diagnostics_button)
        for header_button in (
            self.diagnostics_button,
            self.api_health_button,
            self.hardware_validation_button,
            self.l3_readiness_button,
            self.registry_button,
            self.write_validation_button,
            self.cardlab_button,
        ):
            header_button.add_css_class("header-action")

        root.append(header)

        # Responsive main workspace.
        # Gtk.Paned lets the sidebar and main workspace share available
        # width instead of forcing a fixed 300 px column.
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_vexpand(True)
        body.set_hexpand(True)
        body.set_position(285)
        body.set_resize_start_child(False)
        body.set_resize_end_child(True)
        body.set_shrink_start_child(False)
        body.set_shrink_end_child(True)
        root.append(body)

        # Sidebar gets its own vertical scroll when window height is reduced.
        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        sidebar_scroll.set_vexpand(True)
        sidebar_scroll.set_size_request(260, -1)
        body.set_start_child(sidebar_scroll)

        sidebar = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )
        sidebar.set_margin_top(24)
        sidebar.set_margin_bottom(24)
        sidebar.set_margin_start(20)
        sidebar.set_margin_end(18)
        sidebar_scroll.set_child(sidebar)

        mode_title = Gtk.Label(label="OPERATION MODE", xalign=0)
        mode_title.add_css_class("caption-heading")
        sidebar.append(mode_title)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.virtual_button = Gtk.ToggleButton.new_with_label("Virtual")
        self.physical_button = Gtk.ToggleButton.new_with_label("Physical")
        self.physical_button.set_group(self.virtual_button)
        self.virtual_button.set_active(True)
        self.virtual_button.add_css_class("accent")
        self.virtual_button.connect("toggled", self._mode_changed)
        self.physical_button.connect("toggled", self._mode_changed)
        mode_box.append(self.virtual_button)
        mode_box.append(self.physical_button)
        mode_box.set_homogeneous(True)
        sidebar.append(mode_box)

        self.transport_label = Gtk.Label(label="Physical Transport", xalign=0)
        sidebar.append(self.transport_label)
        self.transport = Gtk.DropDown.new_from_strings([
            "SUNMI V3 · ADB",
            create_pcsc_backend().label,
            "PC/SC · Cardpeek (alternativa)",
        ])
        selected_transport = {"sunmi": 0, "pcsc": 1, "cardpeek": 2}.get(self.settings.mode, 1)
        self.transport.set_selected(selected_transport)
        self.transport.connect("notify::selected", lambda *_args: self._sync_widgets())
        sidebar.append(self.transport)
        self.sunmi_url = self._entry("http://127.0.0.1:5380")
        self.sunmi_url.set_text(self.settings.sunmi_url)
        sidebar.append(self.sunmi_url)
        self.reader_name = self._entry("Lector PC/SC · automático")
        self.reader_name.set_text(self.settings.reader)
        sidebar.append(self.reader_name)
        self.bridge_note = Gtk.Label(xalign=0, wrap=True)
        self.bridge_note.add_css_class("caption")
        self.bridge_note.add_css_class("dim-label")
        sidebar.append(self.bridge_note)
        sidebar.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        api_title = Gtk.Label(label="SWIFT LEDGER · API", xalign=0)
        api_title.add_css_class("caption-heading")
        sidebar.append(api_title)
        self.api_mode = Gtk.DropDown.new_from_strings([
            "API real · VENUS privada",
            "Sandbox POS · local",
        ])
        self.api_mode.set_selected(0 if self.settings.ledger_mode == "real" else 1)
        self.api_mode.connect("notify::selected", lambda *_args: self._sync_widgets())
        sidebar.append(self.api_mode)
        self.api_url = self._entry("https://backend.privado")
        self.api_url.set_text(self.settings.ledger_url)
        self.api_backend_id = self._entry("Backend ID autorizado")
        self.api_backend_id.set_text(self.settings.ledger_backend_id)
        self.terminal_code = self._entry("Terminal Code")
        self.terminal_code.set_text(self.settings.terminal_code)
        self.terminal_key = self._entry("32-byte base64url key")
        self.terminal_key.set_visibility(False)
        for label, widget in [
            ("Swift Ledger URL", self.api_url),
            ("Expected Backend ID · real", self.api_backend_id),
            ("Terminal Code", self.terminal_code),
            ("Terminal Key · not stored", self.terminal_key),
        ]:
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("caption")
            caption.add_css_class("dim-label")
            sidebar.append(caption)
            sidebar.append(widget)
        self.save_settings_button = Gtk.Button.new_with_label("Save Non-Secret Settings")
        self.save_settings_button.connect("clicked", lambda _button: self._save_settings())
        sidebar.append(self.save_settings_button)
        self.api_status = Gtk.Label(label="API not checked", xalign=0, wrap=True)
        self.api_status.add_css_class("caption")
        sidebar.append(self.api_status)

        # Main workspace also scrolls vertically instead of being clipped.
        content_scroll = Gtk.ScrolledWindow()
        content_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        content_scroll.set_hexpand(True)
        content_scroll.set_vexpand(True)
        body.set_end_child(content_scroll)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
        )
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(18)
        content.set_margin_end(24)
        content.set_hexpand(True)
        content_scroll.set_child(content)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.detect_button = Gtk.Button.new_with_label("Detect")
        self.detect_button.add_css_class("suggested-action")
        self.detect_button.connect("clicked", lambda _button: self._detect())
        self.read_button = Gtk.Button.new_with_label("Read")
        self.read_button.connect("clicked", lambda _button: self._read())
        self.sync_button = Gtk.Button.new_with_label("Sync Pending")
        self.sync_button.connect("clicked", lambda _button: self._sync_pending())
        for button in [self.detect_button, self.read_button, self.sync_button]:
            toolbar.append(button)
        self.spinner = Gtk.Spinner()
        toolbar.append(self.spinner)
        content.append(toolbar)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        card.add_css_class("bank-card")
        card.set_margin_top(6)
        card.set_margin_bottom(6)

        card_header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=14,
        )

        logo_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "swift-ledger-logo.png"
        )

        if logo_path.exists():
            logo = Gtk.Picture.new_for_filename(str(logo_path))
            logo.set_size_request(46, 46)
            logo.add_css_class("card-logo")
            card_header.append(logo)

        identity = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        identity.set_hexpand(True)

        brand = Gtk.Label(label="SWIFT LEDGER", xalign=0)
        brand.add_css_class("card-brand")
        identity.append(brand)

        self.card_mode = Gtk.Label(label="VIRTUAL CARD", xalign=0)
        self.card_mode.add_css_class("card-caption")
        identity.append(self.card_mode)

        card_header.append(identity)

        contactless_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "contactless.svg"
        )

        if contactless_path.exists():
            contactless = Gtk.Picture.new_for_filename(
                str(contactless_path)
            )
            contactless.set_size_request(76, 46)
            contactless.set_can_shrink(True)
            card_header.append(contactless)

        card.append(card_header)

        balances = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )

        usd = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        usd.set_hexpand(True)
        usd.add_css_class("currency-block")

        usd_code = Gtk.Label(label="USD", xalign=0)
        usd_code.add_css_class("currency-code")
        usd.append(usd_code)

        self.balance_usd = Gtk.Label(label="$0.00", xalign=0)
        self.balance_usd.add_css_class("balance")
        usd.append(self.balance_usd)

        eur = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        eur.set_hexpand(True)
        eur.add_css_class("currency-block")

        eur_code = Gtk.Label(label="EUR", xalign=0)
        eur_code.add_css_class("currency-code")
        eur.append(eur_code)

        self.balance_eur = Gtk.Label(label="€0.00", xalign=0)
        self.balance_eur.add_css_class("balance")
        eur.append(self.balance_eur)

        balances.append(usd)
        balances.append(eur)
        card.append(balances)

        # Compatibilidad temporal con el modelo de saldo actual.
        self.balance = self.balance_usd

        footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )

        self.card_identifier = Gtk.Label(label="SL-5968", xalign=0)
        self.card_identifier.set_hexpand(True)
        self.card_identifier.add_css_class("card-identifier")
        footer.append(self.card_identifier)

        chip_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "emv-chip.svg"
        )

        if chip_path.exists():
            chip = Gtk.Picture.new_for_filename(str(chip_path))
            chip.set_size_request(44, 32)
            chip.set_can_shrink(True)
            footer.append(chip)

        card.append(footer)
        content.append(card)

        editor = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.identifier_entry = self._entry("SL-5968")
        self.identifier_entry.set_hexpand(True)
        self.write_button = Gtk.Button.new_with_label("Write Identifier")
        self.write_button.connect("clicked", lambda _button: self._confirm_write())
        editor.append(self.identifier_entry)
        editor.append(self.write_button)
        content.append(editor)

        amount_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.amount_entry = self._entry("10.00")
        self.amount_entry.set_hexpand(True)
        self.reload_button = Gtk.Button.new_with_label("Reload")
        self.charge_button = Gtk.Button.new_with_label("Charge")
        self.reload_button.connect("clicked", lambda _button: self._start_amount_operation("RELOAD"))
        self.charge_button.connect("clicked", lambda _button: self._start_amount_operation("CHARGE"))
        self.charge_button.add_css_class("destructive-action")
        amount_box.append(self.amount_entry)
        amount_box.append(self.reload_button)
        amount_box.append(self.charge_button)
        content.append(amount_box)

        disclaimer = Gtk.Label(
            label="Controlled Card Studio environment. State and operations are recorded locally.",
            xalign=0,
            wrap=True,
        )
        disclaimer.add_css_class("caption")
        disclaimer.add_css_class("dim-label")
        content.append(disclaimer)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        transactions_title = Gtk.Label(label="TRANSACTIONS", xalign=0)
        transactions_title.add_css_class("caption-heading")
        content.append(transactions_title)
        self.transaction_list = Gtk.ListBox()
        self.transaction_list.add_css_class("boxed-list")
        transaction_scroller = Gtk.ScrolledWindow()
        transaction_scroller.set_min_content_height(130)
        transaction_scroller.set_child(self.transaction_list)
        content.append(transaction_scroller)
        self.l3_summary = Gtk.Label(
            label="LAB READINESS: NOT RUN · certification NOT COMPLETED · physical PENDING",
            xalign=0,
            wrap=True,
        )
        self.l3_summary.add_css_class("caption")
        content.append(self.l3_summary)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        history_title = Gtk.Label(label="LOCAL & API ACTIVITY", xalign=0)
        history_title.add_css_class("caption-heading")
        content.append(history_title)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_min_content_height(150)
        self.history_list = Gtk.ListBox()
        self.history_list.add_css_class("boxed-list")
        scroller.set_child(self.history_list)
        content.append(scroller)

        provider = Gtk.CssProvider()

        card_background_uri = (
            Path(__file__).resolve().parent
            / "assets"
            / "card-carbon-red.svg"
        ).as_uri()

        css_data = b"""
          .tool-window-title {
            font-size: 13px;
            font-weight: 700;
          }

          .tool-report-hero {
            background-color: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 15px;
            padding: 17px 18px;
          }

          .tool-report-kicker {
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 1.4px;
            color: rgba(255, 255, 255, 0.52);
          }

          .tool-report-title {
            font-size: 15px;
            font-weight: 700;
          }

          .tool-report-row {
            background-color: rgba(255, 255, 255, 0.028);
            border: 1px solid rgba(255, 255, 255, 0.065);
            border-radius: 11px;
            padding: 10px 12px;
          }

          .tool-report-label {
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 1.1px;
            color: rgba(255, 255, 255, 0.47);
          }

          .tool-report-value {
            font-size: 12px;
            font-weight: 500;
          }

          .tool-report-section {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.15px;
            color: rgba(255, 255, 255, 0.65);
            margin-top: 8px;
            margin-bottom: 2px;
          }

          .tool-report-body {
            font-size: 12px;
            color: rgba(255, 255, 255, 0.76);
          }

          .tool-mono {
            font-family: monospace;
            font-size: 11px;
          }

          .tool-status-good {
            color: #8ee6a8;
          }

          .tool-status-warn {
            color: #efc66d;
          }

          .tool-status-bad {
            color: #ff8b8b;
          }

          .tool-close-button {
            min-height: 34px;
            padding-left: 18px;
            padding-right: 18px;
          }

          .hw-reader-stage {
            background-color: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 17px 18px;
          }

          .hw-reader-tag {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.5px;
            color: rgba(255, 255, 255, 0.55);
          }

          .hw-reader-name {
            font-size: 18px;
            font-weight: 700;
          }

          .hw-reader-meta {
            font-size: 12px;
            color: rgba(255, 255, 255, 0.55);
          }

          .hw-status-card {
            background-color: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 14px;
            padding: 14px;
          }

          .hw-status-title {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.4px;
            color: rgba(255, 255, 255, 0.52);
          }

          .hw-status-value {
            font-size: 18px;
            font-weight: 700;
          }

          .hw-status-detail {
            font-size: 11px;
            color: rgba(255, 255, 255, 0.62);
          }

          .hw-status-good {
            color: #8ee6a8;
          }

          .hw-status-warn {
            color: #f1c875;
          }

          .hw-status-bad {
            color: #ff8b8b;
          }

          .hw-result {
            font-size: 12px;
            font-weight: 600;
          }

          .header-logo {
            margin-right: 2px;
          }

          .header-action {
            min-height: 34px;
            padding: 5px 11px;
          }

          .bank-card {
            background-color: #74101d;
            background-image: url("__CARD_BACKGROUND_URI__");
            background-size: cover;
            background-repeat: no-repeat;
            background-position: center;

            border:
              1px solid rgba(255, 255, 255, 0.11);

            border-radius: 22px;
            padding: 26px;
            color: white;

            box-shadow:
              inset 0 1px rgba(255, 255, 255, 0.045),
              inset 0 -20px 36px rgba(0, 0, 0, 0.08);
          }

          .card-logo {
            border-radius: 10px;
          }

          .card-brand {
            font-size: 20px;
            font-weight: 700;
            letter-spacing: 2px;
            color: white;
          }

          .card-caption {
            font-size: 11px;
            letter-spacing: 1px;
            color: rgba(255, 228, 230, 0.76);
          }

          .card-contactless {
            font-size: 18px;
            font-weight: 700;
            color: rgba(255, 255, 255, 0.85);
            padding: 6px;
          }

          .currency-block {
            background-color: rgba(12, 8, 10, 0.20);
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 13px;
            padding: 10px 13px;
          }

          .currency-code {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1.3px;
            color: rgba(255, 228, 230, 0.72);
          }

          .balance {
            font-size: 30px;
            font-weight: 700;
            color: white;
          }

          .card-identifier {
            font-size: 17px;
            font-family: monospace;
            color: rgba(255, 255, 255, 0.86);
            margin-top: 4px;
          }

          .card-chip {
            font-size: 28px;
            color: rgba(255, 255, 255, 0.68);
          }

          .caption-heading {
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
          }
        """

        css_data = css_data.replace(
            b"__CARD_BACKGROUND_URI__",
            card_background_uri.encode("utf-8"),
        )

        provider.load_from_data(
            css_data
        )

        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    @staticmethod
    def _entry(placeholder: str) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        return entry

    def _backend(self) -> CardBackend:
        if self.virtual_button.get_active():
            return VirtualCardBackend(self.store)
        selected = self.transport.get_selected()
        if selected == 0:
            return SunmiBackend(self.sunmi_url.get_text().strip() or "http://127.0.0.1:5380")
        if selected == 1:
            return create_pcsc_backend(reader_name=self.reader_name.get_text())
        return CardpeekBackend()

    def _ledger(self) -> LedgerClient:
        if self.api_mode.get_selected() == 0:
            return RealLedgerClient(
                self.api_url.get_text(),
                self.terminal_code.get_text(),
                self.terminal_key.get_text(),
                self.api_backend_id.get_text(),
            )
        return LedgerClient(
            self.api_url.get_text(),
            self.terminal_code.get_text(),
            self.terminal_key.get_text(),
        )

    def _settings_from_ui(self) -> AppSettings:
        mode = "virtual"
        if self.physical_button.get_active():
            mode = {0: "sunmi", 1: "pcsc", 2: "cardpeek"}.get(self.transport.get_selected(), "pcsc")
        return AppSettings(
            ledger_url=self.api_url.get_text(),
            terminal_code=self.terminal_code.get_text(),
            sunmi_url=self.sunmi_url.get_text(),
            mode=mode,
            reader=self.reader_name.get_text(),
            hardware_validation_enabled=self.settings.hardware_validation_enabled,
            ledger_mode="real" if self.api_mode.get_selected() == 0 else "sandbox",
            ledger_backend_id=self.api_backend_id.get_text(),
        )

    def _save_settings(self) -> None:
        try:
            self.settings = self._settings_from_ui()
            self.settings_store.save(self.settings)
        except (ValueError, LedgerError) as error:
            self._toast(str(error))
        else:
            self._toast("Configuración guardada sin la llave de terminal")

    def _show_diagnostics(self) -> None:
        def success(report: dict[str, Any]) -> None:
            self._present_report_window(
                'Compatibility Diagnostics',
                format_diagnostics(report),
            )

        self._run_async(lambda: collect_diagnostics(self._settings_from_ui()), success)

    def _mode_changed(self, _button: Gtk.ToggleButton) -> None:
        self._sync_widgets()

    def _sync_widgets(self) -> None:
        idle = self.busy_count == 0
        physical = self.physical_button.get_active()
        selected = self.transport.get_selected()
        for widget in [
            self.virtual_button, self.physical_button, self.transport, self.sunmi_url,
            self.reader_name, self.api_health_button, self.detect_button, self.read_button,
            self.sync_button, self.write_button, self.reload_button, self.charge_button,
            self.save_settings_button, self.diagnostics_button, self.hardware_validation_button,
            self.cardlab_button, self.write_validation_button, self.registry_button,
        ]:
            widget.set_sensitive(idle)
        self.transport_label.set_visible(physical)
        self.transport.set_visible(physical)
        self.sunmi_url.set_visible(physical and selected == 0)
        self.reader_name.set_visible(physical and selected == 1)
        self.api_backend_id.set_visible(self.api_mode.get_selected() == 0)
        notes = {
            0: "SUNMI: open Card Bridge and run\nadb forward tcp:5380 tcp:5380",
            1: "PC/SC nativo. Deja el lector en blanco para usar el primero.",
            2: "Alternativa Cardpeek mediante el servicio PC/SC.",
        }
        self.bridge_note.set_text(notes.get(selected, ""))
        self.bridge_note.set_visible(physical)
        self.card_mode.set_text(
            "PHYSICAL CARD" if physical else "VIRTUAL CARD"
        )
        if idle:
            self.spinner.stop()
        else:
            self.spinner.start()

    def _run_async(self, work: Callable[[], Any], success: Callable[[Any], None]) -> None:
        self.busy_count += 1
        self._sync_widgets()

        def task() -> None:
            try:
                result = work()
            except Exception as error:
                GLib.idle_add(self._finish_error, error)
            else:
                GLib.idle_add(self._finish_success, success, result)

        threading.Thread(target=task, daemon=True).start()

    def _finish_success(self, callback: Callable[[Any], None], result: Any) -> bool:
        try:
            callback(result)
        except Exception as error:
            self._toast(str(error))
        self.busy_count -= 1
        self._sync_widgets()
        return False

    def _finish_error(self, error: Exception) -> bool:
        logger.error(
            "Operación de interfaz fallida: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if isinstance(error, (CardError, LedgerError)):
            message = error.message if isinstance(error, CardError) else str(error)
        else:
            message = str(error)
        self._toast(message)
        self.busy_count -= 1
        self._sync_widgets()
        return False

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _render_state(self, state: CardState) -> None:
        self.current_state = state

        amount = state.balance_minor / 100
        self.balance_usd.set_text(f"${amount:,.2f}")

        # EUR queda independiente hasta que incorporemos saldo multi-divisa.
        self.balance_eur.set_text("€0.00")

        display_identifier = state.identifier.replace("SL-DEMO-", "SL-")
        self.card_identifier.set_text(display_identifier)
        self.identifier_entry.set_text(display_identifier)

    def _render_history(self) -> None:
        while child := self.history_list.get_first_child():
            self.history_list.remove(child)
        history = self.store.history()
        if not history:
            row = Gtk.Label(label="No operations recorded", xalign=0)
            row.set_margin_top(16)
            row.set_margin_bottom(16)
            row.set_margin_start(12)
            self.history_list.append(row)
            return
        names = {"WRITE_ID": "Identificador", "RELOAD": "Recarga", "CHARGE": "Cobro"}
        for operation in history:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            title = Gtk.Label(xalign=0)
            title.set_markup(
                f"<b>{names.get(operation.kind, operation.kind)}</b>  "
                f"{GLib.markup_escape_text(format_amount(operation.amount_minor))}"
            )
            box.append(title)
            detail = Gtk.Label(
                label=f"{operation.card_id} · saldo {format_amount(operation.balance_after_minor)}",
                xalign=0,
            )
            detail.add_css_class("caption")
            box.append(detail)
            sync = Gtk.Label(
                label=f"{operation.mode} · {operation.api_status} · {operation.api_message}",
                xalign=0,
                wrap=True,
            )
            sync.add_css_class("caption")
            sync.add_css_class("dim-label")
            box.append(sync)
            self.history_list.append(box)

    def _render_transactions(self) -> None:
        while child := self.transaction_list.get_first_child():
            self.transaction_list.remove(child)
        transactions = self.transaction_application.list_transactions(20)
        readiness = readiness_view(self.transaction_application.l3_readiness())
        try:
            cardlab = load_cardlab_status()
            cardlab_text = (
                f" · CARDLAB {cardlab['state']}"
                f" · APPLET {cardlab['applet_aid']}"
            )
        except Exception:
            cardlab_text = " · CARDLAB UNAVAILABLE"

        self.l3_summary.set_text(
            f"LAB READINESS: {readiness['lab_readiness']} · certification {readiness['certification']}"
            f" · physical {readiness['physical_validation']}"
            f"{cardlab_text}"
        )
        if not transactions:
            self.transaction_list.append(Gtk.Label(label="No transactions recorded", xalign=0))
            return
        for transaction in transactions:
            view = transaction_view(transaction)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.append(Gtk.Label(label=f"{view['operation_id']} · {view['status']}", xalign=0))
            box.append(Gtk.Label(
                label=(
                    f"{transaction['interface']} · {transaction['kernel_ref']} · "
                    f"{transaction['profile_id']} v{transaction['profile_version']}"
                ),
                xalign=0,
            ))
            box.append(Gtk.Label(
                label=(
                    f"Recovery: {view['recovery_action'] or 'NONE'} · "
                    f"Evidence: {view['result_reference'] or 'PENDING'}"
                ),
                xalign=0,
            ))
            if view["actions"]:
                actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                for action in view["actions"]:
                    button = Gtk.Button.new_with_label(action.capitalize())
                    if action == "review":
                        button.connect("clicked", lambda _button: self._toast("Revisión de operador requerida"))
                    else:
                        button.connect(
                            "clicked",
                            lambda _button, selected=action, operation_id=view["operation_id"]:
                            self._transaction_action(operation_id, selected),
                        )
                    actions.append(button)
                box.append(actions)
            self.transaction_list.append(box)

    def _transaction_action(self, operation_id: str, action: str) -> None:
        def work() -> dict[str, Any]:
            if action == "query":
                return self.transaction_application.query(operation_id)
            if action == "retry":
                return self.transaction_application.retry(operation_id)
            raise ValueError("Acción de transacción no soportada.")

        def success(_result: dict[str, Any]) -> None:
            self._render_transactions()
            self._toast(f"Transacción actualizada: {action}")

        self._run_async(work, success)


    def _show_registry(self) -> None:
        def success(report: dict[str, Any]) -> None:
            body = (
                f"Status:          {report['status']}\n"
                f"Result:          {report['result']}\n"
                f"Mode:            {report['mode']}\n\n"
                f"CARDLAB REGISTRY\n"
                f"Package AID:     {report['package_aid']}\n"
                f"Package present: {report['package_present']}\n"
                f"Applet AID:      {report['applet_aid']}\n"
                f"Applet present:  {report['applet_present']}\n\n"
                f"Read only:       {report['physical_modification']}\n"
                f"Auth signal:     {report['authentication_signal']}\n"
                f"Secret stored:   {report['authentication_secret_recorded']}\n\n"
                f"Captured: {report.get('captured_at') or 'N/A'}\n"
                f"Scope: {report.get('evidence_scope', 'HISTORICAL_RECORD')}\n"
                f"Evidence: {report['source_evidence']}"
            )

            self._present_report_window(
                'GlobalPlatform Registry · Read Only',
                body,
            )

        self._run_async(load_registry_status, success)


    def _show_write_validation(self) -> None:
        def success(report: dict[str, Any]) -> None:
            postflight = report.get("postflight") or {}

            body = (
                f"Result:                  {report['result']}\n"
                f"Physical validation:     {report['physical_validation']}\n"
                f"Evidence scope:          {report['evidence_scope']}\n"
                f"Captured:                {report['timestamp']}\n\n"
                f"Applet AID:              {report['applet_aid']}\n"
                f"Preserved AID:           {report['preserved_applet_aid']}\n\n"
                f"Write commands accepted: {report['write_commands_accepted']}\n"
                f"Write executed:          {report['write_executed']}\n"
                f"Container state:         {report['container_state']}\n\n"
                f"Preserved state match:   {postflight.get('preserved_state_match', False)}\n"
                f"Postflight:              {postflight.get('result', 'N/A')}\n\n"
                f"Evidence:\n{report['evidence_path']}"
            )

            self._present_report_window(
                'JavaCard Write Validation · Evidence',
                body,
            )

        self._run_async(load_write_exercise_status, success)

    def _show_cardlab(self) -> None:
        def success(report: dict[str, Any]) -> None:
            software = report["software"]
            physical = report["physical"]

            body = (
                f"Status:          {report['status']}\n"
                f"State:           {report['state']}\n"
                f"Package AID:     {report['package_aid']}\n"
                f"Applet AID:      {report['applet_aid']}\n\n"
                f"SOFTWARE\n"
                f"Regression:      {software['regression']}\n"
                f"CAP:             {software['cap']}\n"
                f"VerifyCAP:       {software['verifycap']}\n"
                f"jCardSim:        {software['jcardsim']}\n"
                f"Container:       {software['container']}\n\n"
                f"PHYSICAL\n"
                f"SELECT:          {physical['select']}\n"
                f"GET_INFO:        {physical['get_info']}\n"
                f"GET_STATUS:      {physical['get_status']}\n"
                f"READ_CONTAINER:  {physical['read_container']}\n\n"
                f"Reader: {report.get('reader') or 'N/A'}\n"
                f"Captured: {report.get('captured_at') or 'N/A'}\n"
                f"Scope: {report.get('evidence_scope', 'HISTORICAL_RECORD')}\n"
                f"Evidence: {report['source_evidence']}"
            )

            self._present_report_window(
                'CardLab · Physical Validation',
                body,
            )

        self._run_async(load_cardlab_status, success)


    def _show_l3_readiness(self) -> None:
        report = readiness_view(self.transaction_application.l3_readiness())
        gates = "\n".join(
            f"{gate['gate_id']}: {gate['status']}" for gate in report["gates"]
        )
        self._present_report_window(
            'EMV / L3 Readiness · LAB / SYNTHETIC',
            f"LAB READINESS: {report['lab_readiness']}\n{gates}\n\nCertification: {report['certification']}\nPhysical validation: {report['physical_validation']}",
        )

    def _present_report_window(
        self,
        heading: str,
        body: str,
    ) -> None:
        """Premium read-only report window for Card Studio."""

        display_heading = str(heading)

        # Keep internal semantics but clean the visual title.
        display_heading = display_heading.replace(
            " · LAB / SYNTHETIC",
            "",
        )
        display_heading = display_heading.replace(
            " · Read Only",
            "",
        )

        upper_heading = str(heading).upper()

        if "REGISTRY" in upper_heading:
            context = "GLOBALPLATFORM · READ ONLY"
        elif "READINESS" in upper_heading:
            context = "EMV / L3 · READINESS"
        elif "DIAGNOST" in upper_heading:
            context = "SYSTEM · DIAGNOSTICS"
        elif "CARDLAB" in upper_heading:
            context = "CARDLAB · STATUS"
        elif "WRITE" in upper_heading:
            context = "CARD · VALIDATION"
        else:
            context = "SWIFT LEDGER · CARD STUDIO"

        window = Gtk.Window()
        window.set_transient_for(self)
        window.set_modal(True)
        window.set_title(display_heading)
        window.set_default_size(700, 540)

        # Keep a reference while the window is alive.
        self._report_window = window

        header = Adw.HeaderBar()

        header_identity = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        logo_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "swift-ledger-logo.png"
        )

        if logo_path.exists():
            logo = Gtk.Image.new_from_file(
                str(logo_path)
            )
            logo.set_pixel_size(20)
            header_identity.append(logo)

        header_title = Gtk.Label(
            label=display_heading
        )
        header_title.add_css_class(
            "tool-window-title"
        )
        header_identity.append(header_title)

        header.set_title_widget(
            header_identity
        )

        window.set_titlebar(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )

        window.set_child(scroll)

        root = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
        )

        root.set_margin_top(22)
        root.set_margin_bottom(22)
        root.set_margin_start(22)
        root.set_margin_end(22)

        scroll.set_child(root)

        # ----------------------------------------------------
        # HERO
        # ----------------------------------------------------

        hero = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=5,
        )

        hero.add_css_class(
            "tool-report-hero"
        )

        kicker = Gtk.Label(
            label=context,
            xalign=0,
        )
        kicker.add_css_class(
            "tool-report-kicker"
        )

        hero.append(kicker)

        hero_title = Gtk.Label(
            label=display_heading,
            xalign=0,
            wrap=True,
        )
        hero_title.add_css_class(
            "tool-report-title"
        )

        hero.append(hero_title)

        root.append(hero)

        # ----------------------------------------------------
        # STRUCTURED BODY
        # ----------------------------------------------------

        report_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )

        root.append(report_box)

        body_lines = str(body or "").splitlines()

        previous_blank = False

        for raw_line in body_lines:
            line = raw_line.strip()

            if not line:
                if not previous_blank:
                    spacer = Gtk.Box()
                    spacer.set_size_request(-1, 5)
                    report_box.append(spacer)

                previous_blank = True
                continue

            previous_blank = False

            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()

                row = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL,
                    spacing=3,
                )
                row.add_css_class(
                    "tool-report-row"
                )

                key_label = Gtk.Label(
                    label=key.upper(),
                    xalign=0,
                )
                key_label.add_css_class(
                    "tool-report-label"
                )

                row.append(key_label)

                value_label = Gtk.Label(
                    label=value or "—",
                    xalign=0,
                    wrap=True,
                )
                value_label.set_selectable(True)
                value_label.set_max_width_chars(72)
                value_label.add_css_class(
                    "tool-report-value"
                )

                value_upper = value.upper()

                if (
                    any(
                        token in value_upper
                        for token in (
                            "PASS",
                            "CONFIRMED",
                            "COMPLETED",
                            "READY",
                        )
                    )
                    and "NOT_" not in value_upper
                    and "NOT " not in value_upper
                ):
                    value_label.add_css_class(
                        "tool-status-good"
                    )

                elif any(
                    token in value_upper
                    for token in (
                        "FAIL",
                        "ERROR",
                        "REJECTED",
                    )
                ):
                    value_label.add_css_class(
                        "tool-status-bad"
                    )

                elif any(
                    token in value_upper
                    for token in (
                        "PENDING",
                        "NOT_RUN",
                        "NOT EXECUTED",
                        "NOT_EXECUTED",
                        "INCONCLUSIVE",
                    )
                ):
                    value_label.add_css_class(
                        "tool-status-warn"
                    )

                # Paths, AIDs and long machine identifiers.
                compact = value.replace(" ", "")

                if (
                    "/" in value
                    or (
                        len(compact) >= 12
                        and all(
                            c in "0123456789ABCDEFabcdef"
                            for c in compact
                        )
                    )
                ):
                    value_label.add_css_class(
                        "tool-mono"
                    )

                row.append(value_label)
                report_box.append(row)

            else:
                # Short standalone uppercase lines become
                # visual section headers.
                if (
                    len(line) <= 42
                    and (
                        line.isupper()
                        or line.endswith("REGISTRY")
                    )
                ):
                    section = Gtk.Label(
                        label=line,
                        xalign=0,
                    )
                    section.add_css_class(
                        "tool-report-section"
                    )
                    report_box.append(section)

                else:
                    body_label = Gtk.Label(
                        label=line,
                        xalign=0,
                        wrap=True,
                    )
                    body_label.set_selectable(True)
                    body_label.set_max_width_chars(72)
                    body_label.add_css_class(
                        "tool-report-body"
                    )
                    report_box.append(body_label)

        # ----------------------------------------------------
        # FOOTER
        # ----------------------------------------------------

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        root.append(separator)

        footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        footer.set_halign(Gtk.Align.END)

        close_button = Gtk.Button.new_with_label(
            "Close"
        )
        close_button.add_css_class(
            "tool-close-button"
        )
        close_button.connect(
            "clicked",
            lambda _button: window.close(),
        )

        footer.append(close_button)
        root.append(footer)

        window.present()

    def _hardware_status_card(
        self,
        title: str,
    ) -> tuple[Gtk.Box, Gtk.Label, Gtk.Label]:
        card = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        card.set_hexpand(True)
        card.add_css_class("hw-status-card")

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("hw-status-title")
        card.append(title_label)

        value = Gtk.Label(label="WAITING", xalign=0)
        value.add_css_class("hw-status-value")
        card.append(value)

        detail = Gtk.Label(label="—", xalign=0, wrap=True)
        detail.add_css_class("hw-status-detail")
        card.append(detail)

        return card, value, detail

    def _set_hardware_status(
        self,
        value: Gtk.Label,
        detail: Gtk.Label,
        status: str,
        description: str,
        *,
        state: str = "neutral",
    ) -> None:
        for css_class in (
            "hw-status-good",
            "hw-status-warn",
            "hw-status-bad",
        ):
            value.remove_css_class(css_class)

        if state == "good":
            value.add_css_class("hw-status-good")
        elif state == "warn":
            value.add_css_class("hw-status-warn")
        elif state == "bad":
            value.add_css_class("hw-status-bad")

        value.set_text(status)
        detail.set_text(description)

    def _show_hardware_validation(self) -> None:
        existing = getattr(self, "_hardware_window", None)

        if existing is not None and existing.get_visible():
            existing.present()
            return

        window = Gtk.Window()
        window.set_transient_for(self)
        window.set_modal(True)
        window.set_title("Hardware Validation")
        window.set_default_size(820, 560)

        self._hardware_window = window

        header = Adw.HeaderBar()

        title_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )

        logo_path = (
            Path(__file__).resolve().parent
            / "assets"
            / "swift-ledger-logo.png"
        )

        if logo_path.exists():
            logo = Gtk.Image.new_from_file(str(logo_path))
            logo.set_pixel_size(22)
            title_box.append(logo)

        title = Gtk.Label(label="Hardware Validation")
        title.add_css_class("title")
        title_box.append(title)

        header.set_title_widget(title_box)
        window.set_titlebar(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        window.set_child(scroll)

        root = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
        )
        root.set_margin_top(24)
        root.set_margin_bottom(24)
        root.set_margin_start(24)
        root.set_margin_end(24)
        scroll.set_child(root)

        # Reader identity / hardware stage
        hero = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=7,
        )
        hero.add_css_class("hw-reader-stage")

        hero_tag = Gtk.Label(
            label="PC/SC HARDWARE",
            xalign=0,
        )
        hero_tag.add_css_class("hw-reader-tag")
        hero.append(hero_tag)

        self._hw_reader_name = Gtk.Label(
            label="Scanning for reader…",
            xalign=0,
            wrap=True,
        )
        self._hw_reader_name.add_css_class("hw-reader-name")
        hero.append(self._hw_reader_name)

        self._hw_reader_meta = Gtk.Label(
            label="Physical smart-card interface",
            xalign=0,
        )
        self._hw_reader_meta.add_css_class("hw-reader-meta")
        hero.append(self._hw_reader_meta)

        root.append(hero)

        cards = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        cards.set_homogeneous(True)

        device_card, self._hw_device_value, self._hw_device_detail = (
            self._hardware_status_card("DEVICE")
        )

        session_card, self._hw_session_value, self._hw_session_detail = (
            self._hardware_status_card("SESSION")
        )

        card_card, self._hw_card_value, self._hw_card_detail = (
            self._hardware_status_card("CARD")
        )

        cards.append(device_card)
        cards.append(session_card)
        cards.append(card_card)

        root.append(cards)

        separator = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL
        )
        root.append(separator)

        self._hw_result = Gtk.Label(
            label="Ready for hardware validation.",
            xalign=0,
            wrap=True,
        )
        self._hw_result.add_css_class("hw-result")
        root.append(self._hw_result)

        self._hw_evidence = Gtk.Label(
            label="",
            xalign=0,
            wrap=True,
        )
        self._hw_evidence.add_css_class("caption")
        self._hw_evidence.add_css_class("dim-label")
        root.append(self._hw_evidence)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        actions.set_halign(Gtk.Align.END)

        self_test = Gtk.Button.new_with_label("Dry-run Self Test")
        run = Gtk.Button.new_with_label("Run Validation")
        close = Gtk.Button.new_with_label("Close")

        run.add_css_class("suggested-action")

        self_test.connect(
            "clicked",
            lambda _button: self._run_hardware_validation_v2(True),
        )

        run.connect(
            "clicked",
            lambda _button: self._run_hardware_validation_v2(False),
        )

        close.connect(
            "clicked",
            lambda _button: window.close(),
        )

        actions.append(self_test)
        actions.append(run)
        actions.append(close)

        root.append(actions)

        # Quick reader discovery. No card connection is opened here.
        try:
            probe = create_pcsc_backend(
                reader_name=self.reader_name.get_text(),
            )
            readers = probe.listReaders()

            if readers:
                preferred = self.reader_name.get_text().strip()
                reader = (
                    preferred
                    if preferred in readers
                    else readers[0]
                )

                self._hw_reader_name.set_text(reader)

                self._set_hardware_status(
                    self._hw_device_value,
                    self._hw_device_detail,
                    "READER DETECTED",
                    f"PC/SC ready · {len(readers)} reader(s)",
                    state="good",
                )

                self._set_hardware_status(
                    self._hw_session_value,
                    self._hw_session_detail,
                    "NOT STARTED",
                    "Run validation to open the card session.",
                    state="neutral",
                )

                self._set_hardware_status(
                    self._hw_card_value,
                    self._hw_card_detail,
                    "NOT CHECKED",
                    "Card presence has not been tested yet.",
                    state="neutral",
                )
            else:
                self._hw_reader_name.set_text(
                    "No PC/SC reader detected"
                )

                self._set_hardware_status(
                    self._hw_device_value,
                    self._hw_device_detail,
                    "NOT CONNECTED",
                    "No PC/SC reader is currently available.",
                    state="warn",
                )

                self._set_hardware_status(
                    self._hw_session_value,
                    self._hw_session_detail,
                    "UNAVAILABLE",
                    "A reader is required to open a physical session.",
                    state="warn",
                )

                self._set_hardware_status(
                    self._hw_card_value,
                    self._hw_card_detail,
                    "UNKNOWN",
                    "Card state cannot be determined.",
                    state="neutral",
                )

        except Exception as error:
            self._hw_reader_name.set_text(
                "PC/SC subsystem unavailable"
            )

            self._set_hardware_status(
                self._hw_device_value,
                self._hw_device_detail,
                "ERROR",
                str(error),
                state="bad",
            )

        window.present()

    def _run_hardware_validation_v2(
        self,
        self_test: bool,
    ) -> None:
        self._set_hardware_status(
            self._hw_session_value,
            self._hw_session_detail,
            "RUNNING",
            "Opening validation session…",
            state="warn",
        )

        self._set_hardware_status(
            self._hw_card_value,
            self._hw_card_detail,
            "CHECKING",
            "Waiting for card and ATR…",
            state="warn",
        )

        self._hw_result.set_text(
            "Validation in progress…"
        )
        self._hw_evidence.set_text("")

        adapter = (
            DryRunAdapter()
            if self_test
            else create_pcsc_backend(
                reader_name=self.reader_name.get_text(),
            )
        )

        validator = HardwareValidator(
            adapter,
            preferred_reader=self.reader_name.get_text(),
        )

        self._run_async(
            validator.run,
            self._show_hardware_result,
        )

    def _show_hardware_result(
        self,
        report: dict[str, Any],
    ) -> None:
        window = getattr(self, "_hardware_window", None)

        if window is None:
            return

        reader_detected = bool(
            report.get("reader_detected")
        )
        card_detected = bool(
            report.get("card_detected")
        )

        mode = report.get("mode", "UNKNOWN")
        self_test = mode == "SIMULATED"

        reader_name = (
            report.get("reader_name")
            or "No reader detected"
        )

        self._hw_reader_name.set_text(
            "Self-test environment"
            if self_test
            else reader_name
        )

        self._hw_reader_meta.set_text(
            "Internal validation path"
            if self_test
            else "PC/SC · physical smart-card interface"
        )

        if reader_detected:
            self._set_hardware_status(
                self._hw_device_value,
                self._hw_device_detail,
                "CONNECTED",
                f"{reader_name} · PC/SC READY",
                state="good",
            )
        else:
            self._set_hardware_status(
                self._hw_device_value,
                self._hw_device_detail,
                "NOT CONNECTED",
                "No compatible reader was selected.",
                state="warn",
            )

        protocol = report.get("protocol") or "UNKNOWN"
        disconnect_clean = bool(
            report.get("disconnect_clean")
        )

        if card_detected:
            session_status = (
                "COMPLETE"
                if disconnect_clean
                else "INCONCLUSIVE"
            )

            session_detail = (
                f"{protocol} · "
                + (
                    "clean disconnect"
                    if disconnect_clean
                    else "disconnect not confirmed"
                )
            )

            self._set_hardware_status(
                self._hw_session_value,
                self._hw_session_detail,
                session_status,
                session_detail,
                state="good" if disconnect_clean else "warn",
            )
        else:
            self._set_hardware_status(
                self._hw_session_value,
                self._hw_session_detail,
                "NO CARD SESSION",
                f"{protocol} · no card connection established",
                state="warn",
            )

        atr = report.get("atr")

        if atr:
            atr_display = " ".join(
                atr[i:i + 2]
                for i in range(0, len(atr), 2)
            )
        else:
            atr_display = "ATR unavailable"

        selected_aid = report.get("selected_aid")

        if selected_aid:
            card_extra = (
                f"ATR {atr_display}\n"
                f"Applet {selected_aid}"
            )
        else:
            card_extra = (
                f"ATR {atr_display}\n"
                f"Result {report.get('result', 'UNKNOWN')}"
            )

        if card_detected:
            self._set_hardware_status(
                self._hw_card_value,
                self._hw_card_detail,
                "DETECTED",
                card_extra,
                state="good",
            )
        else:
            self._set_hardware_status(
                self._hw_card_value,
                self._hw_card_detail,
                "NOT PRESENT",
                report.get("result", "CARD_NOT_PRESENT"),
                state="warn",
            )

        physical = report.get(
            "physical_validation",
            "UNKNOWN",
        )

        result = report.get(
            "result",
            "UNKNOWN",
        )

        visible_mode = (
            "SELF TEST"
            if self_test
            else "PHYSICAL"
        )

        self._hw_result.set_text(
            f"{visible_mode} · {result} · "
            f"Physical validation: {physical}"
        )

        evidence = report.get("evidence_path")
        self._hw_evidence.set_text(
            f"Evidence: {evidence}"
            if evidence
            else ""
        )

    def _detect(self) -> None:
        backend = self._backend()
        self._run_async(backend.detect, lambda state: self._after_read(state, "Tarjeta detectada"))

    def _read(self) -> None:
        backend = self._backend()
        self._run_async(backend.read, lambda state: self._after_read(state, "Lectura completada"))

    def _after_read(self, state: CardState, message: str) -> None:
        self._render_state(state)
        self._toast(f"{message}: {state.identifier}")

    def _confirm_write(self) -> None:
        try:
            identifier = validate_card_id(self.identifier_entry.get_text())
        except CardError as error:
            self._toast(error.message)
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Escribir identificador propio",
            body=f"Se escribirá {identifier} en la tarjeta {self._backend().label}.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("write", "Write")
        dialog.set_response_appearance("write", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._write_confirmed, identifier)
        dialog.present()

    def _write_confirmed(self, dialog: Adw.MessageDialog, response: str, identifier: str) -> None:
        dialog.destroy()
        if response == "write":
            self._start_operation("WRITE_ID", 0, identifier)

    def _start_amount_operation(self, kind: str) -> None:
        try:
            amount = parse_amount(self.amount_entry.get_text())
        except CardError as error:
            self._toast(error.message)
            return
        self._start_operation(kind, amount, None)

    def _start_operation(self, kind: str, amount: int, identifier: str | None) -> None:
        backend = self._backend()

        def work() -> tuple[CardState, Operation]:
            return self.service.perform(backend, kind, amount, identifier, self._ledger())

        def success(result: tuple[CardState, Operation]) -> None:
            state, operation = result
            self._render_state(state)
            self._render_history()
            if operation.api_status == "RECORDED":
                self._toast(operation.api_message)
            else:
                self._toast(f"Operación local guardada; API pendiente: {operation.api_message}")

        self._run_async(work, success)

    def _check_api(self) -> None:
        def success(value: dict[str, Any]) -> None:
            if value.get("simulation") is False:
                identity = f"{value.get('service')} · {value.get('backendId')} · {value.get('environment')}"
                self.api_status.set_text(f"API REAL · {identity}\n{value.get('serverTime', '')}")
                self._toast(f"Backend real confirmado: {value.get('backendId')}")
            else:
                self.api_status.set_text(
                    f"API SANDBOX · {value.get('service', 'POS local')} · {value.get('serverTime', '')}"
                )
                self._toast("Conexión sandbox confirmada; no es el backend real")

        self._run_async(self._ledger().health, success)

    def _sync_pending(self) -> None:
        pending = [item for item in self.store.history() if item.api_status != "RECORDED"]
        if not pending:
            self._toast("No hay operaciones pendientes")
            return

        def work() -> tuple[int, str]:
            return self.service.sync_pending(self._ledger())

        def success(result: tuple[int, str]) -> None:
            recorded, last_error = result
            self._render_history()
            message = f"{recorded} operación(es) sincronizada(s)"
            self._toast(f"{message}. {last_error}" if last_error else message)

        self._run_async(work, success)


class CardStudioApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id="us.swiftledger.CardStudio")
        self.connect("activate", self._activate)

    def _activate(self, app: Adw.Application) -> None:
        window = self.get_active_window()
        if window is None:
            window = CardStudioWindow(app)
        window.present()


def main() -> int:
    return CardStudioApplication().run(["swift-ledger-card-studio"])
