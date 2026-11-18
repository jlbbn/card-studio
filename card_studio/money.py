from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class MoneyValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str
    exponent: int

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise MoneyValidationError("amount_minor debe ser un entero.")
        if self.amount_minor < 0:
            raise MoneyValidationError("amount_minor no puede ser negativo.")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise MoneyValidationError("currency debe ser un código ISO 4217 de tres letras mayúsculas.")
        if not isinstance(self.exponent, int) or isinstance(self.exponent, bool) or not 0 <= self.exponent <= 3:
            raise MoneyValidationError("exponent debe ser un entero entre 0 y 3.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "exponent": self.exponent,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Money":
        return cls(
            amount_minor=value["amount_minor"],
            currency=value["currency"],
            exponent=value["exponent"],
        )
