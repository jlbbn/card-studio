from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_BALANCE_MINOR = 9_999_999
CARD_ID_PATTERN = re.compile(r"^SL-[A-Z0-9][A-Z0-9-]{2,29}$")


class CardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_card_id(value: str) -> str:
    identifier = value.strip().upper()
    if not CARD_ID_PATTERN.fullmatch(identifier):
        raise CardError(
            "INVALID_CARD_ID",
            "Usa SL- seguido de 3 a 30 letras, números o guiones.",
        )
    return identifier


def validate_amount(amount_minor: int) -> int:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise CardError("INVALID_AMOUNT", "El importe debe ser un entero en centavos.")
    if amount_minor <= 0 or amount_minor > MAX_BALANCE_MINOR:
        raise CardError("INVALID_AMOUNT", "Captura un importe entre $0.01 y $99,999.99 MXN.")
    return amount_minor


def parse_amount(text: str) -> int:
    normalized = text.strip().replace("$", "").replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", normalized):
        raise CardError("INVALID_AMOUNT", "Captura un importe con máximo dos decimales.")
    whole, _, fraction = normalized.partition(".")
    return validate_amount(int(whole) * 100 + int(fraction.ljust(2, "0")))


def format_amount(amount_minor: int) -> str:
    return f"${amount_minor / 100:,.2f} MXN"


@dataclass(frozen=True)
class CardState:
    identifier: str
    balance_minor: int
    currency: str = "MXN"

    def __post_init__(self) -> None:
        validate_card_id(self.identifier)
        if not isinstance(self.balance_minor, int) or not 0 <= self.balance_minor <= MAX_BALANCE_MINOR:
            raise CardError("INVALID_BALANCE", "El saldo de la tarjeta no es válido.")
        if self.currency != "MXN":
            raise CardError("INVALID_CURRENCY", "Card Studio solo simula saldo MXN.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "balanceMinor": self.balance_minor,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CardState":
        return cls(
            identifier=value["identifier"],
            balance_minor=value["balanceMinor"],
            currency=value.get("currency", "MXN"),
        )


@dataclass
class Operation:
    mode: str
    kind: str
    card_id: str
    amount_minor: int
    balance_before_minor: int
    balance_after_minor: int
    operation_id: str
    idempotency_key: str
    occurred_at: str
    currency: str = "MXN"
    api_status: str = "PENDING"
    api_message: str = "Pendiente de sincronizar"

    @classmethod
    def create(
        cls,
        mode: str,
        kind: str,
        card_id: str,
        amount_minor: int,
        balance_before_minor: int,
        balance_after_minor: int,
    ) -> "Operation":
        operation_id = str(uuid.uuid4())
        return cls(
            mode=mode,
            kind=kind,
            card_id=card_id,
            amount_minor=amount_minor,
            balance_before_minor=balance_before_minor,
            balance_after_minor=balance_after_minor,
            operation_id=operation_id,
            idempotency_key=f"cs-{operation_id}",
            occurred_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def to_api(self) -> dict[str, Any]:
        return {
            "operationId": self.operation_id,
            "idempotencyKey": self.idempotency_key,
            "mode": self.mode,
            "kind": self.kind,
            "cardId": self.card_id,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "balanceBeforeMinor": self.balance_before_minor,
            "balanceAfterMinor": self.balance_after_minor,
            "occurredAt": self.occurred_at,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.to_api()
        value.update({"apiStatus": self.api_status, "apiMessage": self.api_message})
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Operation":
        return cls(
            mode=value["mode"],
            kind=value["kind"],
            card_id=value["cardId"],
            amount_minor=value["amountMinor"],
            balance_before_minor=value["balanceBeforeMinor"],
            balance_after_minor=value["balanceAfterMinor"],
            operation_id=value["operationId"],
            idempotency_key=value["idempotencyKey"],
            occurred_at=value["occurredAt"],
            currency=value.get("currency", "MXN"),
            api_status=value.get("apiStatus", "PENDING"),
            api_message=value.get("apiMessage", "Pendiente de sincronizar"),
        )


def transition(state: CardState, kind: str, amount_minor: int = 0, identifier: str | None = None) -> CardState:
    if kind == "WRITE_ID":
        return CardState(validate_card_id(identifier or ""), state.balance_minor)
    validate_amount(amount_minor)
    if kind == "RELOAD":
        balance = state.balance_minor + amount_minor
        if balance > MAX_BALANCE_MINOR:
            raise CardError("BALANCE_LIMIT", "La recarga excede el saldo máximo de demostración.")
        return CardState(state.identifier, balance)
    if kind == "CHARGE":
        if amount_minor > state.balance_minor:
            raise CardError("INSUFFICIENT_FUNDS", "El saldo de demostración no es suficiente.")
        return CardState(state.identifier, state.balance_minor - amount_minor)
    raise CardError("INVALID_OPERATION", "Operación no reconocida.")


def read_apdu() -> str:
    return "80CA000200"


def write_id_apdu(identifier: str) -> str:
    encoded = validate_card_id(identifier).encode("ascii")
    return f"80DA0000{len(encoded):02X}{encoded.hex().upper()}"


def amount_apdu(kind: str, amount_minor: int) -> str:
    validate_amount(amount_minor)
    if kind not in {"RELOAD", "CHARGE"}:
        raise CardError("INVALID_OPERATION", "La operación no admite importe.")
    instruction = "D0" if kind == "RELOAD" else "D1"
    return f"80{instruction}000004{amount_minor:08X}"


def parse_state_response(data: bytes) -> CardState:
    try:
        text = data.decode("ascii")
        parts = text.split("|")

        # Contrato SL1 existente.
        if len(parts) == 4:
            version, identifier, balance_hex, currency = parts
            if version == "SL1" and re.fullmatch(r"[0-9A-F]{8}", balance_hex):
                return CardState(
                    validate_card_id(identifier),
                    int(balance_hex, 16),
                    currency,
                )

        # Applet físico demo existente.
        # SWIFT LEDGER DEMO|NONPAYMENT|5968
        if (
            len(parts) == 3
            and parts[0] == "SWIFT LEDGER DEMO"
            and parts[1] == "NONPAYMENT"
            and re.fullmatch(r"[0-9]{4}", parts[2])
        ):
            return CardState(
                validate_card_id(f"SL-DEMO-{parts[2]}"),
                0,
                "MXN",
            )

        raise ValueError

    except (ValueError, UnicodeDecodeError) as exc:
        raise CardError(
            "INVALID_CARD_RESPONSE",
            "La tarjeta devolvió una respuesta no reconocida.",
        ) from exc
