import json
import unittest

from card_studio.kernels import KernelAdapter, MockKernelAdapter
from card_studio.money import Money
from card_studio.transactions import (
    ExecutionState,
    RecoveryClass,
    SimulationScenario,
    TransactionError,
    TransactionRequest,
    TransactionResult,
    TransactionSimulator,
    TransactionType,
    recovery_plan,
)
from card_studio.transactions.recovery import RecoveryError


PROFILE_REF = "lab-generic:1:" + "a" * 64


def make_request(
    operation_id="op-1",
    idempotency_key=None,
    amount=1000,
    scenario=SimulationScenario.APPROVED.value,
    transaction_type=TransactionType.PURCHASE.value,
    original_operation_id=None,
    original_amount=None,
):
    return TransactionRequest.create(
        money=Money(amount, "MXN", 2),
        profile_ref=PROFILE_REF,
        interface="VIRTUAL",
        transaction_type=transaction_type,
        scenario=scenario,
        original_operation_id=original_operation_id,
        original_amount=original_amount,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
    )


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.kernel = MockKernelAdapter()
        self.simulator = TransactionSimulator(self.kernel)

    def purchase(self, operation_id="op-original", amount=1000):
        return self.simulator.execute(make_request(operation_id=operation_id, amount=amount))

    def test_kernel_contract_capabilities_session_and_close(self):
        self.assertIsInstance(self.kernel, KernelAdapter)
        capabilities = self.kernel.capabilities()
        self.assertTrue(capabilities.simulation)
        self.assertIn("VIRTUAL", capabilities.interfaces)
        request = make_request()
        session = self.kernel.open_session(request)
        result = self.kernel.execute(session, request)
        self.assertEqual(self.kernel.query(request.operation_id), result)
        self.assertTrue(self.kernel.close(session).closed)
        self.assertTrue(self.kernel.close(session).closed)

    def test_success_contains_complete_operation_and_simulation_flag(self):
        request = make_request()
        result = self.simulator.execute(request)
        self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
        self.assertEqual(result.outcome, "APPROVED")
        self.assertEqual(result.operation_id, "op-1")
        self.assertEqual(result.idempotency_key, request.idempotency_key)
        self.assertEqual(result.correlation_id, request.correlation_id)
        self.assertEqual(result.profile_ref, PROFILE_REF)
        self.assertEqual(result.interface, "VIRTUAL")
        self.assertEqual(result.kernel_ref, "mock-kernel-v1")
        self.assertEqual(result.money, Money(1000, "MXN", 2))
        self.assertTrue(result.simulation)

    def test_decline_is_a_certain_failure(self):
        result = self.simulator.execute(make_request(scenario=SimulationScenario.DECLINED.value))
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertEqual(result.outcome, "DECLINED")
        self.assertEqual(result.error_code, "SIMULATED_DECLINE")

    def test_timeout_after_send_is_uncertain_and_requires_query(self):
        result = self.simulator.execute(make_request(scenario=SimulationScenario.TIMEOUT.value))
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertEqual(result.error_code, "TIMEOUT_AFTER_SEND")
        self.assertEqual(result.recovery, RecoveryClass.QUERY_REQUIRED)
        self.assertTrue(result.details["command_sent"])
        plan = recovery_plan(result)
        self.assertEqual(plan.recovery_class, RecoveryClass.QUERY_REQUIRED)
        self.assertEqual(plan.action, "query(op-1)")
        self.assertFalse(plan.automatic_retry)

    def test_uncertain_result_is_never_converted_to_decline(self):
        result = self.simulator.execute(make_request(scenario=SimulationScenario.UNCERTAIN.value))
        self.assertEqual(result.execution_state, ExecutionState.UNCERTAIN)
        self.assertIsNone(result.outcome)
        self.assertEqual(result.error_code, "UNCERTAIN_RESULT")

    def test_retry_replays_idempotent_result_without_reexecution(self):
        request = make_request(scenario=SimulationScenario.RETRY.value)
        first = self.simulator.execute(request)
        second = self.simulator.retry(request)
        self.assertEqual(second, first)
        self.assertEqual(self.kernel.execution_count(request.operation_id), 1)
        uncertain = self.simulator.execute(make_request(
            operation_id="op-uncertain-retry",
            scenario=SimulationScenario.TIMEOUT.value,
        ))
        self.assertEqual(uncertain.execution_state, ExecutionState.UNCERTAIN)
        with self.assertRaises(RecoveryError) as caught:
            self.simulator.retry(make_request(
                operation_id="op-uncertain-retry",
                scenario=SimulationScenario.TIMEOUT.value,
            ))
        self.assertEqual(caught.exception.code, "QUERY_REQUIRED")
        self.assertEqual(self.kernel.execution_count("op-uncertain-retry"), 1)

    def test_query_returns_original_result_without_reexecution(self):
        request = make_request(scenario=SimulationScenario.TIMEOUT.value)
        result = self.simulator.execute(request)
        queried = self.simulator.query(request.operation_id)
        self.assertEqual(queried, result)
        self.assertEqual(self.kernel.execution_count(request.operation_id), 1)

    def test_cancellation_is_persistent_and_idempotent(self):
        request = make_request()
        result = self.simulator.cancel(request)
        self.assertEqual(result.execution_state, ExecutionState.CANCELLED)
        self.assertEqual(result.error_code, "OPERATOR_CANCELLED")
        self.assertEqual(self.simulator.execute(request), result)
        self.assertEqual(self.kernel.execution_count(request.operation_id), 0)
        scenario = self.simulator.execute(make_request(
            operation_id="op-cancel-scenario",
            scenario=SimulationScenario.CANCELLATION.value,
        ))
        self.assertEqual(scenario.execution_state, ExecutionState.CANCELLED)

    def test_duplicate_operation_with_different_request_is_rejected(self):
        self.simulator.execute(make_request(operation_id="op-duplicate", idempotency_key="key-1"))
        with self.assertRaises(TransactionError) as caught:
            self.simulator.execute(make_request(operation_id="op-duplicate", idempotency_key="key-2"))
        self.assertEqual(caught.exception.code, "DUPLICATE_OPERATION")

    def test_idempotency_conflict_is_rejected(self):
        self.simulator.execute(make_request(operation_id="op-key-1", idempotency_key="shared-key"))
        with self.assertRaises(TransactionError) as caught:
            self.simulator.execute(make_request(
                operation_id="op-key-2",
                idempotency_key="shared-key",
                amount=2000,
            ))
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_refund_references_original_operation_and_amount(self):
        original = self.purchase()
        request = make_request(
            operation_id="op-refund",
            amount=400,
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        )
        result = self.simulator.execute(request)
        self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
        self.assertEqual(result.original_operation_id, original.operation_id)
        self.assertEqual(result.original_amount, original.money)
        self.assertEqual(result.money.amount_minor, 400)

    def test_reversal_references_original_operation_and_amount(self):
        original = self.purchase()
        result = self.simulator.execute(make_request(
            operation_id="op-reversal",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        ))
        self.assertEqual(result.execution_state, ExecutionState.SUCCEEDED)
        self.assertEqual(result.original_operation_id, original.operation_id)
        self.assertEqual(result.original_amount, original.money)

    def test_excessive_refund_is_rejected_without_kernel_execution(self):
        original = self.purchase(amount=1000)
        result = self.simulator.execute(make_request(
            operation_id="op-excessive-refund",
            amount=1001,
            transaction_type=TransactionType.REFUND.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        ))
        self.assertEqual(result.execution_state, ExecutionState.FAILED)
        self.assertEqual(result.error_code, "EXCESSIVE_REFUND")
        self.assertEqual(self.kernel.execution_count("op-excessive-refund"), 0)

    def test_duplicate_reversal_is_rejected(self):
        original = self.purchase()
        self.simulator.execute(make_request(
            operation_id="op-reversal-1",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        ))
        duplicate = self.simulator.execute(make_request(
            operation_id="op-reversal-2",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        ))
        self.assertEqual(duplicate.execution_state, ExecutionState.FAILED)
        self.assertEqual(duplicate.error_code, "DUPLICATE_REVERSAL")
        self.assertEqual(self.kernel.execution_count("op-reversal-2"), 0)

    def test_evidence_redaction_removes_sensitive_values(self):
        request = make_request()
        result = TransactionResult(
            request=request,
            execution_state=ExecutionState.SUCCEEDED,
            outcome="APPROVED",
            recovery=RecoveryClass.NON_RETRYABLE,
            details={
                "pan": "4111111111111111",
                "pin": "1234",
                "cvv": "123",
                "track2": "4111111111111111=2512",
                "private_key": "secret",
                "raw_apdu": "00A4040000",
                "safe": "conservado",
            },
        )
        evidence = result.to_evidence()
        serialized = json.dumps(evidence)
        for forbidden in ["4111111111111111", "1234", "secret", "00A4040000"]:
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(evidence["details"]["safe"], "conservado")
        self.assertEqual(evidence["mode"], "SIMULATED")

    def test_simulation_flag_survives_result_round_trip_and_cannot_be_disabled(self):
        result = self.simulator.execute(make_request())
        restored = TransactionResult.from_dict(result.to_dict())
        self.assertTrue(restored.simulation)
        self.assertTrue(restored.to_dict()["simulation"])
        evidence = restored.to_evidence()
        self.assertEqual(evidence["mode"], "SIMULATED")
        self.assertEqual(evidence["physical_validation"], "NOT_EXECUTED")
        values = result.to_dict()
        values["simulation"] = False
        with self.assertRaises(ValueError):
            TransactionResult.from_dict(values)


if __name__ == "__main__":
    unittest.main()
