from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .deployment import utc_now
from .manifests import write_json


def write_evidence_manifest(root: Path, artifacts: Iterable[Path], output: Path, regression: dict[str, int]) -> dict[str, Any]:
    entries = []
    for path in sorted(artifacts, key=lambda item: item.as_posix()):
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })
    value = {
        "schema_version": 1,
        "release_candidate": "RC1",
        "created_at": utc_now(),
        "regression": regression,
        "artifacts": entries,
        "evidence_level": "SIMULATED / STAGING_ISOLATED",
        "contains_secrets": False,
        "physical_validation": "DEFERRED / NOT EXECUTED",
        "certification": "NOT COMPLETED",
    }
    write_json(output, value)
    return value
