from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from card_studio.emv.trace import SanitizedTrace


class SessionControlError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: str = "CERTAIN_FAILURE",
        cancelled: bool = False,
        command_sent: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome
        self.cancelled = cancelled
        self.command_sent = command_sent


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass
class SessionExecutionControl:
    trace: SanitizedTrace
    cancellation: CancellationToken
    stage_timeouts_ms: Mapping[str, float] = field(default_factory=dict)
    deadline: float | None = None
    clock: Callable[[], float] = time.monotonic
    on_sent: Callable[[], None] | None = None
    stage: str = "SESSION_OPEN"
    _stage_deadline: float | None = None
    _sent_count: int = 0

    def __post_init__(self) -> None:
        selected: dict[str, float] = {}
        for stage, value in self.stage_timeouts_ms.items():
            if not isinstance(stage, str) or not stage or len(stage) > 64:
                raise ValueError("Stage de timeout inválido.")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= 600000:
                raise ValueError("Cada timeout de etapa debe estar entre 0 y 600000 ms.")
            selected[stage] = float(value)
        self.stage_timeouts_ms = selected
        self._stage_deadline = self._new_stage_deadline(self.stage)

    @classmethod
    def from_profile(
        cls,
        profile: Any,
        operation_id: str,
        cancellation: CancellationToken,
        deadline: float | None,
        on_sent: Callable[[], None],
    ) -> "SessionExecutionControl":
        raw = profile.scheme_parameters.get("execution_control", {})
        if not isinstance(raw, Mapping):
            raise ValueError("execution_control debe ser Mapping.")
        timeouts = raw.get("stage_timeouts_ms", {})
        if not isinstance(timeouts, Mapping):
            raise ValueError("stage_timeouts_ms debe ser Mapping.")
        return cls(
            SanitizedTrace(operation_id),
            cancellation,
            timeouts,
            deadline,
            on_sent=on_sent,
        )

    def _new_stage_deadline(self, stage: str) -> float | None:
        timeout = self.stage_timeouts_ms.get(stage)
        candidates = [self.deadline] if self.deadline is not None else []
        if timeout is not None:
            candidates.append(self.clock() + timeout / 1000)
        return min(candidates) if candidates else None

    def enter_stage(self, stage: str) -> None:
        self.stage = stage
        self._stage_deadline = self._new_stage_deadline(stage)
        self.trace.stage(stage)
        self.check_point()

    def _check_timeout(self) -> None:
        if self._stage_deadline is not None and self._stage_deadline <= self.clock():
            sent = self._sent_count > 0
            raise SessionControlError(
                "TIMEOUT_AFTER_SEND" if sent else "TIMEOUT_BEFORE_SEND",
                "El deadline de ejecución expiró.",
                outcome="UNCERTAIN" if sent else "CERTAIN_FAILURE",
                command_sent=sent,
            )

    def check_point(self) -> None:
        if self.cancellation.is_set():
            sent = self._sent_count > 0
            raise SessionControlError(
                "CANCELLED_AFTER_SEND" if sent else "OPERATOR_CANCELLED",
                "Cancelación cooperativa observada.",
                outcome="UNCERTAIN" if sent else "CERTAIN_FAILURE",
                cancelled=True,
                command_sent=sent,
            )
        self._check_timeout()

    def before_exchange(self) -> None:
        self.check_point()

    def sent(self) -> None:
        self._sent_count += 1
        if self.on_sent is not None:
            self.on_sent()

    def after_exchange(self) -> None:
        self.check_point()

    @property
    def command_sent(self) -> bool:
        return self._sent_count > 0


class ControlledCardInterface:
    def __init__(self, delegate: Any, control: SessionExecutionControl):
        self._delegate = delegate
        self._control = control

    @property
    def kind(self) -> Any:
        return self._delegate.kind

    @property
    def simulated(self) -> bool:
        return self._delegate.simulated

    @property
    def connection(self) -> Any:
        return self._delegate.connection

    @property
    def state(self) -> Any:
        return self._delegate.state

    @property
    def adapter(self) -> Any:
        return self._delegate.adapter

    @property
    def session(self) -> Any:
        return self._delegate.session

    def interface_kind(self) -> Any:
        return self._delegate.interface_kind()

    def open(self) -> Any:
        self._delegate.open()
        return self

    def close(self) -> None:
        self._delegate.close()

    def cancel(self) -> None:
        self._control.cancellation.cancel()
        self._delegate.cancel()

    def exchange(self, command: bytes | str, sensitivity: str = "REDACTED") -> Any:
        stage = self._control.stage
        self._control.before_exchange()
        self._control.trace.sent(stage, command)
        self._control.sent()
        try:
            response = self._delegate.exchange(command, sensitivity=sensitivity)
        except Exception as exc:
            self._control.trace.error(stage, command, getattr(exc, "code", "CARD_EXCHANGE_FAILED"))
            raise
        self._control.trace.response(stage, command, response)
        self._control.after_exchange()
        return response
