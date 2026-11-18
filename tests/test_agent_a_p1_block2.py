from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from card_studio.emv import synthetic_generate_ac_response
from card_studio.emv.online import synthetic_host_response
from card_studio.emv.velocity import DurableVelocityCounters, VelocityError, VelocityPolicy
from card_studio.interfaces import ContactInterface
from card_studio.kernels import InternalEmvKernel
from card_studio.kernels.control import (
    CancellationToken,
    SessionControlError,
    SessionExecutionControl,
)
from card_studio.emv.trace import SanitizedTrace
from card_studio.profiles import CardProfile
from card_studio.readers.base import ReaderError
from card_studio.transactions import ExecutionState, RecoveryClass
from tests.test_agent_a_kernel import make_request
from tests.test_agent_a_p0_block6 import RecordingReader, case_fixture


def open_fixture(fixture, kernel, reader_id="p1-block2-reader"):
    adapter = fixture["reader"]
    reader_session = adapter.open(adapter.reader_info.reader_id, reader_id)
    card_interface = ContactInterface(adapter, reader_session).open()
    session = kernel.open_session(fixture["request"], card_interface, fixture["profile"])
    return session


def with_velocity(fixture, upper=1):
    raw = fixture["profile"].to_dict()
    parameters = dict(raw["scheme_parameters"])
    execution = dict(parameters["emv_execution"])
    decision = dict(execution["decision"])
    risk = dict(decision.get("risk", {}))
    risk["velocity"] = {
        "lower_offline_limit": 0,
        "upper_offline_limit": upper,
        "maximum_cumulative_amount_minor": 0,
        "lower_selection_percent": 100,
    }
    decision["risk"] = risk
    execution["decision"] = decision
    parameters["emv_execution"] = execution
    raw["scheme_parameters"] = parameters
    raw["profile_digest"] = ""
    fixture["profile"] = CardProfile.from_dict(raw)
    fixture["request"] = make_request(
        fixture["profile"],
        operation_id=fixture["request"].operation_id,
        scenario=fixture["request"].scenario,
        interface="CONTACT",
        application_aid=fixture["request"].application_aid,
    )
    return fixture


class K27DurableVelocityTests(unittest.TestCase):
    def test_lower_upper_cumulative_and_restart_are_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "velocity.sqlite3"
            counters = DurableVelocityCounters(path)
            policy = VelocityPolicy(lower_offline_limit=1, upper_offline_limit=3, maximum_cumulative_amount_minor=250)
            first = counters.reserve("op-v1", "profile:MXN", 100, policy)
            self.assertEqual(first.reasons, ())
            counters.complete(first.operation_id, "OFFLINE_APPROVED")
            second = counters.reserve("op-v2", "profile:MXN", 100, policy)
            self.assertIn("LOWER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED", second.reasons)
            with self.assertRaises(VelocityError) as caught:
                counters.complete(second.operation_id, "OFFLINE_APPROVED")
            self.assertEqual(caught.exception.code, "VELOCITY_OFFLINE_OUTCOME_BLOCKED")
            counters.complete(second.operation_id, "ONLINE_COMPLETED")
            self.assertEqual(counters.snapshot("profile:MXN")["offline_count"], 0)

            third = counters.reserve("op-v3", "profile:MXN", 100, policy)
            self.assertIn("VELOCITY_AMOUNT_LIMIT_EXCEEDED", third.reasons)
            restarted = DurableVelocityCounters(path)
            self.assertEqual(restarted.reserve("op-v3", "profile:MXN", 100, policy), third)
            self.assertEqual(restarted.snapshot("profile:MXN")["pending_count"], 1)
            with self.assertRaises(VelocityError):
                restarted.reserve("op-v3", "profile:MXN", 101, policy)

    def test_aborted_pre_send_retry_does_not_duplicate_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "velocity.sqlite3"
            counters = DurableVelocityCounters(path)
            policy = VelocityPolicy(upper_offline_limit=2)
            counters.reserve("op-retry", "scope", 100, policy)
            counters.complete("op-retry", "ABORTED_BEFORE_SEND")
            self.assertEqual(counters.snapshot("scope"), {"offline_count": 0, "pending_count": 0, "cumulative_amount": 0})
            restarted = DurableVelocityCounters(path)
            reservation = restarted.reserve("op-retry", "scope", 100, policy)
            self.assertEqual(reservation.status, "RESERVED")
            self.assertEqual(restarted.snapshot("scope")["cumulative_amount"], 100)
            restarted.complete("op-retry", "OFFLINE_APPROVED")
            self.assertEqual(restarted.snapshot("scope")["offline_count"], 1)

    def test_competing_reservations_cannot_both_bypass_upper_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            counters = DurableVelocityCounters(Path(temporary) / "velocity.sqlite3")
            policy = VelocityPolicy(upper_offline_limit=1)
            barrier = threading.Barrier(3)
            decisions = []
            errors = []

            def reserve(operation_id):
                try:
                    barrier.wait()
                    decisions.append(counters.reserve(operation_id, "shared", 10, policy))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reserve, args=(f"op-concurrent-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(sum(not decision.online_required for decision in decisions), 1)
            self.assertEqual(sum(decision.online_required for decision in decisions), 1)
            self.assertEqual(counters.snapshot("shared")["pending_count"], 2)

    def test_real_kernel_commits_offline_then_upper_limit_completes_online(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = DurableVelocityCounters(Path(temporary) / "velocity.sqlite3")
            kernel = InternalEmvKernel(velocity_store=store)
            first = with_velocity(case_fixture("CONTACT"))
            first["request"] = make_request(
                first["profile"], operation_id="op-kernel-v1", interface="CONTACT", application_aid=first["request"].application_aid
            )
            first_session = open_fixture(first, kernel, "velocity-reader-1")
            first_result = kernel.execute(first_session, first["request"])
            self.assertEqual(first_result.outcome, "APPROVED")
            self.assertEqual(first_result.details["risk_result"]["velocity"]["status"], "OFFLINE_APPROVED")

            second = with_velocity(case_fixture("CONTACT"))
            second["request"] = make_request(
                second["profile"], operation_id="op-kernel-v2", interface="CONTACT", application_aid=second["request"].application_aid
            )

            class ArqcReader(RecordingReader):
                def transmit(self, connection, command, sensitivity="REDACTED"):
                    raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
                    if raw[1] == 0xAE and self.transmitted.count(raw) == 0 and not any(item[1] == 0xAE for item in self.transmitted):
                        self.transmitted.append(raw)
                        return synthetic_generate_ac_response(0x80, 77, "velocity-second")
                    return super().transmit(connection, command, sensitivity)

            second["reader"] = ArqcReader(second["responses"])
            second_session = open_fixture(second, kernel, "velocity-reader-2")
            pending = kernel.execute(second_session, second["request"])
            self.assertEqual(pending.outcome, "ONLINE_REQUIRED")
            self.assertIn("UPPER_CONSECUTIVE_OFFLINE_LIMIT_EXCEEDED", pending.details["risk_result"]["reasons"])
            completed = kernel.complete_online(second_session, second["request"], synthetic_host_response())
            self.assertEqual(completed.outcome, "APPROVED")
            self.assertEqual(completed.details["risk_result"]["velocity"]["status"], "ONLINE_COMPLETED")
            self.assertEqual(store.snapshot(f"{second['profile'].profile_digest}:MXN")["offline_count"], 0)


class K30K31K32K36ControlTests(unittest.TestCase):
    def test_fake_clock_distinguishes_stage_timeout_before_and_after_send(self):
        now = [10.0]
        token = CancellationToken()
        control = SessionExecutionControl(
            SanitizedTrace("op-clock"),
            token,
            {"APPLICATION_SELECTION": 5},
            clock=lambda: now[0],
        )
        control.enter_stage("APPLICATION_SELECTION")
        now[0] += 0.006
        with self.assertRaises(SessionControlError) as caught:
            control.before_exchange()
        self.assertEqual(caught.exception.code, "TIMEOUT_BEFORE_SEND")
        self.assertEqual(caught.exception.outcome, "CERTAIN_FAILURE")

        now[0] = 20.0
        after = SessionExecutionControl(
            SanitizedTrace("op-clock-after"),
            CancellationToken(),
            {"APPLICATION_SELECTION": 5},
            clock=lambda: now[0],
        )
        after.enter_stage("APPLICATION_SELECTION")
        after.before_exchange()
        after.sent()
        now[0] += 0.006
        with self.assertRaises(SessionControlError) as caught_after:
            after.after_exchange()
        self.assertEqual(caught_after.exception.code, "TIMEOUT_AFTER_SEND")
        self.assertEqual(caught_after.exception.outcome, "UNCERTAIN")

    def test_post_send_timeout_at_every_contact_apdu_is_uncertain_without_replay(self):
        for fail_at in range(1, 6):
            with self.subTest(fail_at=fail_at):
                fixture = case_fixture("CONTACT")
                original = fixture["reader"]

                class FailAtReader(RecordingReader):
                    def transmit(self, connection, command, sensitivity="REDACTED"):
                        if len(self.transmitted) + 1 == fail_at:
                            raw = bytes.fromhex(command) if isinstance(command, str) else bytes(command)
                            self.transmitted.append(raw)
                            raise ReaderError("TRANSMIT_TIMEOUT", "LAB post-send timeout", outcome="UNCERTAIN")
                        return super().transmit(connection, command, sensitivity)

                fixture["reader"] = FailAtReader(original.responses)
                kernel = InternalEmvKernel()
                session = open_fixture(fixture, kernel, f"timeout-reader-{fail_at}")
                result = kernel.execute(session, fixture["request"])
                self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
                self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
                self.assertIsNone(result.outcome)
                transmitted = list(fixture["reader"].transmitted)
                self.assertEqual(len(transmitted), fail_at)
                self.assertEqual(kernel.execute(session, fixture["request"]), result)
                self.assertEqual(fixture["reader"].transmitted, transmitted)
                events = result.details["sanitized_trace"]["events"]
                self.assertTrue(any(item.get("error_code") == "TRANSMIT_TIMEOUT" for item in events))

    def test_real_stage_timeout_after_response_is_uncertain(self):
        fixture = case_fixture("CONTACT")
        raw = fixture["profile"].to_dict()
        parameters = dict(raw["scheme_parameters"])
        parameters["execution_control"] = {"stage_timeouts_ms": {"APPLICATION_SELECTION": 1}}
        raw["scheme_parameters"] = parameters
        raw["profile_digest"] = ""
        fixture["profile"] = CardProfile.from_dict(raw)
        fixture["request"] = make_request(
            fixture["profile"], operation_id="op-stage-timeout", interface="CONTACT", application_aid=fixture["request"].application_aid
        )
        original = fixture["reader"]

        class SlowReader(RecordingReader):
            def transmit(self, connection, command, sensitivity="REDACTED"):
                time.sleep(0.005)
                return super().transmit(connection, command, sensitivity)

        fixture["reader"] = SlowReader(original.responses)
        kernel = InternalEmvKernel()
        session = open_fixture(fixture, kernel, "slow-stage-reader")
        result = kernel.execute(session, fixture["request"])
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.error_code, "TIMEOUT_AFTER_SEND")
        self.assertEqual(len(fixture["reader"].transmitted), 1)

    def test_cooperative_cancel_during_blocked_exchange_is_uncertain_and_idempotent(self):
        fixture = case_fixture("CONTACT")
        original = fixture["reader"]
        entered = threading.Event()
        release = threading.Event()

        class BlockingReader(RecordingReader):
            def transmit(self, connection, command, sensitivity="REDACTED"):
                entered.set()
                self.assert_release(release)
                return super().transmit(connection, command, sensitivity)

            @staticmethod
            def assert_release(event):
                if not event.wait(2):
                    raise RuntimeError("LAB blocking reader no fue liberado")

        fixture["reader"] = BlockingReader(original.responses)
        kernel = InternalEmvKernel()
        session = open_fixture(fixture, kernel, "blocking-cancel-reader")
        results = []
        cancel_results = []
        worker = threading.Thread(target=lambda: results.append(kernel.execute(session, fixture["request"])))
        worker.start()
        self.assertTrue(entered.wait(1))
        canceller = threading.Thread(target=lambda: cancel_results.append(kernel.cancel(session)))
        canceller.start()
        time.sleep(0.01)
        release.set()
        worker.join(2)
        canceller.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(results[0].error_code, "CANCELLED_AFTER_SEND")
        self.assertEqual(cancel_results, results)
        self.assertEqual(len(fixture["reader"].transmitted), 1)
        self.assertEqual(kernel.execute(session, fixture["request"]), results[0])
        self.assertEqual(len(fixture["reader"].transmitted), 1)

    def test_sequenced_trace_is_allowlisted_and_does_not_leak_cryptogram_or_apdu_payloads(self):
        fixture = case_fixture("CONTACT")
        kernel = InternalEmvKernel()
        session = open_fixture(fixture, kernel, "trace-reader")
        result = kernel.execute(session, fixture["request"])
        self.assertEqual(result.outcome, "APPROVED")
        trace = result.details["sanitized_trace"]
        events = trace["events"]
        self.assertEqual([item["sequence"] for item in events], list(range(1, len(events) + 1)))
        self.assertEqual(trace["replay_policy"], "NO_AUTOMATIC_REPLAY")
        self.assertEqual(trace["redaction"], "ALLOWLIST_ONLY")
        self.assertEqual(result.details["session_recovery"]["apdu_session_restorable"], False)
        serialized = json.dumps(trace).lower()
        for command in fixture["reader"].transmitted:
            self.assertNotIn(command.hex().lower(), serialized)
        for response in fixture["responses"].values():
            if response.data:
                self.assertNotIn(response.data.hex().lower(), serialized)
        self.assertFalse(any("data" in item or "value" in item for item in events))

    def test_trace_hashes_operation_and_drops_non_allowlisted_error_text(self):
        from card_studio.readers.base import TransportResult

        pan = "4111111111111111"
        trace = SanitizedTrace(pan)
        command = bytes.fromhex("00A4040000")
        trace.sent("APPLICATION_SELECTION", command)
        trace.response("APPLICATION_SELECTION", command, TransportResult(b"secret", "PAN1"))
        trace.error("APPLICATION_SELECTION", command, "PIN=1234")
        evidence = trace.to_evidence()
        serialized = json.dumps(evidence)
        self.assertNotIn(pan, serialized)
        self.assertNotIn("PIN=1234", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("PAN1", serialized)
        self.assertEqual(len(evidence["operation_sha256"]), 64)
        self.assertEqual(evidence["events"][-1]["error_code"], "CARD_EXCHANGE_FAILED")
        self.assertNotIn("sw1sw2", evidence["events"][1])


if __name__ == "__main__":
    unittest.main()
