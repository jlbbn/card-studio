from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class VelocityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _non_negative(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VelocityError("INVALID_VELOCITY_POLICY", f"{name} debe ser un entero no negativo.")
    return value


@dataclass(frozen=True)
class VelocityPolicy:
    lower_offline_limit: int = 0
    upper_offline_limit: int = 0
    maximum_cumulative_amount_minor: int = 0
    lower_selection_percent: float = 100.0
    merchant_forced_online: bool = False

    def __post_init__(self) -> None:
        _non_negative(self.lower_offline_limit, "lower_offline_limit")
        _non_negative(self.upper_offline_limit, "upper_offline_limit")
        _non_negative(self.maximum_cumulative_amount_minor, "maximum_cumulative_amount_minor")
        if self.upper_offline_limit and self.upper_offline_limit < self.lower_offline_limit:
            raise VelocityError("INVALID_VELOCITY_POLICY", "upper_offline_limit no puede ser menor que lower_offline_limit.")
        if not isinstance(self.lower_selection_percent, (int, float)) or isinstance(self.lower_selection_percent, bool):
            raise VelocityError("INVALID_VELOCITY_POLICY", "lower_selection_percent debe estar entre 0 y 100.")
        if not 0 <= self.lower_selection_percent <= 100:
            raise VelocityError("INVALID_VELOCITY_POLICY", "lower_selection_percent debe estar entre 0 y 100.")

    @classmethod
    def from_profile(cls, profile: Any) -> "VelocityPolicy | None":
        emv = profile.scheme_parameters.get("emv_execution", {})
        decision = emv.get("decision", {}) if isinstance(emv, Mapping) else {}
        risk = decision.get("risk", {}) if isinstance(decision, Mapping) else {}
        raw = risk.get("velocity") if isinstance(risk, Mapping) else None
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise VelocityError("INVALID_VELOCITY_POLICY", "risk.velocity debe ser Mapping.")
        return cls(
            lower_offline_limit=raw.get("lower_offline_limit", 0),
            upper_offline_limit=raw.get("upper_offline_limit", 0),
            maximum_cumulative_amount_minor=raw.get("maximum_cumulative_amount_minor", 0),
            lower_selection_percent=raw.get("lower_selection_percent", 100.0),
            merchant_forced_online=raw.get("merchant_forced_online", False) is True,
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "lower_offline_limit": self.lower_offline_limit,
            "upper_offline_limit": self.upper_offline_limit,
            "maximum_cumulative_amount_minor": self.maximum_cumulative_amount_minor,
            "lower_selection_percent": self.lower_selection_percent,
            "merchant_forced_online": self.merchant_forced_online,
        }


@dataclass(frozen=True)
class VelocityDecision:
    operation_id: str
    scope: str
    reasons: tuple[str, ...]
    offline_count_after: int
    pending_count_after: int
    cumulative_amount_after: int
    status: str = "RESERVED"
    mode: str = "LAB_ONLY"

    @property
    def online_required(self) -> bool:
        return bool(self.reasons)

    def to_evidence(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "scope": self.scope,
            "reasons": list(self.reasons),
            "offline_count_after": self.offline_count_after,
            "pending_count_after": self.pending_count_after,
            "cumulative_amount_after": self.cumulative_amount_after,
            "status": self.status,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VelocityDecision":
        return cls(
            operation_id=value["operation_id"],
            scope=value["scope"],
            reasons=tuple(value["reasons"]),
            offline_count_after=value["offline_count_after"],
            pending_count_after=value["pending_count_after"],
            cumulative_amount_after=value["cumulative_amount_after"],
            status=value["status"],
        )


class DurableVelocityCounters:
    def __init__(self, path: str | Path, timeout_seconds: float = 5.0):
        self.path = str(path)
        self.timeout_seconds = timeout_seconds
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS velocity_counters (
                    scope TEXT PRIMARY KEY,
                    offline_count INTEGER NOT NULL,
                    pending_count INTEGER NOT NULL,
                    cumulative_amount INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS velocity_operations (
                    operation_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    policy_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    status TEXT NOT NULL
                );
            """)
            connection.commit()
            self._initialized = True

    @staticmethod
    def _identifiers(operation_id: str, scope: str, amount: int) -> None:
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise VelocityError("INVALID_VELOCITY_OPERATION", "operation_id inválido para velocity.")
        if not isinstance(scope, str) or not 1 <= len(scope) <= 256 or any(ord(char) < 0x20 for char in scope):
            raise VelocityError("INVALID_VELOCITY_SCOPE", "scope inválido para velocity.")
        _non_negative(amount, "amount_minor")

    def reserve(
        self,
        operation_id: str,
        scope: str,
        amount_minor: int,
        policy: VelocityPolicy,
        sample_basis_points: int = 0,
    ) -> VelocityDecision:
        self._identifiers(operation_id, scope, amount_minor)
        if not isinstance(sample_basis_points, int) or isinstance(sample_basis_points, bool) or not 0 <= sample_basis_points <= 9999:
            raise VelocityError("INVALID_VELOCITY_SAMPLE", "sample_basis_points debe estar entre 0 y 9999.")
        policy_json = json.dumps(policy.evidence(), sort_keys=True, separators=(",", ":"))
        connection = self._connect()
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT scope, amount, policy_json, decision_json, status FROM velocity_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if existing[:3] != (scope, amount_minor, policy_json):
                    raise VelocityError("VELOCITY_OPERATION_CONFLICT", "La reserva velocity existe con otro contexto.")
                if existing[4] != "ABORTED_BEFORE_SEND":
                    connection.commit()
                    return VelocityDecision.from_dict(json.loads(existing[3]))
            row = connection.execute(
                "SELECT offline_count, pending_count, cumulative_amount FROM velocity_counters WHERE scope=?",
                (scope,),
            ).fetchone()
            offline, pending, cumulative = row or (0, 0, 0)
            next_offline = offline + pending + 1
            next_cumulative = cumulative + amount_minor
            reasons: list[str] = []
            if policy.merchant_forced_online:
                reasons.append("MERCHANT_FORCED_ONLINE")
            if policy.lower_offline_limit and next_offline > policy.lower_offline_limit:
                threshold = round(policy.lower_selection_percent * 100)
                if sample_basis_points < threshold:
                    reasons.append("LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED")
            if policy.upper_offline_limit and next_offline > policy.upper_offline_limit:
                reasons.append("UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED")
            if policy.maximum_cumulative_amount_minor and next_cumulative > policy.maximum_cumulative_amount_minor:
                reasons.append("VELOCITY_AMOUNT_LIMIT_EXCEEDED")
            decision = VelocityDecision(
                operation_id,
                scope,
                tuple(dict.fromkeys(reasons)),
                next_offline,
                pending + 1,
                next_cumulative,
            )
            connection.execute(
                "INSERT INTO velocity_counters(scope, offline_count, pending_count, cumulative_amount) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET pending_count=excluded.pending_count, cumulative_amount=excluded.cumulative_amount",
                (scope, offline, pending + 1, next_cumulative),
            )
            connection.execute(
                "INSERT INTO velocity_operations(operation_id, scope, amount, policy_json, decision_json, status) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(operation_id) DO UPDATE SET decision_json=excluded.decision_json, status='RESERVED'",
                (operation_id, scope, amount_minor, policy_json, json.dumps(decision.to_evidence(), sort_keys=True), "RESERVED"),
            )
            connection.commit()
            return decision
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, operation_id: str, outcome: str) -> VelocityDecision:
        transitions = {
            "OFFLINE_APPROVED": "OFFLINE_APPROVED",
            "ONLINE_COMPLETED": "ONLINE_COMPLETED",
            "NOT_OFFLINE": "NOT_OFFLINE",
            "ABORTED_BEFORE_SEND": "ABORTED_BEFORE_SEND",
        }
        if outcome not in transitions:
            raise VelocityError("INVALID_VELOCITY_OUTCOME", "Resultado velocity no reconocido.")
        connection = self._connect()
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT scope, amount, decision_json, status FROM velocity_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise VelocityError("VELOCITY_RESERVATION_NOT_FOUND", "No existe la reserva velocity.")
            scope, amount, decision_json, status = row
            decision = VelocityDecision.from_dict(json.loads(decision_json))
            final = transitions[outcome]
            if status != "RESERVED":
                if status != final:
                    raise VelocityError("VELOCITY_OUTCOME_CONFLICT", "La reserva velocity ya tiene otro resultado final.")
                connection.commit()
                return VelocityDecision.from_dict({**decision.to_evidence(), "status": status})
            if outcome == "OFFLINE_APPROVED" and decision.online_required:
                raise VelocityError("VELOCITY_OFFLINE_OUTCOME_BLOCKED", "Una decisión velocity online no puede finalizar offline.")
            counter = connection.execute(
                "SELECT offline_count, pending_count, cumulative_amount FROM velocity_counters WHERE scope=?",
                (scope,),
            ).fetchone()
            if counter is None or counter[1] < 1:
                raise VelocityError("VELOCITY_COUNTER_CORRUPT", "El contador pendiente no es consistente.")
            offline, pending, cumulative = counter
            pending -= 1
            if outcome == "OFFLINE_APPROVED":
                offline += 1
            elif outcome == "ONLINE_COMPLETED":
                offline = 0
            elif outcome == "ABORTED_BEFORE_SEND":
                cumulative -= amount
            connection.execute(
                "UPDATE velocity_counters SET offline_count=?, pending_count=?, cumulative_amount=? WHERE scope=?",
                (offline, pending, cumulative, scope),
            )
            connection.execute(
                "UPDATE velocity_operations SET status=? WHERE operation_id=?",
                (final, operation_id),
            )
            connection.commit()
            return VelocityDecision.from_dict({**decision.to_evidence(), "status": final})
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def snapshot(self, scope: str) -> dict[str, int]:
        connection = self._connect()
        try:
            self._initialize(connection)
            row = connection.execute(
                "SELECT offline_count, pending_count, cumulative_amount FROM velocity_counters WHERE scope=?",
                (scope,),
            ).fetchone()
            offline, pending, cumulative = row or (0, 0, 0)
            return {"offline_count": offline, "pending_count": pending, "cumulative_amount": cumulative}
        finally:
            connection.close()
