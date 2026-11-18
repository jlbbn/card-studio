from __future__ import annotations

import importlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from card_studio.host.base import AuthorizationHostAdapter
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.base import KernelAdapter
from card_studio.profiles.model import CardProfile
from card_studio.profiles.registry import CardProfileRegistry
from card_studio.readers.base import ReaderAdapter
from card_studio.readers.mock import MockReaderAdapter
from card_studio.system import config_directory, data_directory
from card_studio.transactions.evidence import HOST_METADATA_FIELDS, TransactionEvidenceStore
from card_studio.transactions.model import TransactionRequest
from card_studio.transactions.service import InterfaceFactory, TransactionService, default_interface_factory
from card_studio.transactions.store import TransactionRecord, TransactionStore


def laboratory_profile(kernel_ref: str, profile_id: str = "lab-terminal") -> CardProfile:
    return CardProfile(
        schema_version=1,
        profile_id=profile_id,
        profile_version=1,
        profile_digest="",
        scheme_family="laboratory",
        display_name="TEST/SYNTHETIC/LAB_ONLY",
        applications=(
            {"aid": "A0000000031010", "application_label": "LAB APP 1", "priority": 1},
            {"aid": "A0000000041010", "application_label": "LAB APP 2", "priority": 2},
        ),
        interfaces=("CONTACT", "CONTACTLESS", "VIRTUAL"),
        cvm_capabilities=("NO_CVM", "SIGNATURE_SIMULATED"),
        kernel_requirements={
            "simulation": True,
            "kernel_ref": kernel_ref,
            "environment": "TEST/SYNTHETIC/LAB_ONLY",
        },
        scheme_parameters={
            "synthetic": True,
            "cvm": {"method": "NO_CVM"},
            "risk": {"floor_limit_minor": 0, "random_selection_percent": 0},
        },
        test_credential_ref="test://lab-only",
        synthetic_pan_profile={"synthetic": True},
        tokenization_policy={"enabled": False},
        status="APPROVED_FOR_SIMULATION",
    )


def _laboratory_reader(profile: CardProfile) -> MockReaderAdapter:
    from card_studio.emv.protocol import laboratory_responses

    responses = {}
    for application in profile.applications:
        responses.update(laboratory_responses(profile, application_aid=application.aid))
    return MockReaderAdapter(responses=responses)


def _default_kernel() -> KernelAdapter:
    try:
        module = importlib.import_module("card_studio.kernels.internal_emv")
        kernel_class = getattr(module, "InternalEmvKernel")
        return kernel_class()
    except (ImportError, AttributeError):
        from card_studio.l3.scenarios import LabKernelAdapter

        return LabKernelAdapter()


class CardStudioApplication:
    def __init__(
        self,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        profiles: CardProfileRegistry | Iterable[CardProfile] | None = None,
        reader: ReaderAdapter | None = None,
        kernels: KernelAdapter | Iterable[KernelAdapter] | None = None,
        host: AuthorizationHostAdapter | None = None,
        store: TransactionStore | None = None,
        evidence: TransactionEvidenceStore | None = None,
        reader_id: str | None = None,
        interface_factory: InterfaceFactory = default_interface_factory,
        hooks: Mapping[str, Any] | None = None,
    ):
        self.data_dir = data_dir or data_directory()
        self.config_dir = config_dir or config_directory()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kernels is None:
            adapters = [_default_kernel()]
        elif isinstance(kernels, KernelAdapter):
            adapters = [kernels]
        else:
            adapters = list(kernels)
        if not adapters:
            raise ValueError("Se requiere al menos un kernel simulado.")
        if profiles is None:
            registry = CardProfileRegistry([laboratory_profile(adapters[0].kernel_ref)])
        elif isinstance(profiles, CardProfileRegistry):
            registry = profiles
        else:
            registry = CardProfileRegistry(profiles)
        self.profiles = registry
        self.reader = reader or _laboratory_reader(registry.list()[0])
        self.store = store or TransactionStore(self.data_dir / "transactions-v1.sqlite3")
        self.host = host or MockAuthorizationHost(
            self.data_dir / "host-v1.sqlite3",
            evidence_dir=self.data_dir / "evidence" / "host",
        )
        self.evidence = evidence or TransactionEvidenceStore(self.data_dir / "evidence" / "transactions")
        self.service = TransactionService(
            store=self.store,
            profiles=registry,
            reader=self.reader,
            kernels=adapters,
            host=self.host,
            evidence=self.evidence,
            reader_id=reader_id,
            interface_factory=interface_factory,
            hooks=hooks,
        )

    @classmethod
    def create(
        cls,
        data_dir: Path | None = None,
        config_dir: Path | None = None,
        profiles: CardProfileRegistry | Iterable[CardProfile] | None = None,
        **dependencies: Any,
    ) -> "CardStudioApplication":
        return cls(data_dir=data_dir, config_dir=config_dir, profiles=profiles, **dependencies)

    @property
    def default_profile(self) -> CardProfile:
        return self.profiles.list()[0]

    @property
    def default_profile_ref(self) -> str:
        profile = self.default_profile
        return f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"

    def _public(self, record: TransactionRecord) -> dict[str, Any]:
        value = record.to_dict()
        value["state_transitions"] = [
            {
                "previous_state": item.previous_state,
                "new_state": item.new_state,
                "reason": item.reason,
                "timestamp": item.timestamp,
                "operation_id": item.operation_id,
            }
            for item in self.store.audit(record.operation_id)
        ]
        if record.result is not None:
            value["error_code"] = record.result.error_code
            value["details"] = {
                key: record.result.details[key]
                for key in ("selected_aid", "cvm_result", "risk_result", "action_result", *HOST_METADATA_FIELDS)
                if key in record.result.details
            }
        value["mode"] = "LAB / SYNTHETIC"
        value["physical_validation"] = "NOT_EXECUTED"
        value["simulation"] = True
        return value

    @staticmethod
    def _request(request: TransactionRequest | Mapping[str, Any]) -> TransactionRequest:
        return request if isinstance(request, TransactionRequest) else TransactionRequest.from_dict(request)

    def create_transaction(
        self,
        request: TransactionRequest | Mapping[str, Any],
        deadline: float | None = None,
    ) -> dict[str, Any]:
        return self._public(self.service.execute(self._request(request), deadline))

    execute_transaction = create_transaction

    def retry_transaction(
        self,
        request: TransactionRequest | Mapping[str, Any],
        deadline: float | None = None,
    ) -> dict[str, Any]:
        return self._public(self.service.retry(self._request(request), deadline))

    def retry(self, operation_id: str) -> dict[str, Any]:
        return self._public(self.service.retry(self.service.get(operation_id).request))

    def get(self, operation_id: str) -> dict[str, Any]:
        return self._public(self.service.get(operation_id))

    get_transaction = get

    def query(self, operation_id: str) -> dict[str, Any]:
        return self._public(self.service.query(operation_id))

    query_transaction = query

    def cancel(self, operation_id: str) -> dict[str, Any]:
        return self._public(self.service.cancel(operation_id))

    cancel_transaction = cancel

    def recover(self, operation_id: str) -> dict[str, Any]:
        return self._public(self.service.recover(operation_id))

    def list_pending(self) -> list[dict[str, Any]]:
        return [self._public(item) for item in self.service.list_pending()]

    def list_transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self._public(item) for item in self.service.list_transactions(limit)]

    def kernel_readiness(self) -> dict[str, Any]:
        return self.service.readiness()

    def l3_readiness(self) -> dict[str, Any]:
        nested = self.data_dir / "l3" / "readiness.json"
        direct = self.data_dir / "readiness.json"
        path = nested if nested.exists() else direct
        if not path.exists():
            return {
                "status": "NOT_RUN",
                "passed": 0,
                "total": 0,
                "gates": [
                    {"gate_id": f"G{number}", "status": "NOT_RUN"}
                    for number in range(1, 11)
                ],
                "certification": "NOT COMPLETED",
                "physical_validation": "PENDING",
                "simulation": True,
                "mode": "LAB / SYNTHETIC",
            }
        import hashlib
        import stat

        original = path.read_bytes()
        value = json.loads(original)
        from card_studio.l3.model import IDENTITY_FIELDS, P0_GATE_IDS, identity_is_conformant

        gates = value.get("gates", [])
        identity = value.get("identity", {})
        counts_complete = (
            isinstance(value.get("total"), int)
            and value["total"] > 0
            and value.get("passed") == value["total"]
            and len(gates) == 10
            and {gate.get("gate_id") for gate in gates} == {f"G{number}" for number in range(1, 11)}
        )
        identities_complete = bool(identity) and all(gate.get("identity") for gate in gates)
        mixed = identity.get("mixed") is True or any(
            tuple(gate.get("identity", {}).get(field) for field in IDENTITY_FIELDS)
            != tuple(identity.get(field) for field in IDENTITY_FIELDS)
            for gate in gates
        )
        forged = (
            value.get("conformant") is True and not identity_is_conformant(identity)
        ) or any(
            gate.get("conformant") is True and not identity_is_conformant(gate.get("identity", {}))
            for gate in gates
        )
        eligible = (
            counts_complete
            and identities_complete
            and not mixed
            and not forged
            and value.get("conformant") is True
            and identity_is_conformant(identity)
            and all(
                gate.get("status") == "PASS"
                and gate.get("conformant") is True
                and identity_is_conformant(gate.get("identity", {}))
                for gate in gates
            )
        )
        p0_gates = value.get("p0_gates", {})
        p0_complete = (
            value.get("p0_complete") is True
            and set(p0_gates) == P0_GATE_IDS
            and all(p0_gates[gate_id] == "PASS" for gate_id in P0_GATE_IDS)
        )
        manifest_valid = True
        if eligible and p0_complete:
            try:
                directory = path.parent
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                matrix_bytes = (directory / "matrix.json").read_bytes()
                matrix = json.loads(matrix_bytes)
                if (
                    manifest.get("identity") != identity
                    or manifest.get("conformant") is not True
                    or manifest.get("p0_complete") is not True
                    or manifest.get("p0_gates") != p0_gates
                    or manifest.get("matrix_sha256") != hashlib.sha256(matrix_bytes).hexdigest()
                    or manifest.get("readiness_sha256") != hashlib.sha256(original).hexdigest()
                ):
                    raise ValueError("L3 manifest identity or digest mismatch.")
                matrix_cases = {case["test_case_id"]: case for case in matrix.get("cases", [])}
                manifest_cases = manifest.get("cases", [])
                if (
                    len(manifest_cases) != len(matrix_cases)
                    or {case["test_case_id"] for case in manifest_cases} != set(matrix_cases)
                ):
                    raise ValueError("L3 manifest case mismatch.")
                for case in manifest_cases:
                    matrix_case = matrix_cases[case["test_case_id"]]
                    evidence_path = directory / "cases" / case["evidence_reference"]
                    if (
                        case.get("profile_ref") != matrix_case.get("profile_ref")
                        or case.get("profile_digest") != matrix_case.get("profile_digest")
                        or case.get("evidence_sha256") != hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                        or stat.S_IMODE(evidence_path.stat().st_mode) != 0o600
                    ):
                        raise ValueError("L3 case evidence mismatch.")
                    expected_identity = {
                        field: matrix_case.get(field) for field in IDENTITY_FIELDS
                    }
                    expected_identity.update({
                        "profile_digest": matrix_case["profile_digest"],
                        "profile_digests": [matrix_case["profile_digest"]],
                        "mixed": False,
                    })
                    if case.get("identity") != expected_identity:
                        raise ValueError("L3 case identity mismatch.")
            except (OSError, ValueError, KeyError, TypeError):
                manifest_valid = False
        eligible = eligible and p0_complete and manifest_valid
        if eligible:
            status = "PASS"
        elif not manifest_valid or not counts_complete or not identities_complete or mixed or forged or any(
            gate.get("status") == "FAIL" for gate in gates
        ):
            status = "FAIL"
        else:
            status = "NON_CONFORMANT"
        value["status"] = status
        value["readiness_eligible"] = eligible
        value["certification"] = "NOT COMPLETED"
        value["physical_validation"] = "PENDING"
        return value

    def p1_l3_readiness(self) -> dict[str, Any]:
        nested = self.data_dir / "l3" / "p1" / "readiness.json"
        direct = self.data_dir / "readiness.json"
        path = nested if nested.exists() else direct
        if not path.exists():
            return {
                "status": "NOT_RUN",
                "readiness_eligible": False,
                "mode": "SIMULATED/LAB_ONLY",
                "authority": "EXTERNAL_UNVERIFIED",
                "certification": "NOT COMPLETED",
                "physical_validation": "NOT_EXECUTED",
            }
        import hashlib

        original = path.read_bytes()
        value: dict[str, Any] = {}
        try:
            value = json.loads(original)
            manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
            matrix_bytes = (path.parent / "matrix.json").read_bytes()
            matrix = json.loads(matrix_bytes)
            identity = value["identity"]
            internal = (
                identity.get("kernel_adapter") == "InternalEmvKernel"
                and identity.get("kernel_ref") == "internal-emv-lab-v1"
                and identity.get("mixed") is False
            )
            if (
                manifest.get("identity") != identity
                or manifest.get("status") != value.get("status")
                or manifest.get("scenario_set_sha256") != value.get("scenario_set_sha256")
                or manifest.get("matrix_sha256") != hashlib.sha256(matrix_bytes).hexdigest()
                or manifest.get("readiness_sha256") != hashlib.sha256(original).hexdigest()
                or value.get("passed") != value.get("total")
                or value.get("total", 0) <= 0
                or value.get("status") != "PASS"
                or not internal
            ):
                raise ValueError("P1 readiness is incomplete or non-conformant.")
            matrix_cases = {case["scenario_id"]: case for case in matrix["cases"]}
            for case in manifest["cases"]:
                evidence = path.parent / "cases" / case["evidence_reference"]
                if (
                    case.get("scenario_sha256") != matrix_cases[case["scenario_id"]]["scenario_sha256"]
                    or case.get("evidence_sha256") != hashlib.sha256(evidence.read_bytes()).hexdigest()
                ):
                    raise ValueError("P1 case evidence mismatch.")
            value["readiness_eligible"] = True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            if not isinstance(value, dict):
                value = {}
            value["status"] = "FAIL" if value.get("status") == "PASS" else value.get("status", "FAIL")
            value["readiness_eligible"] = False
        value["certification"] = "NOT COMPLETED"
        value["physical_validation"] = "NOT_EXECUTED"
        value["authority"] = "EXTERNAL_UNVERIFIED"
        return value

    def audit(self, operation_id: str) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.store.audit(operation_id)]

    def host_phases(self, operation_id: str) -> list[dict[str, Any]]:
        phase_names = {
            "ONLINE_REQUESTED": "HOST_REQUEST_PREPARED",
            "HOST_AUTHORIZATION_STARTED": "AUTHORIZATION_SENT",
            "HOST_AUTHORIZATION_COMPLETED": "AUTHORIZATION_RECEIVED",
            "HOST_AUTHORIZATION_UNCERTAIN": "AUTHORIZATION_UNCERTAIN",
            "ISSUER_PROCESSING": "ISSUER_PROCESSING",
            "ONLINE_COMPLETED": "ONLINE_COMPLETED",
            "REFUND_STARTED": "REFUND_SENT",
            "REFUND_COMPLETED": "REFUND_RECEIVED",
            "REVERSAL_STARTED": "REVERSAL_SENT",
            "REVERSAL_COMPLETED": "REVERSAL_RECEIVED",
            "HOST_QUERY_COMPLETED": "HOST_QUERY_COMPLETED",
        }
        return [
            {
                "phase": phase_names[item["event_type"]],
                "event_type": item["event_type"],
                "sequence": item["sequence"],
                "timestamp": item["timestamp"],
                "mode": "SIMULATED",
                "authority": "EXTERNAL_UNVERIFIED",
            }
            for item in self.audit(operation_id)
            if item["event_type"] in phase_names
        ]
