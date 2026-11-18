from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any


class CardSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CardSessionInfo:
    reader: str
    protocol: str
    atr: str


class CardSession:
    """
    Dueño único de la conexión física a la tarjeta.

    Regla:
    - conecta una sola vez
    - mantiene la misma conexión
    - transmite APDUs sobre esa conexión
    - desconecta sólo al cerrar explícitamente la sesión
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._lock = RLock()
        self._connected = False
        self._info: CardSessionInfo | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def info(self) -> CardSessionInfo | None:
        return self._info

    def open(self) -> CardSessionInfo:
        with self._lock:
            if self._connected:
                if self._info is None:
                    raise CardSessionError("Session marked connected without info")
                return self._info

            self._backend.connect()

            try:
                reader = getattr(
                    self._backend,
                    "reader_name",
                    None,
                )

                if not reader:
                    getter = getattr(
                        self._backend,
                        "getReader",
                        None,
                    )

                    if callable(getter):
                        reader = getter()

                atr = getattr(
                    self._backend,
                    "atr",
                    None,
                )

                if atr is None:
                    getter = getattr(
                        self._backend,
                        "getATR",
                        None,
                    )

                    if callable(getter):
                        atr = getter()

                protocol = getattr(
                    self._backend,
                    "protocol",
                    None,
                )

                if (
                    not protocol
                    or str(protocol).upper()
                    == "UNKNOWN"
                ):
                    getter = getattr(
                        self._backend,
                        "getProtocol",
                        None,
                    )

                    if callable(getter):
                        protocol = getter()

                if isinstance(
                    atr,
                    (
                        bytes,
                        bytearray,
                        memoryview,
                    ),
                ):
                    atr_text = bytes(
                        atr
                    ).hex().upper()

                else:
                    atr_text = str(
                        atr or ""
                    )

                self._info = CardSessionInfo(
                    reader=str(
                        reader or "UNKNOWN"
                    ),
                    protocol=str(
                        protocol or "UNKNOWN"
                    ),
                    atr=atr_text,
                )

                self._connected = True

                return self._info

            except Exception:
                try:
                    self._backend.disconnect()
                finally:
                    self._connected = False
                    self._info = None

                raise

    def transmit(self, command: bytes | str):
        with self._lock:
            if not self._connected:
                raise CardSessionError("Card session is not open")

            return self._backend.transmit(command)

    def close(self) -> None:
        with self._lock:
            if not self._connected:
                return

            try:
                self._backend.disconnect()
            finally:
                self._connected = False
                self._info = None

    def __enter__(self) -> "CardSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
