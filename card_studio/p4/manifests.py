from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from card_studio import __version__


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def p4_source_digest(root: Path) -> str:
    paths = [
        *sorted((root / "card_studio").rglob("*.py")),
        *sorted((root / "tests").glob("test_*.py")),
        *sorted((root / "migrations").glob("*.sql")),
        *sorted(root.glob("requirements*.txt")),
        root / "Dockerfile.p4",
        root / "staging" / "config.json",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_staging_config(config: Mapping[str, Any]) -> bool:
    return bool(
        config.get("environment") == "STAGING_ISOLATED"
        and config.get("production") is False
        and config.get("physical_validation") is False
        and config.get("monetary_operations") is False
        and config.get("data_classification") == "SYNTHETIC_ONLY"
        and config.get("external_endpoints") == []
        and set(config.get("services", {})) == {"sunmi-pos", "tesoreria", "swiftledger"}
        and str(config.get("service_secret_reference", "")).startswith("env:")
        and str(config.get("database_secret_reference", "")).startswith("env:")
    )


def build_rc1_manifest(
    root: Path,
    image_digest: str,
    rollback_digest: str,
    regression_passed: int,
    regression_total: int,
) -> dict[str, Any]:
    if not _DIGEST.fullmatch(image_digest) or not _DIGEST.fullmatch(rollback_digest):
        raise ValueError("RC1 y rollback requieren digests SHA-256 exactos.")
    if regression_total <= 0 or regression_passed != regression_total:
        raise ValueError("La regresión RC1 debe ser completamente verde.")
    config_path = root / "staging" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not validate_staging_config(config):
        raise ValueError("Staging no es un entorno aislado sintético válido.")
    source_hash = p4_source_digest(root)
    build_hash = hashlib.sha256(
        (root / "Dockerfile.p4").read_bytes() + (root / "requirements-p4-lock.txt").read_bytes()
    ).hexdigest()
    migration = root / "migrations" / "001_p3_postgresql.sql"
    return {
        "schema_version": 1,
        "release_candidate": "RC1",
        "version": f"{__version__}-p4-rc1",
        "commit_sha": source_hash,
        "commit_type": "CONTENT_SHA256_NOT_VCS",
        "source_hash": source_hash,
        "build_hash": build_hash,
        "image": f"card-studio-p4@{image_digest}",
        "image_digest": image_digest,
        "dependency_lock": {
            "path": "requirements-p4-lock.txt",
            "sha256": sha256_file(root / "requirements-p4-lock.txt"),
            "pinned": True,
        },
        "migration_manifest": {
            "version": "001_p3_postgresql",
            "schema_version": 1,
            "sha256": sha256_file(migration),
            "strategy": "ADDITIVE_BACKWARD_COMPATIBLE",
        },
        "configuration_manifest": {
            "path": "staging/config.json",
            "sha256": sha256_file(config_path),
            "environment": "STAGING_ISOLATED",
            "contains_secrets": False,
        },
        "rollback_target": {
            "image": f"card-studio-p4@{rollback_digest}",
            "image_digest": rollback_digest,
            "schema_compatible": True,
        },
        "evidence_manifest": "release/p4/rc1/evidence-manifest.json",
        "task_revisions": {name: values["revision"] for name, values in config["services"].items()},
        "regression_gate": {"passed": regression_passed, "total": regression_total, "status": "PASS"},
        "deployment": {"environment": "STAGING_ISOLATED", "deployed": False, "production": False},
        "physical_validation": "DEFERRED / NOT EXECUTED",
        "certification": "NOT COMPLETED",
    }


def validate_rc1_manifest(manifest: Mapping[str, Any]) -> bool:
    try:
        return bool(
            manifest["release_candidate"] == "RC1"
            and manifest["version"]
            and re.fullmatch(r"[0-9a-f]{64}", manifest["commit_sha"])
            and manifest["commit_type"] == "CONTENT_SHA256_NOT_VCS"
            and manifest["source_hash"] == manifest["commit_sha"]
            and re.fullmatch(r"[0-9a-f]{64}", manifest["build_hash"])
            and _DIGEST.fullmatch(manifest["image_digest"])
            and manifest["image"].endswith(manifest["image_digest"])
            and manifest["dependency_lock"]["pinned"] is True
            and manifest["migration_manifest"]["schema_version"] == 1
            and manifest["configuration_manifest"]["contains_secrets"] is False
            and _DIGEST.fullmatch(manifest["rollback_target"]["image_digest"])
            and manifest["rollback_target"]["schema_compatible"] is True
            and manifest["regression_gate"]["status"] == "PASS"
            and manifest["regression_gate"]["passed"] == manifest["regression_gate"]["total"] > 0
            and manifest["deployment"]["production"] is False
        )
    except (KeyError, TypeError, ValueError):
        return False


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
