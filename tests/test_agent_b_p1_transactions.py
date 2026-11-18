from __future__ import annotations

import contextlib
import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from card_studio.application import CardStudioApplication
from card_studio.host.base import HostError
from card_studio.host.mock import MockAuthorizationHost
from card_studio.kernels.internal_emv import InternalEmvKernel
from card_studio.l3.internal import internal_profile
from card_studio.money import Money
from card_studio.simulator.icc import SyntheticIccReader
from card_studio.transactions.errors import SimulatedCrash
from card_studio.transactions.model import TransactionRequest, TransactionType


CONTACT_OFFLINE = [0xA4, 0xA4, 0xA8, 0xB2, 0xAE]
CONTACT_ONLINE = [*CONTACT_OFFLINE, 0xAE]


class SlowSyntheticIccReader(SyntheticIccReader):
    def transmit(self, connection, command, sensitivity="REDACTED"):
        if not self.commands:
            time.sleep(0.35)
        return super().transmit(connection, command, sensitivity)


def process_execute_internal(root: str, request_data: dict, start, output, delay: bool = False) -> None:
    try:
        request = TransactionRequest.from_dict(request_data)
        kernel = InternalEmvKernel()
        profile = internal_profile(kernel, request.operation_id, request.interface, request.scenario)
        reader = SlowSyntheticIccReader() if delay else SyntheticIccReader()
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
            "kernel_adapter": result["kernel_adapter"],
            "kernel_execution_count": kernel.execution_count(request.operation_id),
        })
    except Exception as error:
        output.put({"ok": False, "error": type(error).__name__, "code": getattr(error, "code", None)})


class P1TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = InternalEmvKernel()
        self.profile = internal_profile(self.kernel, "p1-transactions", "CONTACT", "approved")
        self.host = MockAuthorizationHost(self.root / "host-v1.sqlite3")
        self.app = self.make_app()

    def tearDown(self):
        self.temporary.cleanup()

    def profile_for(self, scenario):
        return internal_profile(self.kernel, "p1-transactions", "CONTACT", scenario)

    def make_app(self, kernel=None, host=None, reader=None, scenario="approved"):
        kernel = kernel or self.kernel
        profile = internal_profile(kernel, "p1-transactions", "CONTACT", scenario)
        return CardStudioApplication(
            data_dir=self.root,
            profiles=[profile],
            kernels=kernel,
            reader=reader or SyntheticIccReader(),
            host=host or self.host,
        )

    def request(self, operation_id, scenario="approved", amount=1000, **kwargs):
        profile = self.profile_for(scenario)
        profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"
        return TransactionRequest.create(
            money=Money(amount, "MXN", 2),
            profile_ref=profile_ref,
            interface="CONTACT",
            kernel_ref=self.kernel.kernel_ref,
            scenario=scenario,
            operation_id=operation_id,
            idempotency_key=kwargs.pop("idempotency_key", f"key-{operation_id}"),
            **kwargs,
        )

    def execute(self, app, request):
        app.reader.reset()
        app.reader.configure(app.default_profile, request)
        return app.create_transaction(request)

    def purchase(self, operation_id="original", scenario="online_required"):
        app = self.make_app(scenario=scenario)
        result = self.execute(app, self.request(operation_id, scenario=scenario))
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["kernel_adapter"], "InternalEmvKernel")
        return result

    def run_pair(self, first, second, delay=False):
        context = multiprocessing.get_context("fork")
        start = context.Event()
        output = context.Queue()
        processes = [
            context.Process(
                target=process_execute_internal,
                args=(str(self.root), request.to_dict(), start, output, delay and index == 0),
            )
            for index, request in enumerate((first, second))
        ]
        for process in processes:
            process.start()
        start.set()
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        self.assertTrue(all(item["ok"] for item in results), results)
        self.assertEqual({item["kernel_adapter"] for item in results}, {"InternalEmvKernel"})
        return results

    def test_t15_two_processes_same_key_execute_once_and_converge(self):
        request = self.request("process-shared", scenario="online_required")
        items = self.run_pair(request, request, delay=True)
        results = [item["result"] for item in items]
        self.assertEqual({item["execution_state"] for item in results}, {"SUCCEEDED"})
        self.assertEqual({item["operation_id"] for item in results}, {"process-shared"})
        self.assertEqual({item["updated_at"] for item in results}, {results[0]["updated_at"]})
        self.assertEqual({item["result_reference"] for item in results}, {results[0]["result_reference"]})
        self.assertEqual({item["kernel_adapter"] for item in results}, {"InternalEmvKernel"})
        self.assertEqual(self.host.operation_count("authorize"), 1)
        self.assertEqual(sorted(item["kernel_execution_count"] for item in items), [0, 1])
        executed = [item for item in items if item["kernel_execution_count"] == 1]
        duplicate = [item for item in items if item["kernel_execution_count"] == 0]
        self.assertEqual(executed[0]["instructions"], CONTACT_ONLINE)
        self.assertEqual(executed[0]["instructions"].count(0xAE), 2)
        self.assertEqual(duplicate[0]["instructions"], [])
        with contextlib.closing(sqlite3.connect(self.root / "transactions-v1.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)

    def test_t13_t16_concurrent_refunds_reserve_original_atomically(self):
        original = self.purchase()
        linked = {
            "transaction_type": TransactionType.REFUND.value,
            "original_operation_id": original["operation_id"],
            "original_amount": Money(1000, "MXN", 2),
        }
        first = self.request("refund-a", amount=600, **linked)
        second = self.request("refund-b", amount=600, **linked)
        items = self.run_pair(first, second)
        results = [item["result"] for item in items]
        self.assertEqual({item["execution_state"] for item in results}, {"SUCCEEDED", "FAILED"})
        failed = next(item for item in results if item["execution_state"] == "FAILED")
        self.assertEqual(failed["error_code"], "EXCESSIVE_REFUND")
        self.assertEqual(self.host.operation_count("refund"), 1)
        self.assertEqual(self.host.operation_count("authorize"), 1)
        approved_item = next(item for item in items if item["result"]["execution_state"] == "SUCCEEDED")
        failed_item = next(item for item in items if item["result"]["execution_state"] == "FAILED")
        approved = approved_item["result"]
        self.assertEqual(approved["details"]["original_host_reference"], original["details"]["host_reference"])
        self.assertEqual(approved["details"]["host_operation"], "refund")
        self.assertEqual(approved_item["instructions"], CONTACT_OFFLINE)
        self.assertEqual(approved_item["instructions"].count(0xAE), 1)
        self.assertEqual(failed_item["instructions"], [])
        self.assertEqual(failed_item["kernel_execution_count"], 0)

    def test_t14_t16_concurrent_reversal_has_one_winner(self):
        original = self.purchase()
        linked = {
            "transaction_type": TransactionType.REVERSAL.value,
            "original_operation_id": original["operation_id"],
            "original_amount": Money(1000, "MXN", 2),
        }
        items = self.run_pair(
            self.request("reversal-a", **linked),
            self.request("reversal-b", **linked),
        )
        results = [item["result"] for item in items]
        self.assertEqual({item["execution_state"] for item in results}, {"SUCCEEDED", "FAILED"})
        failed = next(item for item in results if item["execution_state"] == "FAILED")
        self.assertEqual(failed["error_code"], "DUPLICATE_REVERSAL")
        self.assertEqual(self.host.operation_count("reverse"), 1)
        self.assertEqual(self.host.operation_count("authorize"), 1)
        approved_item = next(item for item in items if item["result"]["execution_state"] == "SUCCEEDED")
        failed_item = next(item for item in items if item["result"]["execution_state"] == "FAILED")
        self.assertEqual(approved_item["instructions"], CONTACT_OFFLINE)
        self.assertEqual(approved_item["instructions"].count(0xAE), 1)
        self.assertEqual(failed_item["instructions"], [])
        self.assertEqual(failed_item["kernel_execution_count"], 0)

    def test_t16_crash_reservation_is_conservative_until_cancel(self):
        original = self.purchase()
        linked = {
            "transaction_type": TransactionType.REFUND.value,
            "original_operation_id": original["operation_id"],
            "original_amount": Money(1000, "MXN", 2),
        }

        def crash(_operation_id):
            raise SimulatedCrash("after_created")

        crashing = self.make_app(host=self.host)
        crashing.service.hooks["after_created"] = crash
        crashed_request = self.request("refund-crash", amount=700, **linked)
        self.assertRaises(SimulatedCrash, crashing.create_transaction, crashed_request)
        self.assertEqual(crashing.service.kernels[self.kernel.kernel_ref].execution_count(crashed_request.operation_id), 0)
        blocked_request = self.request("refund-blocked", amount=400, **linked)
        blocked = self.execute(self.app, blocked_request)
        self.assertEqual(blocked["error_code"], "EXCESSIVE_REFUND")
        self.assertEqual(self.app.reader.instructions, [])
        self.assertEqual(self.app.cancel("refund-crash")["execution_state"], "CANCELLED")
        accepted = self.execute(self.app, self.request("refund-after-cancel", amount=400, **linked))
        self.assertEqual(accepted["execution_state"], "SUCCEEDED")
        self.assertEqual(accepted["kernel_adapter"], "InternalEmvKernel")
        self.assertEqual(self.app.reader.instructions, CONTACT_OFFLINE)
        self.assertEqual(self.app.reader.instructions.count(0xAE), 1)
        self.assertEqual(self.host.operation_count("refund"), 1)

    def test_t17_t22_independent_late_response_reconciles_without_new_authorization(self):
        request = self.request("late-purchase", scenario="online_required")
        timeout_host = MockAuthorizationHost(self.root / "host-v1.sqlite3", timeout_operations={request.operation_id})
        app = self.make_app(host=timeout_host, scenario="online_required")
        uncertain = self.execute(app, request)
        self.assertEqual(uncertain["execution_state"], "UNCERTAIN")
        self.assertEqual(uncertain["kernel_adapter"], "InternalEmvKernel")
        self.assertEqual(app.reader.instructions, CONTACT_OFFLINE)
        self.assertEqual(app.reader.instructions.count(0xAE), 1)
        initial = timeout_host.query(request.operation_id)
        late = timeout_host.publish_late_result(request.operation_id, "APPROVED")
        self.assertEqual(late["host_reference"], initial["host_reference"])
        self.assertEqual(late["timestamp"], initial["timestamp"])
        with self.assertRaises(HostError) as duplicate:
            timeout_host.publish_late_result(request.operation_id, "DECLINED")
        self.assertEqual(duplicate.exception.code, "LATE_RESULT_ALREADY_EXISTS")

        restarted_kernel = InternalEmvKernel()
        restarted_host = MockAuthorizationHost(self.root / "host-v1.sqlite3")
        restarted = self.make_app(kernel=restarted_kernel, host=restarted_host, scenario="online_required")
        reconciled = restarted.query(request.operation_id)
        self.assertEqual(reconciled["execution_state"], "SUCCEEDED")
        self.assertEqual(reconciled["functional_result"], "APPROVED")
        self.assertEqual(reconciled["details"]["host_reference"], initial["host_reference"])
        self.assertEqual(reconciled["details"]["host_response_timestamp"], initial["timestamp"])
        self.assertEqual(reconciled["details"]["late_response_timestamp"], late["late_response_timestamp"])
        self.assertEqual(restarted_kernel.execution_count(request.operation_id), 0)
        self.assertEqual(restarted.reader.instructions, [])
        self.assertEqual(restarted_host.operation_count("authorize"), 1)
        events = [entry["event_type"] for entry in restarted.audit(request.operation_id)]
        self.assertEqual(events.count("HOST_AUTHORIZATION_STARTED"), 1)
        self.assertNotIn("ONLINE_COMPLETED", events[events.index("HOST_QUERY_COMPLETED"):])

    def test_t13_refund_timeout_reconciles_same_host_reference(self):
        original = self.purchase()
        request = self.request(
            "late-refund",
            amount=400,
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=original["operation_id"],
            original_amount=Money(1000, "MXN", 2),
        )
        timeout_host = MockAuthorizationHost(self.root / "host-v1.sqlite3", timeout_operations={request.operation_id})
        app = self.make_app(host=timeout_host)
        uncertain = self.execute(app, request)
        self.assertEqual(uncertain["execution_state"], "UNCERTAIN")
        self.assertEqual(uncertain["details"]["host_operation"], "refund")
        self.assertEqual(app.reader.instructions, CONTACT_OFFLINE)
        self.assertEqual(app.reader.instructions.count(0xAE), 1)
        initial = timeout_host.query(request.operation_id)
        late = timeout_host.publish_late_result(request.operation_id, "APPROVED")
        restarted = self.make_app(kernel=InternalEmvKernel(), host=MockAuthorizationHost(self.root / "host-v1.sqlite3"))
        final = restarted.query(request.operation_id)
        self.assertEqual(final["execution_state"], "SUCCEEDED")
        self.assertEqual(final["details"]["host_reference"], initial["host_reference"])
        self.assertEqual(final["details"]["host_response_timestamp"], late["timestamp"])
        self.assertEqual(final["details"]["original_host_reference"], original["details"]["host_reference"])
        self.assertEqual(restarted.host.operation_count("refund"), 1)
        self.assertEqual(restarted.reader.instructions, [])
        self.assertEqual(restarted.service.kernels[self.kernel.kernel_ref].execution_count(request.operation_id), 0)

    def test_t14_reversal_timeout_query_and_late_outcome(self):
        original = self.purchase()
        request = self.request(
            "late-reversal",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original["operation_id"],
            original_amount=Money(1000, "MXN", 2),
        )
        timeout_host = MockAuthorizationHost(self.root / "host-v1.sqlite3", timeout_operations={request.operation_id})
        app = self.make_app(host=timeout_host)
        uncertain = self.execute(app, request)
        self.assertEqual(uncertain["execution_state"], "UNCERTAIN")
        self.assertEqual(uncertain["details"]["host_operation"], "reverse")
        self.assertEqual(app.reader.instructions, CONTACT_OFFLINE)
        self.assertEqual(app.reader.instructions.count(0xAE), 1)
        initial = timeout_host.query(request.operation_id)
        late = timeout_host.publish_late_result(request.operation_id, "APPROVED")
        restarted = self.make_app(kernel=InternalEmvKernel(), host=MockAuthorizationHost(self.root / "host-v1.sqlite3"))
        final = restarted.query(request.operation_id)
        self.assertEqual(final["execution_state"], "SUCCEEDED")
        self.assertEqual(final["details"]["host_reference"], initial["host_reference"])
        self.assertEqual(final["details"]["host_response_timestamp"], late["timestamp"])
        self.assertEqual(final["details"]["original_host_reference"], original["details"]["host_reference"])
        self.assertEqual(restarted.host.operation_count("reverse"), 1)
        self.assertEqual(restarted.reader.instructions, [])
        self.assertEqual(restarted.service.kernels[self.kernel.kernel_ref].execution_count(request.operation_id), 0)

    def test_t11_facade_exposes_host_and_issuer_phases_only(self):
        result = self.purchase("phase-purchase")
        phases = self.app.host_phases(result["operation_id"])
        self.assertEqual(
            [item["phase"] for item in phases],
            [
                "HOST_REQUEST_PREPARED",
                "AUTHORIZATION_SENT",
                "AUTHORIZATION_RECEIVED",
                "ISSUER_PROCESSING",
                "ONLINE_COMPLETED",
            ],
        )
        self.assertTrue(all(item["authority"] == "EXTERNAL_UNVERIFIED" for item in phases))
        self.assertNotIn("reader", str(phases).lower())

    def test_t13_host_refund_requires_original_and_enforces_cumulative_limit(self):
        original_request = self.request("host-original", scenario="online_required")
        original = self.host.authorize(original_request)
        first = self.request(
            "host-refund-1",
            amount=700,
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=original_request.operation_id,
            original_amount=Money(1000, "MXN", 2),
        )
        second = self.request(
            "host-refund-2",
            amount=400,
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=original_request.operation_id,
            original_amount=Money(1000, "MXN", 2),
        )
        approved = self.host.refund(first)
        self.assertEqual(approved["original_host_reference"], original["host_reference"])
        with self.assertRaises(HostError) as excessive:
            self.host.refund(second)
        self.assertEqual(excessive.exception.code, "EXCESSIVE_REFUND")
        orphan = self.request(
            "host-refund-orphan",
            transaction_type=TransactionType.REFUND.value,
            original_operation_id="missing-original",
            original_amount=Money(1000, "MXN", 2),
        )
        with self.assertRaises(HostError) as missing:
            self.host.refund(orphan)
        self.assertEqual(missing.exception.code, "HOST_ORIGINAL_OPERATION_NOT_FOUND")

        timeout_host = MockAuthorizationHost(
            self.root / "host-v1.sqlite3", timeout_operations={"host-original-uncertain"},
        )
        uncertain_original = self.request("host-original-uncertain", scenario="online_required")
        timeout_host.authorize(uncertain_original)
        blocked = self.request(
            "host-refund-uncertain-original",
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=uncertain_original.operation_id,
            original_amount=Money(1000, "MXN", 2),
        )
        with self.assertRaises(HostError) as not_approved:
            timeout_host.refund(blocked)
        self.assertEqual(not_approved.exception.code, "HOST_ORIGINAL_NOT_APPROVED")


if __name__ == "__main__":
    unittest.main()
