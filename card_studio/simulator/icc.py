from __future__ import annotations

import hashlib
import threading
from typing import Any

from card_studio.emv.apdu import ApduError, CommandApdu
from card_studio.l3.internal import prepare_internal_reader
from card_studio.profiles.model import CardProfile
from card_studio.readers.base import ReaderError, ReaderSession, TransportResult
from card_studio.readers.mock import MockReaderAdapter
from card_studio.transactions.model import TransactionRequest


class SyntheticIccReader(MockReaderAdapter):
    adapter_name = "stateful-synthetic-icc"
    adapter_version = "1"

    def __init__(self):
        super().__init__(responses={})
        self._reader_present = True
        self._block_instruction: int | None = None
        self.exchange_started = threading.Event()
        self.exchange_release = threading.Event()
        self.reset()

    def reset(self) -> None:
        self.stage = "RESET"
        self.directory: str | None = None
        self.selected_aid: str | None = None
        self.afl = bytes.fromhex("08010100")
        self.records = {(1, 1)}
        self.atc = 0
        self.torn = False
        self.reset_count = getattr(self, "reset_count", 0) + 1
        self.instructions: list[int] = []
        self.command_digests: list[str] = []
        self.commands: list[bytes] = []
        self.malformed_instruction: int | None = None
        self.malformed_payload = b"\x6f\x05\x84\x01"
        self.remove_card_at: int | None = None
        self.remove_reader_at: int | None = None
        self.torn_at: int | None = None
        self._reader_present = True
        self.card_available = True
        self._exchange_cancelled = False
        self._recovery_atc: int | None = None

    def prepare_recover_ac(self, command: CommandApdu, response: TransportResult, expected_atc: bytes) -> None:
        if command.ins != 0xD0 or len(expected_atc) != 2:
            raise ValueError("RECOVER AC requiere INS D0 y ATC de dos bytes.")
        self.responses[command.to_bytes().hex().upper()] = response
        self._recovery_atc = int.from_bytes(expected_atc, "big")
        self.stage = "RECOVERY_READY"
        self.torn = True
        self.card_available = True

    def cancel_exchange(self) -> None:
        self._exchange_cancelled = True
        self.exchange_release.set()

    def configure(
        self,
        profile: CardProfile,
        request: TransactionRequest,
        unpredictable_number: bytes | None = None,
    ) -> None:
        prepare_internal_reader(
            self,
            profile,
            request,
            unpredictable_number=unpredictable_number,
        )

    def open(self, reader_id: str, operation_id: str, deadline: float | None = None) -> ReaderSession:
        if not self._reader_present:
            raise ReaderError("READER_REMOVED", "SIMULATED: lector retirado.")
        return super().open(reader_id, operation_id, deadline)

    def remove_reader(self) -> None:
        self._reader_present = False
        self.card_available = False

    def block_exchange(self, instruction: int) -> None:
        self._block_instruction = instruction
        self.exchange_started.clear()
        self.exchange_release.clear()

    @staticmethod
    def _environment(command: CommandApdu) -> str | None:
        if command.ins != 0xA4:
            return None
        if command.data == b"1PAY.SYS.DDF01":
            return "PSE"
        if command.data == b"2PAY.SYS.DDF01":
            return "PPSE"
        return None

    def _valid_sequence(self, command: CommandApdu, environment: str | None) -> bool:
        if environment is not None:
            return self.stage == "RESET"
        if command.ins == 0xA4:
            return self.stage == "DIRECTORY_SELECTED"
        if command.ins == 0xA8:
            return self.stage == "APPLICATION_SELECTED"
        if command.ins == 0xB2:
            return self.stage in {"GPO_PROCESSED", "RECORDS_READ"} and (command.p2 >> 3, command.p1) in self.records
        if command.ins == 0xAE:
            return self.stage in {"RECORDS_READ", "CRYPTOGRAM_GENERATED", "ISSUER_AUTHENTICATED"}
        if command.ins == 0x82:
            return self.stage == "CRYPTOGRAM_GENERATED"
        if command.ins == 0xD0:
            return self.stage == "RECOVERY_READY"
        if command.ins == 0xC0:
            return True
        return False

    def _advance(self, command: CommandApdu, environment: str | None) -> None:
        if environment is not None:
            self.directory = environment
            self.stage = "DIRECTORY_SELECTED"
        elif command.ins == 0xA4:
            self.selected_aid = command.data.hex().upper()
            self.stage = "APPLICATION_SELECTED"
        elif command.ins == 0xA8:
            self.stage = "GPO_PROCESSED"
            if self.directory == "PPSE":
                self.atc += 1
        elif command.ins == 0xB2:
            self.stage = "RECORDS_READ"
        elif command.ins == 0xAE:
            self.atc += 1
            self.stage = "CRYPTOGRAM_GENERATED"
        elif command.ins == 0x82:
            self.stage = "ISSUER_AUTHENTICATED"
        elif command.ins == 0xD0:
            self.atc = self._recovery_atc if self._recovery_atc is not None else self.atc
            self.stage = "RECOVERED"
            self.torn = False

    def transmit(self, connection, command, sensitivity="REDACTED") -> TransportResult:
        if not self._reader_present:
            raise ReaderError("READER_REMOVED", "SIMULATED: lector retirado durante exchange.")
        if not self.card_available:
            raise ReaderError("CARD_REMOVED", "SIMULATED: tarjeta retirada.", outcome="UNCERTAIN")
        raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
        try:
            parsed = CommandApdu.parse(raw)
        except ApduError as error:
            raise ReaderError(error.code, error.message) from error
        if self._block_instruction == parsed.ins:
            self.exchange_started.set()
            if not self.exchange_release.wait(10):
                raise ReaderError("TRANSMIT_TIMEOUT", "SIMULATED: exchange bloqueado.", outcome="UNCERTAIN")
        if self._exchange_cancelled:
            raise ReaderError("OPERATOR_CANCELLED", "SIMULATED: exchange cancelado.", outcome="UNCERTAIN")
        self.instructions.append(parsed.ins)
        self.commands.append(raw)
        self.command_digests.append(hashlib.sha256(raw).hexdigest())
        environment = self._environment(parsed)
        key = raw.hex().upper()
        if self.remove_reader_at == parsed.ins:
            self.remove_reader()
            raise ReaderError("READER_REMOVED", "SIMULATED: lector retirado durante exchange.")
        if self.remove_card_at == parsed.ins:
            self.card_available = False
            raise ReaderError("CARD_REMOVED", "SIMULATED: tarjeta retirada.", outcome="UNCERTAIN")
        if self.torn_at == parsed.ins:
            self.torn = True
            self.card_available = False
            self.stage = "TORN"
            raise ReaderError("CONTACTLESS_TORN", "SIMULATED: contactless torn.", outcome="UNCERTAIN")
        if self.malformed_instruction == parsed.ins:
            return TransportResult(self.malformed_payload, "9000")
        if not self._valid_sequence(parsed, environment) or key not in self.responses:
            return TransportResult(sw1sw2="6A82" if parsed.ins in {0xA4, 0xB2} else "6D00")
        response = self.responses[key]
        if isinstance(response, bytes):
            response = TransportResult(response, "9000")
        if response.sw1sw2 == "9000":
            self._advance(parsed, environment)
        return response

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "adapter": self.adapter_name,
            "adapter_version": self.adapter_version,
            "stage": self.stage,
            "directory": self.directory,
            "selected_aid": self.selected_aid,
            "afl": self.afl.hex().upper(),
            "records": [f"{sfi}:{record}" for sfi, record in sorted(self.records)],
            "atc": self.atc,
            "torn": self.torn,
            "reset_count": self.reset_count,
            "instructions": list(self.instructions),
            "command_sha256": list(self.command_digests),
            "simulation": True,
            "mode": "LAB_ONLY",
            "authority": "EXTERNAL_UNVERIFIED",
            "physical_validation": "NOT_EXECUTED",
        }
