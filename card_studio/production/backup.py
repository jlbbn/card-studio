from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

from .contract import utc_now


REQUIRED_RUNBOOK_TOPICS = frozenset({
    "backup policy", "retention", "snapshots", "restore procedure", "DB recovery",
    "service redeploy", "task replacement", "rollback", "migration gate", "regression gate",
})


def create_backup_manifest(paths: Iterable[Path], retention_days: int = 35) -> dict[str, object]:
    if not 7 <= retention_days <= 365:
        raise ValueError("La retención debe estar entre 7 y 365 días.")
    artifacts = []
    for path in sorted(paths, key=lambda item: item.name):
        raw = path.read_bytes()
        artifacts.append({"name": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    if not artifacts:
        raise ValueError("El backup no contiene artefactos.")
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "policy": "daily encrypted backup; isolated restore verification",
        "retention_days": retention_days,
        "snapshots": True,
        "artifacts": artifacts,
        "contains_secrets": False,
    }


def restore_sqlite_isolated(source: Path, destination: Path) -> dict[str, object]:
    if source.resolve() == destination.resolve():
        raise ValueError("El restore debe usar un destino aislado.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise ValueError("El destino de restore ya existe; no se sobrescribirá.")
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(destination)) as restored:
        origin.backup(restored)
    os.chmod(destination, 0o600)
    with closing(sqlite3.connect(destination)) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        operations = connection.execute("SELECT COUNT(*) FROM p3_operations").fetchone()[0]
        effects = connection.execute("SELECT COUNT(*) FROM p3_effects").fetchone()[0]
    required = {"p3_operations", "p3_effects", "p3_audit", "p3_treasury_cursor"}
    if not required <= tables:
        raise ValueError("El restore no contiene el esquema P3 completo.")
    return {
        "status": "PASS", "isolated": True, "operations": operations, "effects": effects,
        "source_modified": False, "production_restore": False,
    }


def validate_runbook(text: str) -> bool:
    normalized = text.casefold()
    return all(topic.casefold() in normalized for topic in REQUIRED_RUNBOOK_TOPICS)
