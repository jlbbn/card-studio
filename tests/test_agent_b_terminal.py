import contextlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from card_studio.application import CardStudioApplication, laboratory_profile
from card_studio.cli import main as cli_main
from card_studio.config import AppSettings, SettingsStore
from card_studio.config_migration import ConfigMigration, ConfigMigrationError
from card_studio.host.base import HostError
from card_studio.host.mock import MockAuthorizationHost
from card_studio.l3.scenarios import LabKernelAdapter
from card_studio.money import Money
from card_studio.profiles.model import ProfileStatus
from card_studio.readers.base import CapabilityState
from card_studio.readers.mock import MockReaderAdapter
from card_studio.transactions.errors import SimulatedCrash
from card_studio.transactions.model import ExecutionState, TransactionError, TransactionRequest, TransactionResult, TransactionType
from card_studio.transactions.recovery import RecoveryClass, RecoveryError
from card_studio.transactions.store import PersistentState, canonical_request_digest
from card_studio.transactions.view import readiness_view, transaction_view


class TerminalTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = LabKernelAdapter()
        self.profile = laboratory_profile(self.kernel.kernel_ref)
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"
        self.app = self.make_app()

    def tearDown(self):
        self.temporary.cleanup()

    def make_app(self, **overrides):
        values = {
            "data_dir": self.root,
            "config_dir": self.root / "config",
            "profiles": [self.profile],
            "kernels": self.kernel,
        }
        values.update(overrides)
        return CardStudioApplication(**values)

    def request(self, operation_id="op-b-1", scenario="approved", amount=1000, **kwargs):
        return TransactionRequest.create(
            money=Money(amount, "MXN", 2),
            profile_ref=self.profile_ref,
            interface=kwargs.pop("interface", "VIRTUAL"),
            kernel_ref=self.kernel.kernel_ref,
            scenario=scenario,
            operation_id=operation_id,
            idempotency_key=kwargs.pop("idempotency_key", f"key-{operation_id}"),
            **kwargs,
        )

    def purchase(self, operation_id="op-original", amount=1000):
        return self.app.create_transaction(self.request(operation_id, amount=amount))

    def test_persistence_fields_audit_and_private_evidence(self):
        result = self.purchase()
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["functional_result"], "APPROVED")
        self.assertEqual(result["profile_digest"], self.profile.profile_digest)
        self.assertEqual(result["kernel_adapter"], "LabKernelAdapter")
        self.assertEqual(result["kernel_mode"], "LAB / SYNTHETIC")
        self.assertTrue(result["simulation"])
        self.assertEqual(result["physical_validation"], "NOT_EXECUTED")
        events = {entry["event_type"] for entry in self.app.audit(result["operation_id"])}
        self.assertTrue({
            "TRANSACTION_CREATED", "SESSION_OPENED", "KERNEL_STARTED", "KERNEL_COMPLETED",
            "TRANSACTION_SUCCEEDED", "KERNEL_CLOSED", "INTERFACE_CLOSED",
        } <= events)
        evidence = self.root / "evidence" / "transactions" / result["result_reference"]
        self.assertTrue(evidence.exists())
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
        payload = json.loads(evidence.read_text())
        self.assertEqual(payload["physical_validation"], "NOT_EXECUTED")
        self.assertEqual(payload["mode"], "SIMULATED")
        self.assertGreaterEqual(len(list(evidence.parent.glob("op-original-event-*.json"))), 7)
        with contextlib.closing(sqlite3.connect(self.root / "transactions-v1.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_restart_and_idempotent_replay_return_same_result(self):
        first = self.app.create_transaction(self.request())
        self.kernel = LabKernelAdapter()
        restarted = self.make_app()
        replay = restarted.create_transaction(self.request())
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(replay["result_reference"], first["result_reference"])
        self.assertEqual(replay["updated_at"], first["updated_at"])
        self.assertEqual(self.kernel.execution_count("op-b-1"), 0)
        same_canonical = replace(
            self.request(), operation_id="op-new", correlation_id="new-correlation",
        )
        duplicate = restarted.create_transaction(same_canonical)
        self.assertEqual(duplicate["operation_id"], first["operation_id"])

    def test_canonical_digest_and_idempotency_conflict(self):
        request = self.request()
        changed_ids = replace(request, operation_id="other", correlation_id="other")
        self.assertEqual(canonical_request_digest(request), canonical_request_digest(changed_ids))
        self.app.create_transaction(request)
        with self.assertRaises(TransactionError) as caught:
            self.app.create_transaction(replace(request, operation_id="op-conflict", money=Money(1001, "MXN", 2)))
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(TransactionError) as duplicate:
            self.app.create_transaction(replace(request, idempotency_key="another-key"))
        self.assertEqual(duplicate.exception.code, "DUPLICATE_OPERATION")

    def test_concurrent_duplicate_executes_only_once(self):
        request = self.request("op-concurrent")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.app.create_transaction(request), range(8)))
        self.assertEqual({item["operation_id"] for item in results}, {"op-concurrent"})
        self.assertTrue(all(item["execution_state"] == "SUCCEEDED" for item in results))
        self.assertEqual(self.kernel.execution_count("op-concurrent"), 1)
        self.assertEqual(len(self.app.list_transactions()), 1)

    def test_audit_triggers_reject_update_and_delete(self):
        self.purchase()
        with contextlib.closing(sqlite3.connect(self.root / "transactions-v1.sqlite3")) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_trail SET reason='CHANGED' WHERE sequence=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_trail WHERE sequence=1")

    def test_timeout_is_uncertain_and_explicit_query_recovers(self):
        request = self.request("op-timeout", scenario="timeout")
        result = self.app.create_transaction(request)
        self.assertEqual(result["execution_state"], "UNCERTAIN")
        self.assertEqual(result["recovery_action"], "QUERY_REQUIRED")
        self.assertNotEqual(result["functional_result"], "DECLINED")
        recovered = self.app.query(request.operation_id)
        self.assertEqual(recovered["execution_state"], "SUCCEEDED")
        self.assertEqual(recovered["functional_result"], "APPROVED")
        self.assertEqual(self.kernel.execution_count(request.operation_id), 1)

    def test_restart_unknown_query_conserves_uncertain(self):
        request = self.request("op-restart-uncertain", scenario="timeout")
        self.app.create_transaction(request)
        self.kernel = LabKernelAdapter()
        restarted = self.make_app()
        result = restarted.query(request.operation_id)
        self.assertEqual(result["execution_state"], "UNCERTAIN")
        self.assertEqual(result["recovery_action"], "QUERY_REQUIRED")
        self.assertEqual(self.kernel.execution_count(request.operation_id), 0)

    def test_crash_in_progress_requires_explicit_readback_recovery(self):
        def crash(_operation_id):
            raise SimulatedCrash("crash")

        crashing = self.make_app(hooks={"after_in_progress": crash})
        request = self.request("op-crash")
        with self.assertRaises(SimulatedCrash):
            crashing.create_transaction(request)
        self.kernel = LabKernelAdapter()
        restarted = self.make_app()
        self.assertEqual(restarted.get(request.operation_id)["execution_state"], "IN_PROGRESS")
        self.assertEqual(len(restarted.list_pending()), 1)
        recovered = restarted.recover(request.operation_id)
        self.assertEqual(recovered["execution_state"], "UNCERTAIN")
        self.assertEqual(recovered["recovery_action"], "READ_BACK_REQUIRED")
        self.assertEqual(self.kernel.execution_count(request.operation_id), 0)
        self.assertEqual(restarted.query(request.operation_id)["recovery_action"], "READ_BACK_REQUIRED")
        events = [entry["event_type"] for entry in restarted.audit(request.operation_id)]
        self.assertIn("RECOVERY_STARTED", events)
        self.assertIn("RECOVERY_COMPLETED", events)

    def test_retry_only_failed_safe_to_retry(self):
        request = self.request("op-safe-retry", scenario="failure_before_send")
        failed = self.app.create_transaction(request)
        self.assertEqual(failed["execution_state"], "FAILED")
        self.assertEqual(failed["recovery_action"], "SAFE_TO_RETRY")
        recovered = self.app.retry_transaction(request)
        self.assertEqual(recovered["execution_state"], "SUCCEEDED")
        self.assertEqual(self.kernel.execution_count(request.operation_id), 2)
        unsafe = self.request("op-unsafe", scenario="declined")
        self.app.create_transaction(unsafe)
        with self.assertRaises(RecoveryError):
            self.app.retry_transaction(unsafe)
        uncertain = self.request("op-uncertain-retry", scenario="timeout")
        self.app.create_transaction(uncertain)
        with self.assertRaises(RecoveryError):
            self.app.retry_transaction(uncertain)
        self.assertEqual(self.kernel.execution_count(uncertain.operation_id), 1)

    def test_cancel_created_and_cancellation_scenario(self):
        def crash(_operation_id):
            raise SimulatedCrash("crash")

        crashing = self.make_app(hooks={"after_created": crash})
        request = self.request("op-cancel-created")
        with self.assertRaises(SimulatedCrash):
            crashing.create_transaction(request)
        cancelled = self.app.cancel(request.operation_id)
        self.assertEqual(cancelled["execution_state"], "CANCELLED")
        scenario = self.app.create_transaction(self.request("op-cancel-scenario", scenario="cancellation"))
        self.assertEqual(scenario["execution_state"], "CANCELLED")

    def test_partial_refund_limits_and_original_references(self):
        original = self.purchase(amount=1000)
        first = self.request(
            "op-refund-1", amount=400, transaction_type=TransactionType.REFUND.value,
            original_operation_id=original["operation_id"], original_amount=Money(1000, "MXN", 2),
        )
        second = self.request(
            "op-refund-2", amount=600, transaction_type=TransactionType.REFUND.value,
            original_operation_id=original["operation_id"], original_amount=Money(1000, "MXN", 2),
        )
        self.assertEqual(self.app.create_transaction(first)["execution_state"], "SUCCEEDED")
        self.assertEqual(self.app.create_transaction(second)["execution_state"], "SUCCEEDED")
        excessive = self.request(
            "op-refund-excessive", amount=1, transaction_type=TransactionType.REFUND.value,
            original_operation_id=original["operation_id"], original_amount=Money(1000, "MXN", 2),
        )
        result = self.app.create_transaction(excessive)
        self.assertEqual(result["error_code"], "EXCESSIVE_REFUND")
        self.assertEqual(self.kernel.execution_count(excessive.operation_id), 0)
        self.assertEqual(result["original_operation_id"], original["operation_id"])
        self.assertEqual(result["original_amount"]["amount_minor"], 1000)

    def test_reversal_and_duplicate_reversal(self):
        original = self.purchase()
        kwargs = {
            "transaction_type": TransactionType.REVERSAL.value,
            "original_operation_id": original["operation_id"],
            "original_amount": Money(1000, "MXN", 2),
        }
        reversal = self.app.create_transaction(self.request("op-reversal-1", **kwargs))
        self.assertEqual(reversal["execution_state"], "SUCCEEDED")
        duplicate = self.app.create_transaction(self.request("op-reversal-2", **kwargs))
        self.assertEqual(duplicate["error_code"], "DUPLICATE_REVERSAL")
        self.assertEqual(self.kernel.execution_count("op-reversal-2"), 0)
        events = {entry["event_type"] for entry in self.app.audit("op-reversal-1")}
        self.assertIn("REVERSAL_STARTED", events)
        self.assertIn("REVERSAL_COMPLETED", events)

    def test_host_idempotency_restart_conflict_timeout_and_safe_retry(self):
        request = self.request("op-host", scenario="online_required")
        host = MockAuthorizationHost(self.root / "host-v1.sqlite3", unavailable_operations={request.operation_id})
        app = self.make_app(host=host)
        failed = app.create_transaction(request)
        self.assertEqual(failed["error_code"], "HOST_UNAVAILABLE_BEFORE_SEND")
        self.assertEqual(failed["recovery_action"], "SAFE_TO_RETRY")
        replay = MockAuthorizationHost(self.root / "host-v1.sqlite3").authorize(request)
        self.assertEqual(replay["error_code"], "HOST_UNAVAILABLE_BEFORE_SEND")
        host.unavailable_operations.clear()
        replayed_retry = app.retry_transaction(request)
        self.assertEqual(replayed_retry["execution_state"], "FAILED")
        self.assertEqual(replayed_retry["recovery_action"], "SAFE_TO_RETRY")
        with self.assertRaises(HostError) as conflict:
            host.authorize(replace(request, money=Money(2000, "MXN", 2)))
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

        timeout_request = self.request("op-host-timeout", scenario="online_required")
        timeout_host = MockAuthorizationHost(self.root / "host-v1.sqlite3", timeout_operations={timeout_request.operation_id})
        timeout_app = self.make_app(host=timeout_host)
        uncertain = timeout_app.create_transaction(timeout_request)
        self.assertEqual(uncertain["execution_state"], "UNCERTAIN")
        self.assertEqual(uncertain["recovery_action"], "QUERY_REQUIRED")
        persisted = MockAuthorizationHost(self.root / "host-v1.sqlite3").query(timeout_request.operation_id)
        self.assertEqual(persisted["error_code"], "HOST_TIMEOUT_AFTER_SEND")
        self.assertEqual(persisted["mode"], "SIMULATED")

    def test_colon_operation_evidence_and_host_cancellation_rules(self):
        result = self.app.create_transaction(self.request("op:colon"))
        self.assertNotIn(":", result["result_reference"])
        self.assertTrue((self.root / "evidence" / "transactions" / result["result_reference"]).exists())
        events = {entry["event_type"] for entry in self.app.audit("op:colon")}
        self.assertIn("APPLICATION_SELECTED", events)

        host = MockAuthorizationHost(self.root / "cancel-host.sqlite3", unavailable_operations={"op-host-cancel"})
        request = self.request("op-host-cancel", scenario="online_required")
        host.authorize(request)
        cancelled = host.cancel(request.operation_id)
        self.assertEqual(cancelled["execution_state"], "CANCELLED")
        restarted = MockAuthorizationHost(self.root / "cancel-host.sqlite3")
        self.assertEqual(restarted.query(request.operation_id)["execution_state"], "CANCELLED")
        sent = self.request("op-host-sent")
        host.authorize(sent)
        not_cancelled = host.cancel(sent.operation_id)
        self.assertEqual(not_cancelled["execution_state"], "SUCCEEDED")
        self.assertEqual(not_cancelled["cancellation_status"], "NOT_CANCELLED_ALREADY_SENT")

    def test_evidence_redaction_excludes_sensitive_details(self):
        class SensitiveKernel(LabKernelAdapter):
            def execute(self, session, request, deadline=None):
                result = super().execute(session, request, deadline)
                details = dict(result.details)
                details.update({"pan": "4111111111111111", "pin": "1234", "raw_apdu": "00A4040000", "safe": "ok"})
                return TransactionResult(result.request, result.execution_state, result.outcome, result.recovery, result.error_code, details)

        self.kernel = SensitiveKernel()
        app = self.make_app()
        result = app.create_transaction(self.request("op-redaction"))
        serialized = json.dumps(result)
        evidence = (self.root / "evidence" / "transactions" / result["result_reference"]).read_text()
        database = (self.root / "transactions-v1.sqlite3").read_bytes()
        self.assertNotIn("4111111111111111", serialized + evidence)
        self.assertNotIn("00A4040000", serialized + evidence)
        self.assertNotIn(b"4111111111111111", database)
        self.assertNotIn(b"00A4040000", database)
        self.assertNotIn("safe", result["details"])

    def test_facade_readiness_and_view_model_actions(self):
        approved = self.purchase()
        public = self.app.get(approved["operation_id"])
        self.assertNotIn("request", public)
        self.assertNotIn("session", public)
        self.assertNotIn("raw_apdu", public)
        self.assertEqual(transaction_view(public)["status"], "LAB — APPROVED")
        self.assertEqual(transaction_view(public)["actions"], [])
        timeout = self.app.create_transaction(self.request("op-view-timeout", scenario="timeout"))
        self.assertEqual(transaction_view(timeout)["actions"], ["query", "review"])
        safe = self.app.create_transaction(self.request("op-view-retry", scenario="failure_before_send"))
        self.assertEqual(transaction_view(safe)["actions"], ["retry"])
        readiness = readiness_view(self.app.l3_readiness())
        self.assertEqual(readiness["certification"], "NOT COMPLETED")
        self.assertEqual(readiness["physical_validation"], "PENDING")
        self.assertEqual(readiness["lab_readiness"], "NOT_RUN")
        kernel = self.app.kernel_readiness()["kernels"][0]
        self.assertEqual(kernel["kernel_ref"], self.kernel.kernel_ref)
        self.assertTrue(kernel["ready"])

    def test_reader_unknown_and_unapproved_profile_block_before_create(self):
        reader = MockReaderAdapter(interface_states={"VIRTUAL": CapabilityState.UNKNOWN})
        app = self.make_app(reader=reader)
        with self.assertRaises(TransactionError) as caught:
            app.create_transaction(self.request("op-unknown"))
        self.assertEqual(caught.exception.code, "CAPABILITY_UNKNOWN")
        self.assertEqual(app.list_transactions(), [])
        value = self.profile.to_dict()
        value["status"] = ProfileStatus.DRAFT.value
        value["profile_digest"] = ""
        from card_studio.profiles.model import CardProfile
        draft = CardProfile.from_dict(value)
        draft_ref = f"{draft.profile_id}:{draft.profile_version}:{draft.profile_digest}"
        draft_app = self.make_app(profiles=[draft])
        request = replace(self.request("op-draft"), profile_ref=draft_ref)
        with self.assertRaises(TransactionError) as rejected:
            draft_app.create_transaction(request)
        self.assertEqual(rejected.exception.code, "PROFILE_NOT_EXECUTABLE")
        self.assertEqual(draft_app.list_transactions(), [])

    def test_config_migration_twice_rollback_and_legacy_v2(self):
        config = self.root / "migration"
        legacy = SettingsStore(config)
        legacy.save(AppSettings(terminal_code="LAB-ONLY"))
        original = legacy.path.read_bytes()
        migration = ConfigMigration(config)
        first = migration.migrate()
        first_bytes = migration.path.read_bytes()
        second = migration.migrate()
        self.assertEqual(first, second)
        self.assertEqual(migration.path.read_bytes(), first_bytes)
        self.assertEqual(legacy.path.read_bytes(), original)
        self.assertEqual(first["profiles"], [])
        self.assertTrue(first["simulation"]["required"])
        self.assertEqual(stat.S_IMODE(migration.path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(legacy.path.read_text())["version"], 2)
        self.assertTrue(migration.rollback())
        self.assertFalse(migration.path.exists())
        self.assertEqual(legacy.path.read_bytes(), original)

    def test_config_migration_rejects_unknown_version_without_writing(self):
        config = self.root / "bad-migration"
        config.mkdir()
        (config / "settings.json").write_text('{"version": 9}')
        with self.assertRaises(ConfigMigrationError) as caught:
            ConfigMigration(config).migrate()
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CONFIG_VERSION")
        self.assertFalse((config / "settings-v3.json").exists())

    def test_cli_facade_create_get_query_pending_cancel_and_readiness(self):
        def invoke(arguments):
            output = io.StringIO()
            with patch("card_studio.cli.TransactionApplication", return_value=self.app), contextlib.redirect_stdout(output):
                code = cli_main(arguments)
            return code, json.loads(output.getvalue())

        code, created = invoke([
            "--data-dir", str(self.root), "transaction", "create", "--amount-minor", "1000",
            "--operation-id", "op-cli", "--idempotency-key", "key-cli", "--kernel-ref", self.kernel.kernel_ref,
            "--profile-ref", self.profile_ref, "--scenario", "timeout",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(created["execution_state"], "UNCERTAIN")
        self.assertEqual(invoke(["transaction", "get", "op-cli"])[1]["operation_id"], "op-cli")
        self.assertEqual(len(invoke(["transaction", "pending"])[1]["transactions"]), 1)
        self.assertEqual(invoke(["transaction", "query", "op-cli"])[1]["execution_state"], "SUCCEEDED")
        self.assertEqual(invoke(["transaction", "cancel", "op-cli"])[1]["execution_state"], "SUCCEEDED")
        self.assertEqual(invoke(["kernel", "readiness"])[1]["mode"], "LAB / SYNTHETIC")
        l3 = invoke(["l3", "readiness"])[1]
        self.assertEqual(l3["certification"], "NOT COMPLETED")
        self.assertEqual(l3["physical_validation"], "PENDING")
        self.assertEqual(invoke(["transaction", "list"])[1]["transactions"][0]["simulation"], True)


if __name__ == "__main__":
    unittest.main()
