from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from card_studio.transactions.model import TransactionRequest, TransactionType


class TransactionSemanticsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KernelTransactionSemantics:
    transaction_type: str
    card_kernel_mode: str
    transaction_code: int | None
    original_link: str

    def to_evidence(self) -> dict[str, Any]:
        return {
            "transaction_type": self.transaction_type,
            "card_kernel_mode": self.card_kernel_mode,
            "transaction_code": f"{self.transaction_code:02X}" if self.transaction_code is not None else None,
            "original_link": self.original_link,
            "second_approval": False,
            "mode": "LAB_ONLY",
        }


class KernelTransactionSemanticsPolicy:
    @classmethod
    def from_profile(cls, profile: Any) -> "KernelTransactionSemanticsPolicy | None":
        raw = profile.scheme_parameters.get("kernel_transaction_semantics")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise TransactionSemanticsError(
                "INVALID_TRANSACTION_SEMANTICS",
                "kernel_transaction_semantics requiere schema_version=1.",
            )
        expected = {
            "purchase": ("REQUIRED", 0x00, "NONE"),
            "refund": ("REQUIRED", 0x20, "REQUEST_ORIGINAL"),
            "reversal": ("HOST_ONLY", None, "HOST_ORIGINAL"),
        }
        for transaction_type, (card_mode, code, original_link) in expected.items():
            item = raw.get(transaction_type)
            if not isinstance(item, Mapping) or item.get("card_kernel_mode") != card_mode:
                raise TransactionSemanticsError(
                    "INVALID_TRANSACTION_SEMANTICS",
                    f"Semántica card_kernel_mode inválida para {transaction_type}.",
                )
            if item.get("original_link") != original_link:
                raise TransactionSemanticsError(
                    "INVALID_TRANSACTION_SEMANTICS",
                    f"Semántica original_link inválida para {transaction_type}.",
                )
            raw_code = item.get("transaction_code")
            if code is None:
                if raw_code is not None:
                    raise TransactionSemanticsError(
                        "INVALID_TRANSACTION_SEMANTICS",
                        "Reversal HOST_ONLY no admite transaction_code de tarjeta.",
                    )
            else:
                try:
                    parsed = bytes.fromhex(raw_code)
                except (TypeError, ValueError) as exc:
                    raise TransactionSemanticsError(
                        "INVALID_TRANSACTION_SEMANTICS",
                        f"transaction_code inválido para {transaction_type}.",
                    ) from exc
                if parsed != bytes((code,)):
                    raise TransactionSemanticsError(
                        "INVALID_TRANSACTION_SEMANTICS",
                        f"transaction_code no revisado para {transaction_type}.",
                    )
        return cls()

    def resolve(self, request: TransactionRequest) -> KernelTransactionSemantics:
        selected = TransactionType(request.transaction_type)
        if selected == TransactionType.PURCHASE:
            return KernelTransactionSemantics(selected.value, "REQUIRED", 0x00, "NONE")
        if selected == TransactionType.REFUND:
            if not request.original_operation_id or request.original_operation_id == request.operation_id:
                raise TransactionSemanticsError(
                    "REFUND_ORIGINAL_REQUIRED",
                    "Refund de kernel requiere una operación original distinta.",
                )
            return KernelTransactionSemantics(selected.value, "REQUIRED", 0x20, "REQUEST_ORIGINAL")
        return KernelTransactionSemantics(selected.value, "HOST_ONLY", None, "HOST_ORIGINAL")

    def assert_card_kernel(self, request: TransactionRequest) -> KernelTransactionSemantics:
        semantics = self.resolve(request)
        if semantics.card_kernel_mode != "REQUIRED":
            raise TransactionSemanticsError(
                "REVERSAL_CARD_KERNEL_NOT_REQUIRED",
                "Reversal es compensación HOST_ONLY; no selecciona tarjeta ni genera otra aprobación.",
            )
        return semantics
