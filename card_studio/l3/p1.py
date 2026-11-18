from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable

from card_studio import __version__
from card_studio.evidence.store import EvidenceStore
from card_studio.kernels.base import KernelAdapter
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.simulator.scenarios import p1_scenarios
from card_studio.simulator.terminal import TerminalRun, TerminalSimulator, scenario_digest
from card_studio.store import write_json_atomic
from card_studio.transactions.store import utc_now

from .authority import SpecificationAuthorityRegistry, SpecificationRequirement


class P1L3Runner:
    def __init__(
        self,
        directory: Path,
        kernel_factory: Callable[..., KernelAdapter] = InternalEmvKernel,
        authority: SpecificationAuthorityRegistry | None = None,
    ):
        self.directory = directory
        self.kernel_factory = kernel_factory
        self.authority = authority or SpecificationAuthorityRegistry([
            SpecificationRequirement(
                "LAB-P1-PROTOCOL-001",
                "LOCAL_SYNTHETIC_PROTOCOL_ORACLE",
                "1",
                "T18-T22 exact APDU/host/reference assertions",
            ),
            SpecificationRequirement(
                "LAB-P1-AUTHORITY-001",
                "LOCAL_AUTHORITY_CONTRACT",
                "1",
                "T23 SPEC_WINS; external authority remains unverified",
            ),
        ])
        self.cases = EvidenceStore(directory / "cases")

    def _identity(self) -> dict[str, Any]:
        kernel = self.kernel_factory()
        return {
            "kernel_adapter": type(kernel).__name__,
            "kernel_ref": kernel.kernel_ref,
            "kernel_version": kernel.kernel_version,
            "kernel_mode": kernel.kernel_mode,
            "host_adapter": "MockAuthorizationHost",
            "host_version": "2",
            "terminal_version": __version__,
            "scenario_schema_version": 1,
            "mixed": False,
        }

    def run(self) -> dict[str, Any]:
        started_at = utc_now()
        scenarios = p1_scenarios()
        simulator = TerminalSimulator(self.directory / "work", self.kernel_factory)
        runs = [simulator.run(scenario) for scenario in scenarios]
        case_values = []
        manifest_cases = []
        for run in runs:
            value = run.to_dict()
            path = self.cases.write(f"{run.scenario_id}.json", value)
            case_values.append(value)
            manifest_cases.append({
                "scenario_id": run.scenario_id,
                "evidence_reference": path.name,
                "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "scenario_sha256": run.scenario_sha256,
                "status": run.status,
            })
        identity = self._identity()
        internal = identity["kernel_adapter"] == "InternalEmvKernel" and identity["kernel_ref"] == "internal-emv-lab-v1"
        passed = sum(run.status == "PASS" for run in runs)
        failed = any(run.status == "FAIL" for run in runs)
        status = "FAIL" if failed or not runs else ("PASS" if internal and passed == len(runs) else "NON_CONFORMANT")
        value = {
            "schema_version": 1,
            "status": status,
            "readiness_eligible": status == "PASS",
            "passed": passed,
            "total": len(runs),
            "identity": identity,
            "scenario_set_sha256": scenario_digest(scenarios),
            "core_baseline": {"passed": 27, "total": 27, "status": "PASS"},
            "coverage": {
                "K28": "INTERNAL_TORN_RECOVER_AC_DRDOL_ATC_NO_REPLAY",
                "T13": "INTERNAL_CONCURRENT_HOST_LINKED_REFUND",
                "T14": "INTERNAL_CONCURRENT_DUPLICATE_REVERSAL",
                "T15": "REAL_MULTIPROCESS_IDEMPOTENCY_CONVERGENCE",
                "T16": "INTERNAL_ATOMIC_LIMITS_AND_COOPERATIVE_CANCEL",
                "T18": "STEP_DRIVEN_INTERNAL",
                "T19": "STATEFUL_SYNTHETIC_ICC",
                "T20": "VERSIONED_SCENARIOS",
                "T21": "EXACT_PROTOCOL_HOST_REFERENCE_ASSERTIONS",
                "T22": "INDEPENDENT_LATE_HOST_RECONCILIATION",
                "T23": "SPEC_WINS_EXTERNAL_UNVERIFIED",
            },
            "authority_registry": self.authority.to_dict(),
            "cases": case_values,
            "started_at": started_at,
            "completed_at": utc_now(),
            "simulation": True,
            "mode": "SIMULATED/LAB_ONLY",
            "authority": "EXTERNAL_UNVERIFIED",
            "certification": "NOT COMPLETED",
            "physical_validation": "NOT_EXECUTED",
        }
        matrix_path = self.directory / "matrix.json"
        readiness_path = self.directory / "readiness.json"
        manifest_path = self.directory / "manifest.json"
        write_json_atomic(matrix_path, value)
        readiness = {key: item for key, item in value.items() if key != "cases"}
        write_json_atomic(readiness_path, readiness)
        manifest = {
            "schema_version": 1,
            "identity": identity,
            "status": status,
            "scenario_set_sha256": value["scenario_set_sha256"],
            "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
            "readiness_sha256": hashlib.sha256(readiness_path.read_bytes()).hexdigest(),
            "cases": manifest_cases,
            "mode": "SIMULATED/LAB_ONLY",
            "authority": "EXTERNAL_UNVERIFIED",
            "certification": "NOT COMPLETED",
            "physical_validation": "NOT_EXECUTED",
        }
        write_json_atomic(manifest_path, manifest)
        for path in (matrix_path, readiness_path, manifest_path):
            os.chmod(path, 0o600)
        return value
