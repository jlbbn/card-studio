from __future__ import annotations

import contextlib
import hashlib
import multiprocessing
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from card_studio.application import CardStudioApplication
from card_studio.emv.c8_recovery import (
    RecoverAcError,
    TornTransactionJournal,
    build_recover_ac_command,
    recover_ac,
)
from card_studio.emv.card_action import synthetic_generate_ac_response
from card_studio.emv.data import DataRecord, DataSource, EmvDataStore, OwnershipDomain
from card_studio.emv.dol import DolDataType, DolDocument, DolEntry, DolKind
from card_studio.host.mock import MockAuthorizationHost
from card_studio.interfaces.contactless import ContactlessInterface
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.transactions.model import TransactionRequest, TransactionType
from card_studio.transactions.recovery import RecoveryError

from .icc import SyntheticIccReader


CONTACT_OFFLINE = [0xA4, 0xA4, 0xA8, 0xB2, 0xAE]
CONTACT_ONLINE = [*CONTACT_OFFLINE, 0xAE]


@dataclass
class IntegrationOutcome:
    result: dict[str, Any]
    icc: dict[str, Any]
    assertions: list[dict[str, Any]]

    @property
    def status(self) -> str:
        return "PASS" if self.assertions and all(item["status"] == "PASS" for item in self.assertions) else "FAIL"


class _SlowSyntheticIccReader(SyntheticIccReader):
    def transmit(self, connection, command, sensitivity="REDACTED"):
        if not self.commands:
            time.sleep(0.35)
        return super().transmit(connection, command, sensitivity)


def _process_execute(
    root: str,
    request_data: dict[str, Any],
    start: Any,
    output: Any,
    delay: bool,
) -> None:
    try:
        request = TransactionRequest.from_dict(request_data)
        kernel = InternalEmvKernel()
        profile = internal_profile(kernel, request.operation_id, request.interface, request.scenario)
        reader = _SlowSyntheticIccReader() if delay else SyntheticIccReader()
        reader.configure(profile, request)
        app = CardStudioApplication(
            data_dir=Path(root),
            profiles=[profile],
            kernels=kernel,
            reader=reader,
            host=MockAuthorizationHost(Path(root) / "host-v1.sqlite3"),
        )
        start.wait(10)
        result = app.create_transaction(request)
        output.put({
            "ok": True,
            "result": result,
            "instructions": list(reader.instructions),
            "icc": reader.snapshot(),
            "kernel_execution_count": kernel.execution_count(request.operation_id),
            "process_id": os.getpid(),
            "process_name": multiprocessing.current_process().name,
        })
    except Exception as error:
        output.put({"ok": False, "error": type(error).__name__, "code": getattr(error, "code", None)})


def _run_pair(
    directory: Path,
    first: TransactionRequest,
    second: TransactionRequest,
    delay: bool = False,
) -> list[dict[str, Any]]:
    context = multiprocessing.get_context("fork")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_execute,
            args=(str(directory), request.to_dict(), start, output, delay and index == 0),
        )
        for index, request in enumerate((first, second))
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        results = [output.get(timeout=20) for _ in processes]
    finally:
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(2)
    exit_codes = [process.exitcode for process in processes]
    if exit_codes != [0, 0] or not all(item.get("ok") for item in results):
        raise RuntimeError(f"Internal integration workers failed: {exit_codes}, {results}")
    for item in results:
        item["process_exit_codes"] = exit_codes
    return results


def _process_evidence(workers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(workers),
        "process_ids": sorted(worker["process_id"] for worker in workers),
        "process_names": sorted(worker["process_name"] for worker in workers),
        "exit_codes": list(workers[0]["process_exit_codes"]),
    }


class _Harness:
    def __init__(self, directory: Path):
        self.directory = directory
        self.kernel = InternalEmvKernel()
        self.host = MockAuthorizationHost(directory / "host-v1.sqlite3")

    def profile(self, kernel: InternalEmvKernel, scenario: str):
        return internal_profile(kernel, "p1-integrated-transactions", "CONTACT", scenario)

    def app(self, scenario: str = "approved") -> CardStudioApplication:
        kernel = InternalEmvKernel()
        return CardStudioApplication(
            data_dir=self.directory,
            profiles=[self.profile(kernel, scenario)],
            kernels=kernel,
            reader=SyntheticIccReader(),
            host=self.host,
        )

    def request(
        self,
        operation_id: str,
        scenario: str = "approved",
        amount: int = 1000,
        **linked: Any,
    ) -> TransactionRequest:
        profile = self.profile(self.kernel, scenario)
        profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
        return TransactionRequest.create(
            money=Money(amount, "MXN", 2),
            profile_ref=profile_ref,
            interface="CONTACT",
            kernel_ref=self.kernel.kernel_ref,
            scenario=scenario,
            operation_id=operation_id,
            idempotency_key=f"key-{operation_id}",
            **linked,
        )

    def execute(self, app: CardStudioApplication, request: TransactionRequest) -> dict[str, Any]:
        app.reader.reset()
        app.reader.configure(app.default_profile, request)
        return app.create_transaction(request)

    def purchase(self, operation_id: str) -> dict[str, Any]:
        app = self.app("online_required")
        result = self.execute(app, self.request(operation_id, "online_required"))
        if result["execution_state"] != "SUCCEEDED" or result["kernel_adapter"] != "InternalEmvKernel":
            raise RuntimeError("Internal original purchase did not succeed.")
        return result


class _Assertions:
    def __init__(self):
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, actual: Any, expected: Any) -> None:
        self.items.append({
            "name": name,
            "status": "PASS" if actual == expected else "FAIL",
            "actual": actual,
            "expected": expected,
        })


def _multiprocess_idempotency(directory: Path) -> IntegrationOutcome:
    harness = _Harness(directory)
    request = harness.request("p1-multiprocess-idempotency", "online_required")
    workers = _run_pair(directory, request, request, delay=True)
    results = [worker["result"] for worker in workers]
    counts = sorted(worker["kernel_execution_count"] for worker in workers)
    winner = next(worker for worker in workers if worker["kernel_execution_count"] == 1)
    duplicate = next(worker for worker in workers if worker["kernel_execution_count"] == 0)
    with contextlib.closing(sqlite3.connect(directory / "transactions-v1.sqlite3")) as connection:
        transaction_count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    signatures = [
        {
            "operation_id": result["operation_id"],
            "execution_state": result["execution_state"],
            "updated_at": result["updated_at"],
            "result_reference": result["result_reference"],
            "host_reference": result["details"].get("host_reference"),
            "host_response_timestamp": result["details"].get("host_response_timestamp"),
        }
        for result in results
    ]
    process_evidence = _process_evidence(workers)
    assertions = _Assertions()
    assertions.check("real_multiprocess_count", process_evidence["count"], 2)
    assertions.check("distinct_process_ids", len(set(process_evidence["process_ids"])), 2)
    assertions.check("process_exit_codes", process_evidence["exit_codes"], [0, 0])
    assertions.check("internal_worker_adapters", sorted({result["kernel_adapter"] for result in results}), ["InternalEmvKernel"])
    assertions.check("one_durable_transaction", transaction_count, 1)
    assertions.check("one_kernel_execution", sum(counts), 1)
    assertions.check("duplicate_execution_counts", counts, [0, 1])
    assertions.check("idempotent_result_convergence", signatures, [signatures[0], signatures[0]])
    assertions.check("single_host_authorization", harness.host.operation_count("authorize"), 1)
    assertions.check("winner_apdu_sequence", winner["instructions"], CONTACT_ONLINE)
    assertions.check("winner_generate_ac_count", winner["instructions"].count(0xAE), 2)
    assertions.check("duplicate_apdu_count", len(duplicate["instructions"]), 0)
    result = {
        "execution_state": "SUCCEEDED" if all(item["execution_state"] == "SUCCEEDED" for item in results) else "FAILED",
        "results": results,
        "transaction_count": transaction_count,
        "kernel_execution_counts": counts,
        "host_authorization_count": harness.host.operation_count("authorize"),
        "convergence_signatures": signatures,
        "processes": process_evidence,
    }
    return IntegrationOutcome(result, {"workers": [worker["icc"] for worker in workers]}, assertions.items)


def _concurrent_linked(directory: Path, transaction_type: str) -> IntegrationOutcome:
    harness = _Harness(directory)
    original = harness.purchase(f"p1-{transaction_type}-original")
    linked = {
        "transaction_type": transaction_type,
        "original_operation_id": original["operation_id"],
        "original_amount": Money(1000, "MXN", 2),
    }
    linked_amount = 600 if transaction_type == TransactionType.REFUND.value else 1000
    first = harness.request(f"p1-concurrent-{transaction_type}-a", amount=linked_amount, **linked)
    second = harness.request(f"p1-concurrent-{transaction_type}-b", amount=linked_amount, **linked)
    workers = _run_pair(directory, first, second)
    results = [worker["result"] for worker in workers]
    succeeded = [worker for worker in workers if worker["result"]["execution_state"] == "SUCCEEDED"]
    failed = [worker for worker in workers if worker["result"]["execution_state"] == "FAILED"]
    expected_error = "EXCESSIVE_REFUND" if transaction_type == TransactionType.REFUND.value else "DUPLICATE_REVERSAL"
    host_operation = "refund" if transaction_type == TransactionType.REFUND.value else "reverse"
    assertions = _Assertions()
    assertions.check("one_linked_winner", len(succeeded), 1)
    assertions.check("one_linked_loser", len(failed), 1)
    if len(succeeded) == 1 and len(failed) == 1:
        winner = succeeded[0]
        loser = failed[0]
        assertions.check("linked_negative_result", loser["result"].get("error_code"), expected_error)
        assertions.check("winner_kernel_execution", winner["kernel_execution_count"], 1)
        assertions.check("loser_kernel_execution", loser["kernel_execution_count"], 0)
        assertions.check("winner_apdu_sequence", winner["instructions"], CONTACT_OFFLINE)
        assertions.check("winner_generate_ac_count", winner["instructions"].count(0xAE), 1)
        assertions.check("loser_apdu_count", len(loser["instructions"]), 0)
        assertions.check(
            "original_host_reference",
            winner["result"]["details"].get("original_host_reference"),
            original["details"].get("host_reference"),
        )
        assertions.check("host_linked_operation", winner["result"]["details"].get("host_operation"), host_operation)
    assertions.check("single_original_authorization", harness.host.operation_count("authorize"), 1)
    assertions.check(f"single_host_{host_operation}", harness.host.operation_count(host_operation), 1)
    approved_amount = sum(worker["result"]["amount_minor"] for worker in succeeded)
    assertions.check("cumulative_amount_within_original", approved_amount <= 1000, True)
    result = {
        "execution_state": "SUCCEEDED" if len(succeeded) == 1 and len(failed) == 1 else "FAILED",
        "original": original,
        "results": results,
        "expected_negative_error": expected_error,
        "approved_amount_minor": approved_amount,
        "host_authorization_count": harness.host.operation_count("authorize"),
        "host_linked_operation_count": harness.host.operation_count(host_operation),
    }
    return IntegrationOutcome(result, {"workers": [worker["icc"] for worker in workers]}, assertions.items)


def _cancel_exchange(directory: Path) -> IntegrationOutcome:
    harness = _Harness(directory)
    app = harness.app()
    request = harness.request("p1-cancel-during-exchange")
    reader = app.reader
    reader.configure(app.default_profile, request)
    reader.block_exchange(0xA4)
    output: list[dict[str, Any]] = []
    failures: list[Exception] = []

    def execute() -> None:
        try:
            output.append(app.create_transaction(request))
        except Exception as error:
            failures.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    if not reader.exchange_started.wait(2):
        reader.cancel_exchange()
        thread.join(2)
        raise RuntimeError("Internal exchange did not reach cancellation barrier.")
    started = time.monotonic()
    cancelled = app.cancel(request.operation_id)
    elapsed = time.monotonic() - started
    thread.join(2)
    if thread.is_alive() or failures or len(output) != 1:
        reader.cancel_exchange()
        thread.join(1)
        raise RuntimeError("Internal exchange did not finish after cooperative cancellation.")
    final = app.get(request.operation_id)
    kernel = app.service.kernels[request.kernel_ref]
    assertions = _Assertions()
    assertions.check("cooperative_cancel_latency", elapsed < 1.0, True)
    assertions.check("cancel_uncertain", cancelled["execution_state"], "UNCERTAIN")
    assertions.check("exchange_uncertain", output[0]["execution_state"], "UNCERTAIN")
    assertions.check("no_post_cancellation_approval", final.get("functional_result") == "APPROVED", False)
    assertions.check("single_kernel_execution", kernel.execution_count(request.operation_id), 1)
    assertions.check("completed_apdu_count", len(reader.instructions), 0)
    assertions.check("host_authorization_count", harness.host.operation_count("authorize"), 0)
    result = {
        "execution_state": final["execution_state"],
        "error_code": final.get("error_code"),
        "cancel_result": cancelled,
        "exchange_result": output[0],
        "final_result": final,
        "cancel_latency_seconds": elapsed,
        "kernel_execution_count": kernel.execution_count(request.operation_id),
        "apdu_count": len(reader.instructions),
        "host_authorization_count": harness.host.operation_count("authorize"),
    }
    return IntegrationOutcome(result, reader.snapshot(), assertions.items)


def _torn_recovery(directory: Path) -> IntegrationOutcome:
    operation_id = "p1-contactless-torn-recovery"
    kernel = InternalEmvKernel()
    profile = internal_profile(kernel, operation_id, "CONTACTLESS", "online_required")
    profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
    request = TransactionRequest.create(
        money=Money(1000, "MXN", 2),
        profile_ref=profile_ref,
        interface="CONTACTLESS",
        kernel_ref=kernel.kernel_ref,
        scenario="online_required",
        operation_id=operation_id,
        idempotency_key=f"key-{operation_id}",
    )
    host = MockAuthorizationHost(directory / "host-v1.sqlite3")
    torn_reader = SyntheticIccReader()
    torn_reader.configure(profile, request)
    torn_reader.torn_at = 0xA8
    app = CardStudioApplication(
        data_dir=directory,
        profiles=[profile],
        kernels=kernel,
        reader=torn_reader,
        host=host,
    )
    torn_result = app.create_transaction(request)
    journal_path = directory / "torn.sqlite3"
    journal = TornTransactionJournal(journal_path)
    expected_atc = b"\x00\x01"
    journal.mark_torn(operation_id, profile_ref, expected_atc)
    torn_record = journal.get(operation_id)
    torn_snapshot = torn_reader.snapshot()

    restarted_journal = TornTransactionJournal(journal_path)
    restarted_record = restarted_journal.get(operation_id)
    drdol = DolDocument(DolKind.DRDOL, (DolEntry("9F37", 4, DolDataType.BINARY),))
    store = EmvDataStore()
    unpredictable = b"\x01\x02\x03\x04"
    store.put(DataRecord("9F37", unpredictable, DataSource.TERMINAL, OwnershipDomain.TERMINAL))
    command = build_recover_ac_command(drdol, store)
    command_digest = hashlib.sha256(command.to_bytes()).hexdigest()
    recovery_reader = SyntheticIccReader()
    recovery_reader.prepare_recover_ac(
        command,
        synthetic_generate_ac_response(0x40, 80, "p1-torn-recovery", atc=expected_atc),
        expected_atc,
    )
    session = recovery_reader.open(recovery_reader.reader_info.reader_id, f"{operation_id}-restart")
    card_interface = ContactlessInterface(recovery_reader, session).open()
    try:
        recovered = recover_ac(card_interface, restarted_journal, operation_id, drdol, store)
        replay_error = None
        try:
            recover_ac(card_interface, restarted_journal, operation_id, drdol, store)
        except RecoverAcError as error:
            replay_error = error.code
    finally:
        card_interface.close()
    retry_error = None
    try:
        app.retry_transaction(request)
    except RecoveryError as error:
        retry_error = error.code
    persisted = TornTransactionJournal(journal_path).get(operation_id)

    assertions = _Assertions()
    assertions.check("torn_execution_uncertain", torn_result["execution_state"], "UNCERTAIN")
    assertions.check("torn_error", torn_result.get("error_code"), "CONTACTLESS_TORN")
    assertions.check("torn_apdu_sequence", torn_reader.instructions, [0xA4, 0xA4, 0xA8])
    assertions.check("durable_torn_record", torn_record.status if torn_record else None, "TORN")
    assertions.check("restart_torn_record", restarted_record.status if restarted_record else None, "TORN")
    assertions.check("recover_instruction_sequence", recovery_reader.instructions, [0xD0])
    assertions.check("exact_recover_command", recovery_reader.command_digests, [command_digest])
    assertions.check("exact_drdol_payload", command.data.hex().upper(), unpredictable.hex().upper())
    assertions.check("recover_command_count", recovered.command_count, 1)
    assertions.check("recovered_expected_atc", recovered.cryptogram.atc.hex().upper(), expected_atc.hex().upper())
    assertions.check("recovered_tc_not_arqc", recovered.cryptogram.cryptogram_type.value, "TC")
    assertions.check("recovered_functional_result", recovered.cryptogram.functional_result.value, "APPROVED")
    assertions.check("recovered_journal_status", persisted.status if persisted else None, "RECOVERED")
    assertions.check("recovered_journal_digest", bool(persisted.recovery_digest) if persisted else False, True)
    assertions.check("replay_blocked", replay_error, "RECOVER_AC_ALREADY_COMPLETED")
    assertions.check("replay_no_additional_apdu", len(recovery_reader.instructions), 1)
    assertions.check("no_generate_ac_during_recovery", recovery_reader.instructions.count(0xAE), 0)
    assertions.check("no_second_generate_ac", recovered.to_evidence()["second_generate_ac"], False)
    assertions.check("unsafe_retry_blocked", retry_error, "NON_RETRYABLE")
    assertions.check("no_second_host_authorization", host.operation_count("authorize"), 0)
    result = {
        "execution_state": "SUCCEEDED" if persisted and persisted.status == "RECOVERED" else "FAILED",
        "torn_result": torn_result,
        "torn_record": torn_record.to_evidence() if torn_record else None,
        "restart_record": restarted_record.to_evidence() if restarted_record else None,
        "recovery": recovered.to_evidence(),
        "persisted_record": persisted.to_evidence() if persisted else None,
        "recover_command_sha256": command_digest,
        "drdol_sha256": hashlib.sha256(command.data).hexdigest(),
        "recover_ac_command_count": recovered.command_count,
        "replay_error": replay_error,
        "retry_error": retry_error,
        "host_authorization_count": host.operation_count("authorize"),
    }
    return IntegrationOutcome(
        result,
        {"before_restart": torn_snapshot, "after_restart": recovery_reader.snapshot()},
        assertions.items,
    )


def run_integration(kind: str, directory: Path) -> IntegrationOutcome:
    runners: dict[str, Callable[[Path], IntegrationOutcome]] = {
        "multiprocess_idempotency": _multiprocess_idempotency,
        "concurrent_refund": lambda path: _concurrent_linked(path, TransactionType.REFUND.value),
        "concurrent_reversal": lambda path: _concurrent_linked(path, TransactionType.REVERSAL.value),
        "cancel_exchange": _cancel_exchange,
        "torn_recovery": _torn_recovery,
    }
    try:
        runner = runners[kind]
    except KeyError as error:
        raise ValueError(f"Unknown Internal integration: {kind}") from error
    return runner(directory)
