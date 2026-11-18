from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from card_studio import __build__, __version__


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_tree_digest(root: Path) -> str:
    paths = [
        *sorted((root / "card_studio").rglob("*.py")),
        *sorted((root / "tests").glob("test_*.py")),
        *sorted((root / "migrations").glob("*.sql")),
        *sorted(root.glob("requirements*.txt")),
        root / "Dockerfile.p3",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_manifest(root: Path, image_digest: str, regression_passed: int, regression_total: int) -> dict[str, Any]:
    if not _DIGEST.fullmatch(image_digest):
        raise ValueError("La imagen requiere digest SHA-256 inmutable.")
    if regression_total <= 0 or regression_passed != regression_total:
        raise ValueError("La regresión completa debe ser verde.")
    migration = root / "migrations" / "001_p3_postgresql.sql"
    dockerfile = root / "Dockerfile.p3"
    lock = root / "requirements-p3-lock.txt"
    tree = source_tree_digest(root)
    return {
        "schema_version": 1,
        "service": "card-studio-swift-ledger-p3",
        "version": __version__,
        "build": __build__,
        "commit_provenance": f"tree-sha256:{tree}",
        "source_tree_sha256": tree,
        "image_digest": image_digest,
        "dockerfile_sha256": file_digest(dockerfile),
        "requirements_lock_sha256": file_digest(lock),
        "migration": {"version": "001_p3_postgresql", "sha256": file_digest(migration), "gate": "PASS"},
        "deployment": {"environment": "SIMULATED", "manifest_required": True, "deployed": False},
        "regression_gate": {"status": "PASS", "passed": regression_passed, "total": regression_total},
        "rollback": {"image_digest": image_digest, "procedure": "docs/BACKUP_RECOVERY_P3.md#rollback"},
        "physical_validation": "NOT_EXECUTED",
        "certification": "NOT COMPLETED",
    }


def validate_manifest(manifest: Mapping[str, Any]) -> bool:
    try:
        return bool(
            manifest["schema_version"] == 1
            and manifest["version"]
            and manifest["build"]
            and _DIGEST.fullmatch(manifest["image_digest"])
            and _DIGEST.fullmatch(manifest["rollback"]["image_digest"])
            and re.fullmatch(r"tree-sha256:[0-9a-f]{64}", manifest["commit_provenance"])
            and re.fullmatch(r"[0-9a-f]{64}", manifest["source_tree_sha256"])
            and manifest["migration"]["gate"] == "PASS"
            and manifest["deployment"]["manifest_required"] is True
            and manifest["regression_gate"]["status"] == "PASS"
            and manifest["regression_gate"]["passed"] == manifest["regression_gate"]["total"] > 0
        )
    except (KeyError, TypeError, ValueError):
        return False


def deployment_decision(manifest: Mapping[str, Any], requested_image_digest: str) -> dict[str, Any]:
    valid = validate_manifest(manifest)
    matches = valid and manifest["image_digest"] == requested_image_digest
    return {
        "allowed": matches,
        "reason": "MANIFEST_VALID" if matches else "RELEASE_BLOCKED_MANIFEST_OR_DIGEST",
        "deployed": False,
    }


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    if not validate_manifest(manifest):
        raise ValueError("Manifest P3 incompleto o inválido.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
