from __future__ import annotations

from typing import Any

from .card_session import CardSession
from .model import CardError


class GlobalPlatformTransport:
    """
    Transporte GlobalPlatform sobre una CardSession ya abierta.

    Esta clase:
    - NO conecta la tarjeta
    - NO desconecta la tarjeta
    - NO crea otro proceso
    - NO crea otra sesión PC/SC

    Toda APDU GlobalPlatform viaja por la misma CardSession.
    """

    def __init__(self, session: CardSession) -> None:
        self._session = session

    @property
    def session(self) -> CardSession:
        return self._session

    def transmit(self, command: bytes | str) -> Any:
        if not self._session.connected:
            raise CardError(
                "GP_SESSION_NOT_OPEN",
                "GlobalPlatform requiere una CardSession abierta.",
            )

        return self._session.transmit(command)

    def transmit_hex(self, command: str) -> Any:
        normalized = "".join(command.split()).upper()

        if len(normalized) < 8 or len(normalized) % 2:
            raise CardError(
                "INVALID_GP_APDU",
                "La APDU GlobalPlatform no es válida.",
            )

        try:
            bytes.fromhex(normalized)
        except ValueError as exc:
            raise CardError(
                "INVALID_GP_APDU",
                "La APDU GlobalPlatform contiene caracteres no hexadecimales.",
            ) from exc

        return self.transmit(normalized)
