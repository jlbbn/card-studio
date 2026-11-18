from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONFORMANT_KERNELS = frozenset({("InternalEmvKernel", "internal-emv-lab-v1")})
IDENTITY_FIELDS = (
    "kernel_adapter",
    "kernel_ref",
    "kernel_version",
    "kernel_mode",
    "host_adapter",
    "host_version",
    "terminal_version",
)


@dataclass(frozen=True)
class AssertionResult:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def identity_is_conformant(identity: dict[str, Any]) -> bool:
    if identity.get("mixed") is True:
        return False
    if (identity.get("kernel_adapter"), identity.get("kernel_ref")) not in CONFORMANT_KERNELS:
        return False
    return all([
        identity.get("kernel_mode") == "LAB / SYNTHETIC",
        bool(identity.get("kernel_version")),
        identity.get("host_adapter") == "MockAuthorizationHost",
        bool(identity.get("host_version")),
        bool(identity.get("terminal_version")),
        bool(identity.get("profile_digests") or identity.get("profile_digest")),
    ])


def build_run_identity(cases: list["L3CaseResult"]) -> dict[str, Any]:
    if not cases:
        return {"mixed": True, "profile_digests": []}
    identities = [case.identity() for case in cases]
    signatures = {tuple(identity.get(field) for field in IDENTITY_FIELDS) for identity in identities}
    digests = sorted({case.profile_digest for case in cases if case.profile_digest})
    identity = dict(identities[0])
    identity["profile_digests"] = digests
    identity["profile_digest"] = digests[0] if len(digests) == 1 else "MULTIPLE"
    identity["mixed"] = len(signatures) != 1
    if identity["mixed"]:
        for field in IDENTITY_FIELDS:
            identity[field] = "MIXED"
    return identity


@dataclass
class L3CaseResult:
    test_case_id: str
    operation_id: str
    profile_ref: str
    kernel_ref: str
    kernel_version: str
    terminal_version: str
    interface: str
    host: str
    kernel_adapter: str = "UNKNOWN"
    kernel_mode: str = "UNKNOWN"
    profile_digest: str = ""
    host_adapter: str = "UNKNOWN"
    host_version: str = "UNKNOWN"
    conformant: bool = False
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    functional_result: str | None = None
    execution_state: str | None = None
    recovery_action: str | None = None
    started_at: str = ""
    completed_at: str = ""
    assertions: list[AssertionResult] = field(default_factory=list)
    status: str = "FAIL"
    evidence_reference: str | None = None

    def identity(self) -> dict[str, Any]:
        return {
            "kernel_adapter": self.kernel_adapter,
            "kernel_ref": self.kernel_ref,
            "kernel_version": self.kernel_version,
            "kernel_mode": self.kernel_mode,
            "profile_digest": self.profile_digest,
            "profile_digests": [self.profile_digest] if self.profile_digest else [],
            "host_adapter": self.host_adapter,
            "host_version": self.host_version,
            "terminal_version": self.terminal_version,
            "mixed": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_case_id": self.test_case_id,
            "operation_id": self.operation_id,
            "profile_ref": self.profile_ref,
            "profile_digest": self.profile_digest,
            "kernel_adapter": self.kernel_adapter,
            "kernel_ref": self.kernel_ref,
            "kernel_version": self.kernel_version,
            "kernel_mode": self.kernel_mode,
            "terminal_version": self.terminal_version,
            "interface": self.interface,
            "host": self.host,
            "host_adapter": self.host_adapter,
            "host_version": self.host_version,
            "conformant": self.conformant,
            "readiness_eligible": self.conformant,
            "state_transitions": self.state_transitions,
            "functional_result": self.functional_result,
            "execution_state": self.execution_state,
            "recovery_action": self.recovery_action,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "assertions": [item.to_dict() for item in self.assertions],
            "status": self.status,
            "evidence_reference": self.evidence_reference,
            "simulation": True,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
        }


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    status: str
    case_ids: tuple[str, ...]
    identity: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def conformant(self) -> bool:
        return self.status == "PASS" and identity_is_conformant(self.identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status,
            "case_ids": list(self.case_ids),
            "identity": self.identity,
            "conformant": self.conformant,
            "readiness_eligible": self.conformant,
            "reason": self.reason,
        }


P0_GATE_IDS = frozenset({
    *(f"K{number:02d}" for number in range(1, 26)),
    *(f"T{number:02d}" for number in range(1, 11)),
})


@dataclass
class L3Report:
    cases: list[L3CaseResult]
    gates: list[GateResult]
    started_at: str
    completed_at: str
    identity: dict[str, Any] = field(default_factory=dict)
    conformant: bool = False
    p0_gates: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(item.status == "PASS" for item in self.cases)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def p0_complete(self) -> bool:
        return set(self.p0_gates) == P0_GATE_IDS and all(
            self.p0_gates[gate_id] == "PASS" for gate_id in P0_GATE_IDS
        )

    @property
    def status(self) -> str:
        if not self.cases or self.passed != self.total:
            return "FAIL"
        current = build_run_identity(self.cases)
        expected_gates = {f"G{number}" for number in range(1, 11)}
        if {gate.gate_id for gate in self.gates} != expected_gates or len(self.gates) != 10:
            return "FAIL"
        if current.get("mixed") or any(gate.status == "FAIL" for gate in self.gates):
            return "FAIL"
        if self.identity and current != self.identity:
            return "FAIL"
        eligible = (
            self.conformant
            and self.p0_complete
            and identity_is_conformant(current)
            and all(gate.status == "PASS" and identity_is_conformant(gate.identity) for gate in self.gates)
        )
        return "PASS" if eligible else "NON_CONFORMANT"

    def to_dict(self) -> dict[str, Any]:
        status = self.status
        return {
            "status": status,
            "readiness_eligible": status == "PASS",
            "conformant": self.conformant,
            "identity": self.identity,
            "p0_gates": dict(self.p0_gates),
            "p0_complete": self.p0_complete,
            "manifest_reference": "manifest.json",
            "passed": self.passed,
            "total": self.total,
            "cases": [item.to_dict() for item in self.cases],
            "gates": [item.to_dict() for item in self.gates],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "simulation": True,
            "mode": "LAB / SYNTHETIC",
            "certification": "NOT COMPLETED",
            "physical_validation": "PENDING",
        }
