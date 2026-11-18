from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from card_studio.transactions.model import TransactionRequest

from .control import CancellationToken
from .state import KernelStateMachine

if TYPE_CHECKING:
    from card_studio.interfaces.base import CardInterface
    from card_studio.profiles.model import CardProfile


@dataclass
class InternalSession:
    session_id: str
    request: TransactionRequest
    profile: "CardProfile"
    card_interface: "CardInterface"
    machine: KernelStateMachine
    selected_aid: str | None = None
    cvm_result: dict[str, Any] | None = None
    risk_result: dict[str, Any] | None = None
    action_result: dict[str, Any] | None = None
    command_sent: bool = False
    closed: bool = False
    cancelled: bool = False
    commands: list[str] = field(default_factory=list)
    online_flow: Any = None
    emv_flow: Any = None
    data_store: Any = None
    online_fingerprint: str | None = None
    retry_execution: bool = False
    un_evidence: Any = None
    configuration_provenance: Any = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    control: Any = None
    velocity_policy: Any = None
    velocity_reservation: Any = None
    transaction_semantics: Any = None
