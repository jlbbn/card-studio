from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from .applet import APPLET_AID_CANDIDATES, ApduResponse, build_select_apdu, parse_apdu_response
from .backends import ApduCardBackend, check_status_word
from .model import CardError

SELECT_APPLET_APDU = build_select_apdu(APPLET_AID_CANDIDATES[0].aid).hex().upper()


def system_readers() -> list[Any]:
    try:
        from smartcard.System import readers
    except (ImportError, ModuleNotFoundError) as exc:
        raise CardError(
            "PCSC_UNAVAILABLE",
            "Falta el adaptador pyscard. La aplicación empaquetada debe incluirlo o instalar requirements.txt.",
        ) from exc
    try:
        return list(readers())
    except Exception as exc:
        raise CardError("PCSC_UNAVAILABLE", f"PC/SC no está disponible: {exc}") from exc


def _hresult_is(error: Exception, expected: int) -> bool:
    value = getattr(error, "hresult", None)
    return isinstance(value, int) and value & 0xFFFFFFFF == expected


def _card_not_present(error: Exception) -> bool:
    return _hresult_is(error, 0x8010000C)


def _card_removed(error: Exception) -> bool:
    return _hresult_is(error, 0x80100069)


def _transmit_timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or _hresult_is(error, 0x8010000A)


class PcscBackend(ApduCardBackend):
    def __init__(
        self,
        reader_name: str = "",
        reader_provider: Callable[[], list[Any]] | None = None,
        timeout: float = 15.0,
    ):
        self.reader_name = reader_name.strip()
        self.reader_provider = reader_provider or system_readers
        self.timeout = timeout
        self.mode = "SIMULATED" if reader_provider is not None else "PHYSICAL"
        self._reader_handle: Any | None = None
        self._connection: Any | None = None

    def list_readers(self) -> list[str]:
        try:
            return [str(reader) for reader in self.reader_provider()]
        except CardError:
            raise
        except Exception as exc:
            raise CardError("PCSC_UNAVAILABLE", f"PC/SC no está disponible: {exc}") from exc

    def listReaders(self) -> list[str]:
        return self.list_readers()

    def _reader(self) -> Any:
        try:
            readers = list(self.reader_provider())
        except CardError:
            raise
        except Exception as exc:
            raise CardError("PCSC_UNAVAILABLE", f"PC/SC no está disponible: {exc}") from exc
        if not readers:
            raise CardError("NO_READER", "No se detectó un lector PC/SC compatible.")
        if not self.reader_name:
            return readers[0]
        if self.reader_name not in [str(reader) for reader in readers]:
            raise CardError("READER_NOT_FOUND", f"No se encontró el lector seleccionado: {self.reader_name}")
        return next(reader for reader in readers if str(reader) == self.reader_name)

    def connect(self) -> "PcscBackend":
        if self._connection is not None:
            return self
        try:
            reader = self._reader()
        except CardError as error:
            if error.code == "NO_READER":
                raise CardError("READER_NOT_FOUND", error.message) from error
            raise
        try:
            connection = reader.createConnection()
        except Exception as exc:
            raise CardError("CARD_CONNECTION_FAILED", f"No se pudo abrir el lector: {exc}") from exc
        try:
            connection.connect()
        except Exception as exc:
            if _card_not_present(exc):
                raise CardError("CARD_NOT_PRESENT", "No hay una tarjeta presente en el lector.") from exc
            raise CardError("CARD_CONNECTION_FAILED", f"No se pudo conectar con la tarjeta: {exc}") from exc
        self._reader_handle = reader
        self._connection = connection
        return self

    def waitForCard(self, timeout_seconds: float = 0.0, poll_interval: float = 0.25) -> "PcscBackend":
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                return self.connect()
            except CardError as error:
                if error.code != "CARD_NOT_PRESENT" or time.monotonic() >= deadline:
                    raise
                time.sleep(poll_interval)

    def getReader(self) -> str:
        if self._reader_handle is not None:
            return str(self._reader_handle)
        try:
            return str(self._reader())
        except CardError as error:
            if error.code == "NO_READER":
                raise CardError("READER_NOT_FOUND", error.message) from error
            raise

    def _require_connection(self) -> Any:
        if self._connection is None:
            raise CardError("CARD_CONNECTION_FAILED", "No existe una sesión PC/SC abierta.")
        return self._connection

    def getATR(self) -> bytes:
        connection = self._require_connection()
        try:
            return bytes(connection.getATR())
        except Exception as exc:
            if _card_removed(exc):
                raise CardError("CARD_REMOVED", "La tarjeta fue retirada durante la sesión.") from exc
            raise CardError("CARD_CONNECTION_FAILED", f"No se pudo obtener el ATR: {exc}") from exc

    def getProtocol(self) -> str:
        connection = self._require_connection()
        try:
            value = connection.getProtocol()
        except (AttributeError, NotImplementedError):
            return "UNKNOWN"
        except Exception as exc:
            if _card_removed(exc):
                raise CardError("CARD_REMOVED", "La tarjeta fue retirada durante la sesión.") from exc
            raise CardError("CARD_CONNECTION_FAILED", f"No se pudo obtener el protocolo: {exc}") from exc
        return {0: "T=0", 1: "T=1"}.get(value, "UNKNOWN")

    def transmit(self, command: bytes | str) -> ApduResponse:
        connection = self._require_connection()
        try:
            raw_command = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        except (TypeError, ValueError) as exc:
            raise CardError("INVALID_APDU", "El comando APDU no contiene bytes hexadecimales válidos.") from exc
        if len(raw_command) < 4:
            raise CardError("INVALID_APDU", "El comando APDU debe incluir CLA, INS, P1 y P2.")
        try:
            data, sw1, sw2 = connection.transmit(list(raw_command))
            if not isinstance(sw1, int) or not isinstance(sw2, int) or not 0 <= sw1 <= 255 or not 0 <= sw2 <= 255:
                raise CardError("INVALID_RESPONSE", "PC/SC devolvió un status word inválido o truncado.")
            return parse_apdu_response(bytes(data) + bytes([sw1, sw2]))
        except CardError:
            raise
        except Exception as exc:
            if _card_removed(exc):
                raise CardError("CARD_REMOVED", "La tarjeta fue retirada durante la transmisión.") from exc
            if _transmit_timeout(exc):
                raise CardError("TRANSMIT_TIMEOUT", "La tarjeta no respondió dentro del tiempo permitido.") from exc
            raise CardError("TRANSMIT_FAILED", f"La transmisión APDU falló: {exc}") from exc

    def selectApplet(self, aid: str | None = None, occurrence: str = "first") -> ApduResponse:
        command = build_select_apdu(aid or APPLET_AID_CANDIDATES[0].aid, occurrence=occurrence)
        return self.transmit(command)

    def read(self):
        # El hardware físico usa GET DATA P1/P2=0001.
        # Los backends simulados conservan el contrato legacy 0002.
        if self.mode == "PHYSICAL":
            return self.command("80CA000100")
        return super().read()

    def disconnect(self) -> None:
        connection = self._connection
        self._connection = None
        self._reader_handle = None
        if connection is None:
            return
        try:
            connection.disconnect()
        except Exception as exc:
            raise CardError("CARD_CONNECTION_FAILED", f"No se pudo cerrar la sesión PC/SC: {exc}") from exc

    def exchange(self, apdu_hex: str) -> bytes:
        own_session = self._connection is None
        if own_session:
            try:
                self.connect()
            except CardError as error:
                if error.code == "READER_NOT_FOUND" and not self.reader_name:
                    raise CardError("NO_READER", "No se detectó un lector PC/SC compatible.") from error
                raise
        try:
            selected = self.selectApplet()
            if selected.sw1sw2 != "9000":
                raise CardError("INCOMPATIBLE_CARD", "La tarjeta no contiene el applet Swift Ledger.")
            response = self.transmit(apdu_hex)
            return check_status_word(int(response.sw1sw2, 16), response.data.hex())
        except CardError:
            raise
        except Exception as exc:
            raise CardError("TRANSMIT_FAILED", f"El intercambio PC/SC falló: {exc}") from exc
        finally:
            if own_session:
                try:
                    self.disconnect()
                except CardError:
                    if sys.exc_info()[0] is None:
                        raise


class LinuxPcscBackend(PcscBackend):
    label = "PC/SC · Linux"


class MacOSPcscBackend(PcscBackend):
    label = "PC/SC · macOS"


def create_pcsc_backend(
    platform: str | None = None,
    reader_name: str = "",
    reader_provider: Callable[[], list[Any]] | None = None,
) -> PcscBackend:
    selected = sys.platform if platform is None else platform
    is_macos = selected == "macos" or selected.startswith("darwin")
    backend_class = MacOSPcscBackend if is_macos else LinuxPcscBackend
    return backend_class(reader_name, reader_provider, 15.0)
