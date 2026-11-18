from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

from card_studio.readers.base import CloseEvidence
from card_studio.transactions.model import TransactionRequest, TransactionResult
from card_studio.transactions.recovery import RecoveryClass

if TYPE_CHECKING:
    from card_studio.interfaces.base import CardInterface
    from card_studio.profiles.model import CardProfile


class KernelError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        recovery: RecoveryClass = RecoveryClass.NON_RETRYABLE,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery


@dataclass(frozen=True)
class KernelCapabilities:
    interfaces: tuple[str, ...]
    transaction_types: tuple[str, ...]
    simulation: bool = True
    online_processing: bool = False


@dataclass(frozen=True)
class KernelSession:
    session_id: str
    operation_id: str
    profile_ref: str
    interface: str
    kernel_ref: str
    simulation: bool = True


class KernelAdapter(ABC):
    kernel_ref = "abstract"
    kernel_version = "1"
    kernel_mode = "SIMULATED"

    @abstractmethod
    def capabilities(self) -> KernelCapabilities:
        raise NotImplementedError

    @abstractmethod
    def open_session(
        self,
        request: TransactionRequest,
        card_interface: CardInterface | None = None,
        profile: CardProfile | None = None,
    ) -> KernelSession:
        raise NotImplementedError

    @abstractmethod
    def execute(
        self,
        session: KernelSession,
        request: TransactionRequest,
        deadline: float | None = None,
    ) -> TransactionResult:
        raise NotImplementedError

    def complete_online(
        self,
        session: KernelSession,
        request: TransactionRequest,
        host_response: Mapping[str, Any],
    ) -> TransactionResult:
        raise KernelError(
            "ONLINE_COMPLETION_UNSUPPORTED",
            "El kernel no admite completado online.",
            RecoveryClass.NON_RETRYABLE,
        )

    @abstractmethod
    def query(self, operation_id: str) -> TransactionResult:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, session: KernelSession) -> TransactionResult:
        raise NotImplementedError

    @abstractmethod
    def close(self, session: KernelSession) -> CloseEvidence:
        raise NotImplementedError
