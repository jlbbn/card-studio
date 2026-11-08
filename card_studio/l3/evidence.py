from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from card_studio.evidence.store import EvidenceStore
from card_studio.store import write_json_atomic

from .model import L3CaseResult, L3Report


class L3EvidenceStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.cases = EvidenceStore(directory / "cases")

    def write_case(self, result: L3CaseResult) -> Path:
        path = self.cases.write(f"{result.test_case_id}.json", result.to_dict())
        result.evidence_reference = path.name
        return path

    def write_report(self, report: L3Report) -> tuple[Path, Path]:
        report_path = self.directory / "matrix.json"
        readiness_path = self.directory / "readiness.json"
        manifest_path = self.directory / "manifest.json"
        write_json_atomic(report_path, report.to_dict())
        value = report.to_dict()
        value.pop("cases", None)
        write_json_atomic(readiness_path, value)
        cases = []
        for case in report.cases:
            evidence_path = self.directory / "cases" / case.evidence_reference
            cases.append({
                "test_case_id": case.test_case_id,
                "evidence_reference": case.evidence_reference,
                "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "profile_ref": case.profile_ref,
                "profile_digest": case.profile_digest,
                "identity": case.identity(),
            })
        manifest = {
            "schema_version": 1,
            "identity": report.identity,
            "conformant": report.conformant,
            "p0_gates": dict(report.p0_gates),
            "p0_complete": report.p0_complete,
            "matrix_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "readiness_sha256": hashlib.sha256(readiness_path.read_bytes()).hexdigest(),
            "cases": cases,
            "simulation": True,
            "mode": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
        }
        write_json_atomic(manifest_path, manifest)
        os.chmod(report_path, 0o600)
        os.chmod(readiness_path, 0o600)
        os.chmod(manifest_path, 0o600)
        return report_path, readiness_path

    @staticmethod
    def private_file(path: Path) -> bool:
        return stat.S_IMODE(path.stat().st_mode) == 0o600
