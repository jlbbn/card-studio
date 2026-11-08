from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from card_studio.transactions.model import TransactionRequest


class HostError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthorizationHostAdapter(ABC):
    host_ref = "abstract"
    host_version = "1"
    mode = "EXTERNAL_UNVERIFIED"

    @abstractmethod
    def authorize(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def query(self, operation_id: str) -> Mapping[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def reverse(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        raise NotImplementedError

    def refund(self, request: TransactionRequest, deadline: float | None = None) -> Mapping[str, Any]:
        raise HostError("HOST_OPERATION_UNSUPPORTED", "El host no admite refunds vinculados.")

    @abstractmethod
    def cancel(self, operation_id: str) -> Mapping[str, Any]:
        raise NotImplementedError
