from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from card_studio.interfaces.base import CardInterface
from card_studio.transactions.recovery import RecoveryClass

from .apdu import ApduContext, CommandApdu, StatusAction, exchange_with_followups
from .contactless import ContactlessError
from .cryptogram import CryptogramResult, CryptogramType
from .data import EmvDataStore, OwnershipDomain
from .dol import DolDocument, DolKind
from .gac import parse_generate_ac_response


class RecoverAcError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome: str = "CERTAIN_FAILURE",
        recovery: RecoveryClass = RecoveryClass.OPERATOR_REVIEW,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome
        self.recovery = recovery


@dataclass(frozen=True)
class TornRecord:
    operation_id: str
    profile_ref: str
    expected_atc: bytes
    status: str
    recovery_digest: str | None = None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "profile_ref": self.profile_ref,
            "expected_atc": self.expected_atc.hex().upper(),
            "status": self.status,
            "recovery_digest": self.recovery_digest,
            "mode": "LAB_ONLY",
        }


class TornTransactionJournal:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            connection.execute("""
                CREATE TABLE IF NOT EXISTS torn_transactions (
                    operation_id TEXT PRIMARY KEY,
                    profile_ref TEXT NOT NULL,
                    expected_atc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    recovery_digest TEXT
                )
            """)
            connection.commit()
            self._initialized = True

    @staticmethod
    def _context(operation_id: str, profile_ref: str, expected_atc: bytes) -> bytes:
        if not operation_id or not profile_ref:
            raise RecoverAcError("INVALID_TORN_CONTEXT", "La transacción torn requiere operación y perfil.")
        value = bytes(expected_atc)
        if len(value) != 2:
            raise RecoverAcError("INVALID_TORN_ATC", "ATC esperado debe ocupar dos bytes.")
        return value

    def mark_torn(self, operation_id: str, profile_ref: str, expected_atc: bytes) -> TornRecord:
        atc = self._context(operation_id, profile_ref, expected_atc)
        connection = self._connect()
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT profile_ref, expected_atc, status, recovery_digest FROM torn_transactions WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                if row[0] != profile_ref or row[1] != atc.hex().upper():
                    raise RecoverAcError("TORN_CONTEXT_CONFLICT", "La transacción torn existe con otro contexto.")
                connection.commit()
                return TornRecord(operation_id, row[0], bytes.fromhex(row[1]), row[2], row[3])
            connection.execute(
                "INSERT INTO torn_transactions(operation_id, profile_ref, expected_atc, status) VALUES(?, ?, ?, 'TORN')",
                (operation_id, profile_ref, atc.hex().upper()),
            )
            connection.commit()
            return TornRecord(operation_id, profile_ref, atc, "TORN")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, operation_id: str) -> TornRecord | None:
        connection = self._connect()
        try:
            self._initialize(connection)
            row = connection.execute(
                "SELECT profile_ref, expected_atc, status, recovery_digest FROM torn_transactions WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            return TornRecord(operation_id, row[0], bytes.fromhex(row[1]), row[2], row[3])
        finally:
            connection.close()

    def mark_recovered(self, record: TornRecord, cryptogram: CryptogramResult) -> TornRecord:
        evidence = json.dumps(cryptogram.to_evidence(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(evidence).hexdigest()
        connection = self._connect()
        try:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM torn_transactions WHERE operation_id=? AND profile_ref=? AND expected_atc=?",
                (record.operation_id, record.profile_ref, record.expected_atc.hex().upper()),
            ).fetchone()
            if row is None:
                raise RecoverAcError("TORN_CONTEXT_CONFLICT", "El journal torn cambió de contexto.")
            if row[0] != "TORN":
                raise RecoverAcError("RECOVER_AC_ALREADY_COMPLETED", "RECOVER AC ya finalizó; se prohíbe reenviarlo.")
            connection.execute(
                "UPDATE torn_transactions SET status='RECOVERED', recovery_digest=? WHERE operation_id=?",
                (digest, record.operation_id),
            )
            connection.commit()
            return TornRecord(record.operation_id, record.profile_ref, record.expected_atc, "RECOVERED", digest)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def build_recover_ac_command(drdol: DolDocument, store: EmvDataStore) -> CommandApdu:
    if drdol.kind != DolKind.DRDOL:
        raise RecoverAcError("INVALID_DRDOL_KIND", "RECOVER AC requiere DRDOL.")
    for entry in drdol.entries:
        if store.require(entry.tag).ownership not in {OwnershipDomain.TERMINAL, OwnershipDomain.PROFILE, OwnershipDomain.KERNEL}:
            raise RecoverAcError("DRDOL_OWNERSHIP_CONFLICT", f"DRDOL contiene un ownership inválido: {entry.tag}.")
    return CommandApdu(0x80, 0xD0, 0x00, 0x00, drdol.build(store), 0x00)


@dataclass(frozen=True)
class RecoverAcResult:
    cryptogram: CryptogramResult
    torn: TornRecord
    command_count: int

    def to_evidence(self) -> dict[str, Any]:
        return {
            "cryptogram_result": self.cryptogram.to_evidence(),
            "torn": self.torn.to_evidence(),
            "command_count": self.command_count,
            "second_generate_ac": False,
            "mode": "LAB_ONLY",
        }


def recover_ac(
    card_interface: CardInterface,
    journal: TornTransactionJournal,
    operation_id: str,
    drdol: DolDocument,
    store: EmvDataStore,
    max_followups: int = 4,
) -> RecoverAcResult:
    record = journal.get(operation_id)
    if record is None:
        raise RecoverAcError("TORN_TRANSACTION_NOT_FOUND", "No existe una transacción torn registrada.")
    if record.status != "TORN":
        raise RecoverAcError("RECOVER_AC_ALREADY_COMPLETED", "La recuperación final no admite replay ni otro RECOVER AC.")
    command = build_recover_ac_command(drdol, store)
    exchange = exchange_with_followups(card_interface, command, context=ApduContext.GENERIC, max_followups=max_followups)
    if exchange.action != StatusAction.SUCCESS:
        raise RecoverAcError("RECOVER_AC_STATUS_ERROR", "RECOVER AC no terminó correctamente.")
    cryptogram = parse_generate_ac_response(exchange.response).result
    if cryptogram.atc != record.expected_atc:
        raise RecoverAcError("RECOVER_AC_ATC_MISMATCH", "El ATC recuperado no corresponde a la transacción torn.")
    if cryptogram.cryptogram_type == CryptogramType.ARQC:
        raise RecoverAcError("RECOVER_AC_SECOND_ONLINE_REQUEST", "RECOVER AC no puede iniciar otra autorización online.")
    recovered = journal.mark_recovered(record, cryptogram)
    return RecoverAcResult(cryptogram, recovered, exchange.command_count)


def enforce_relay_time_limit(started_at: float, limit_ms: int, clock: Callable[[], float] = time.monotonic) -> None:
    if not isinstance(limit_ms, int) or isinstance(limit_ms, bool) or not 0 <= limit_ms <= 600000:
        raise ContactlessError("INVALID_RELAY_LIMIT", "relay_time_limit_ms debe estar entre 0 y 600000.")
    if limit_ms and (clock() - started_at) * 1000 > limit_ms:
        raise ContactlessError(
            "RELAY_TIME_LIMIT_EXCEEDED",
            "El intercambio GPO excedió el límite relay LAB.",
            outcome="UNCERTAIN",
        )
