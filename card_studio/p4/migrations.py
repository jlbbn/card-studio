from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from card_studio.production.postgres import psycopg


class MigrationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PostgreSQLMigrationRunner:
    def __init__(self, dsn: str, migration_path: Path):
        if psycopg is None:
            raise MigrationError("POSTGRES_DRIVER_UNAVAILABLE")
        self.dsn = dsn
        self.migration_path = migration_path
        self.version = migration_path.stem
        self.digest = hashlib.sha256(migration_path.read_bytes()).hexdigest()

    def _connect(self) -> Any:
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)

    def apply(self, *, interrupt_before_commit: bool = False) -> dict[str, Any]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", ("card-p4-migration-lock",))
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS p4_migration_history (
                        version TEXT PRIMARY KEY,
                        sha256 TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                """)
                cursor.execute("SELECT sha256 FROM p4_migration_history WHERE version=%s", (self.version,))
                existing = cursor.fetchone()
                if existing is not None:
                    if existing[0] != self.digest:
                        raise MigrationError("INCOMPATIBLE_MIGRATION_DIGEST")
                    return {"status": "ALREADY_APPLIED", "version": self.version, "sha256": self.digest}
                cursor.execute("SELECT to_regclass('p3_schema_metadata')")
                if cursor.fetchone()[0] is not None:
                    cursor.execute("SELECT value FROM p3_schema_metadata WHERE key='schema_version'")
                    row = cursor.fetchone()
                    if row is None or row[0] != "1":
                        raise MigrationError("INCOMPATIBLE_SCHEMA")
                sql = self.migration_path.read_text(encoding="utf-8")
                sql = re.sub(r"^\s*BEGIN\s*;", "", sql, flags=re.IGNORECASE)
                sql = re.sub(r"COMMIT\s*;\s*$", "", sql, flags=re.IGNORECASE)
                cursor.execute("BEGIN")
                try:
                    cursor.execute(sql)
                    cursor.execute(
                        "INSERT INTO p4_migration_history(version, sha256, applied_at) VALUES (%s, %s, %s)",
                        (self.version, self.digest, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                    )
                    if interrupt_before_commit:
                        raise MigrationError("SYNTHETIC_MIGRATION_INTERRUPTION")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                return {"status": "APPLIED", "version": self.version, "sha256": self.digest}
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("card-p4-migration-lock",))
            finally:
                connection.close()

    def verify(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT sha256 FROM p4_migration_history WHERE version=%s", (self.version,))
                history = cursor.fetchone()
                cursor.execute("SELECT value FROM p3_schema_metadata WHERE key='schema_version'")
                schema = cursor.fetchone()
                cursor.execute("SELECT to_regclass('p3_operations'), to_regclass('p3_effects')")
                tables = cursor.fetchone()
                valid = bool(
                    history is not None and history[0] == self.digest
                    and schema is not None and schema[0] == "1"
                    and all(tables)
                )
                if not valid:
                    raise MigrationError("MIGRATION_VERIFY_FAILED")
                return {"status": "PASS", "version": self.version, "schema_version": 1, "sha256": self.digest}
        finally:
            connection.close()
