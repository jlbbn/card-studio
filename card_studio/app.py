from __future__ import annotations

import logging
import threading
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
from .ledger import LedgerClient, LedgerError
from .model import (
    CardError,
    CardState,
    Operation,
    format_amount,
    parse_amount,
    validate_card_id,
)
from .pcsc import create_pcsc_backend
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
        title = Gtk.Label(label=f"Card Studio {__version__} · build {__build__}")
        title.add_css_class("title")
        header.set_title_widget(title)
        self.cardlab_button = Gtk.Button.new_with_label("CardLab")
        self.cardlab_button.connect(
            "clicked",
            lambda _button: self._show_cardlab(),
        )
        header.pack_end(self.cardlab_button)

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
        self.api_health_button = Gtk.Button.new_with_label("Probar API")
        self.api_health_button.connect("clicked", lambda _button: self._check_api())
        header.pack_end(self.api_health_button)
        self.diagnostics_button = Gtk.Button.new_with_label("Diagnosticar")
        self.diagnostics_button.connect("clicked", lambda _button: self._show_diagnostics())
        header.pack_end(self.diagnostics_button)
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root.append(body)
        body.set_vexpand(True)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        sidebar.set_size_request(300, -1)
        sidebar.set_margin_top(24)
        sidebar.set_margin_bottom(24)
        sidebar.set_margin_start(24)
        sidebar.set_margin_end(18)
        body.append(sidebar)

        mode_title = Gtk.Label(label="MODO DE OPERACIÓN", xalign=0)
        mode_title.add_css_class("caption-heading")
        sidebar.append(mode_title)
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.virtual_button = Gtk.ToggleButton.new_with_label("Virtual")
        self.physical_button = Gtk.ToggleButton.new_with_label("Físico")
        self.physical_button.set_group(self.virtual_button)
        self.virtual_button.set_active(True)
        self.virtual_button.add_css_class("accent")
        self.virtual_button.connect("toggled", self._mode_changed)
        self.physical_button.connect("toggled", self._mode_changed)
        mode_box.append(self.virtual_button)
        mode_box.append(self.physical_button)
        mode_box.set_homogeneous(True)
        sidebar.append(mode_box)

        self.transport_label = Gtk.Label(label="Transporte físico", xalign=0)
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

        api_title = Gtk.Label(label="SWIFT LEDGER · API SANDBOX", xalign=0)
        api_title.add_css_class("caption-heading")
        sidebar.append(api_title)
        self.api_url = self._entry("https://pos.ejemplo.local")
        self.api_url.set_text(self.settings.ledger_url)
        self.terminal_code = self._entry("Código de terminal sandbox")
        self.terminal_code.set_text(self.settings.terminal_code)
        self.terminal_key = self._entry("Llave base64url de 32 bytes")
        self.terminal_key.set_visibility(False)
        for label, widget in [
            ("URL de Swift Ledger", self.api_url),
            ("Código de terminal", self.terminal_code),
            ("Llave de terminal · no se guarda", self.terminal_key),
        ]:
            caption = Gtk.Label(label=label, xalign=0)
            caption.add_css_class("caption")
            caption.add_css_class("dim-label")
            sidebar.append(caption)
            sidebar.append(widget)
        self.save_settings_button = Gtk.Button.new_with_label("Guardar configuración no secreta")
        self.save_settings_button.connect("clicked", lambda _button: self._save_settings())
        sidebar.append(self.save_settings_button)
        self.api_status = Gtk.Label(label="API sin comprobar", xalign=0, wrap=True)
        self.api_status.add_css_class("caption")
        sidebar.append(self.api_status)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(6)
        content.set_margin_end(24)
        content.set_hexpand(True)
        body.append(content)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.detect_button = Gtk.Button.new_with_label("Detectar")
        self.detect_button.add_css_class("suggested-action")
        self.detect_button.connect("clicked", lambda _button: self._detect())
        self.read_button = Gtk.Button.new_with_label("Leer")
        self.read_button.connect("clicked", lambda _button: self._read())
        self.sync_button = Gtk.Button.new_with_label("Sincronizar pendientes")
        self.sync_button.connect("clicked", lambda _button: self._sync_pending())
        for button in [self.detect_button, self.read_button, self.sync_button]:
            toolbar.append(button)
        self.spinner = Gtk.Spinner()
        toolbar.append(self.spinner)
        content.append(toolbar)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("bank-card")
        card.set_margin_top(6)
        card.set_margin_bottom(6)
        brand = Gtk.Label(label="SWIFT LEDGER", xalign=0)
        brand.add_css_class("card-brand")
        card.append(brand)
        self.card_mode = Gtk.Label(label="TARJETA VIRTUAL · SIMULACIÓN", xalign=0)
        self.card_mode.add_css_class("card-caption")
        card.append(self.card_mode)
        self.balance = Gtk.Label(xalign=0)
        self.balance.add_css_class("balance")
        card.append(self.balance)
        self.card_identifier = Gtk.Label(xalign=0)
        self.card_identifier.add_css_class("card-identifier")
        card.append(self.card_identifier)
        content.append(card)

        editor = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.identifier_entry = self._entry("SL-DEMO-5968")
        self.identifier_entry.set_hexpand(True)
        self.write_button = Gtk.Button.new_with_label("Escribir identificador")
        self.write_button.connect("clicked", lambda _button: self._confirm_write())
        editor.append(self.identifier_entry)
        editor.append(self.write_button)
        content.append(editor)

        amount_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.amount_entry = self._entry("10.00")
        self.amount_entry.set_hexpand(True)
        self.reload_button = Gtk.Button.new_with_label("Simular recarga")
        self.charge_button = Gtk.Button.new_with_label("Simular cobro")
        self.reload_button.connect("clicked", lambda _button: self._start_amount_operation("RELOAD"))
        self.charge_button.connect("clicked", lambda _button: self._start_amount_operation("CHARGE"))
        self.charge_button.add_css_class("destructive-action")
        amount_box.append(self.amount_entry)
        amount_box.append(self.reload_button)
        amount_box.append(self.charge_button)
        content.append(amount_box)

        disclaimer = Gtk.Label(
            label="Saldo de demostración. No es dinero real, no usa PAN/CVV y no inicia cargos bancarios.",
            xalign=0,
            wrap=True,
        )
        disclaimer.add_css_class("caption")
        disclaimer.add_css_class("dim-label")
        content.append(disclaimer)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        transactions_title = Gtk.Label(label="TRANSACTIONS · LAB / SYNTHETIC", xalign=0)
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

        history_title = Gtk.Label(label="ACTIVIDAD LOCAL Y API", xalign=0)
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
        provider.load_from_data(b"""
          .bank-card { background: linear-gradient(135deg, #172554, #1d4ed8); border-radius: 22px; padding: 26px; color: white; }
          .card-brand { font-size: 20px; font-weight: 700; letter-spacing: 2px; color: white; }
          .card-caption { font-size: 11px; letter-spacing: 1px; color: #bfdbfe; }
          .balance { font-size: 34px; font-weight: 700; color: white; margin-top: 18px; }
          .card-identifier { font-size: 17px; font-family: monospace; color: #dbeafe; margin-top: 14px; }
          .caption-heading { font-size: 11px; font-weight: 700; letter-spacing: 1px; }
        """)
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
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Diagnóstico de compatibilidad",
                body=format_diagnostics(report),
            )
            dialog.add_response("ok", "Aceptar")
            dialog.present()

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
            self.cardlab_button, self.registry_button,
        ]:
            widget.set_sensitive(idle)
        self.transport_label.set_visible(physical)
        self.transport.set_visible(physical)
        self.sunmi_url.set_visible(physical and selected == 0)
        self.reader_name.set_visible(physical and selected == 1)
        notes = {
            0: "SUNMI: abre Card Bridge y ejecuta\nadb forward tcp:5380 tcp:5380",
            1: "PC/SC nativo. Deja el lector en blanco para usar el primero.",
            2: "Alternativa Cardpeek mediante el servicio PC/SC.",
        }
        self.bridge_note.set_text(notes.get(selected, ""))
        self.bridge_note.set_visible(physical)
        self.card_mode.set_text(
            "TARJETA FÍSICA · SIMULACIÓN" if physical else "TARJETA VIRTUAL · SIMULACIÓN"
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
        self.balance.set_text(format_amount(state.balance_minor))
        self.card_identifier.set_text(state.identifier)
        self.identifier_entry.set_text(state.identifier)

    def _render_history(self) -> None:
        while child := self.history_list.get_first_child():
            self.history_list.remove(child)
        history = self.store.history()
        if not history:
            row = Gtk.Label(label="Sin operaciones registradas", xalign=0)
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
            self.transaction_list.append(Gtk.Label(label="Sin transacciones LAB", xalign=0))
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
                f"Evidence: {report['source_evidence']}"
            )

            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="GlobalPlatform Registry · Read Only",
                body=body,
            )
            dialog.add_response("ok", "Aceptar")
            dialog.present()

        self._run_async(load_registry_status, success)

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
                f"Evidence: {report['source_evidence']}"
            )

            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="CardLab · Physical Validation",
                body=body,
            )
            dialog.add_response("ok", "Aceptar")
            dialog.present()

        self._run_async(load_cardlab_status, success)


    def _show_l3_readiness(self) -> None:
        report = readiness_view(self.transaction_application.l3_readiness())
        gates = "\n".join(
            f"{gate['gate_id']}: {gate['status']}" for gate in report["gates"]
        )
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="EMV / L3 Readiness · LAB / SYNTHETIC",
            body=(
                f"LAB READINESS: {report['lab_readiness']}\n{gates}\n\n"
                f"Certification: {report['certification']}\n"
                f"Physical validation: {report['physical_validation']}"
            ),
        )
        dialog.add_response("close", "Cerrar")
        dialog.present()

    def _show_hardware_validation(self) -> None:
        software = "PASS" if self.startup.get("software_ready") else "FAIL"
        body = "\n".join([
            f"Card Studio {__version__} · build {__build__}",
            f"Platform: {self.startup.get('platform', 'unknown')} · {self.startup.get('architecture', 'native')}",
            f"Software readiness: {software}",
            "Canonical AID: UNRESOLVED",
            "Physical validation: PENDING",
            "Connect the supported reader only when available.",
        ])
        if self.startup.get("startup_error"):
            body += f"\nStartup: {self.startup['startup_error']}"
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Hardware Validation",
            body=body,
        )
        dialog.add_response("close", "Cerrar")
        dialog.add_response("dry", "Dry-run · SIMULATED")
        dialog.add_response("run", "Run Validation")
        dialog.set_response_appearance("run", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._hardware_validation_response)
        dialog.present()

    def _hardware_validation_response(self, dialog: Adw.MessageDialog, response: str) -> None:
        dialog.destroy()
        if response not in {"run", "dry"}:
            return
        adapter = DryRunAdapter() if response == "dry" else create_pcsc_backend()
        validator = HardwareValidator(adapter, preferred_reader=self.reader_name.get_text())
        self._run_async(validator.run, self._show_hardware_result)

    def _show_hardware_result(self, report: dict[str, Any]) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Hardware Validation · {report['mode']}",
            body=validation_summary(report),
        )
        dialog.add_response("close", "Cerrar")
        dialog.present()

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
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("write", "Escribir")
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
                self._toast("Operación y registro sandbox completados")
            else:
                self._toast(f"Operación local guardada; API pendiente: {operation.api_message}")

        self._run_async(work, success)

    def _check_api(self) -> None:
        def success(value: dict[str, Any]) -> None:
            self.api_status.set_text(f"API sandbox disponible · {value.get('serverTime', '')}")
            self._toast("Conexión con Swift Ledger confirmada")

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
