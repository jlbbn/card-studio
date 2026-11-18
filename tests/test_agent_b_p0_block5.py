import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from card_studio.application import CardStudioApplication, laboratory_profile
from card_studio.host.base import AuthorizationHostAdapter
from card_studio.host.mock import MockAuthorizationHost
from card_studio.host.response import validate_response_template
from card_studio.kernels.base import KernelError
from card_studio.l3.scenarios import LabKernelAdapter
from card_studio.money import Money
from card_studio.transactions.model import ExecutionState, TransactionError, TransactionRequest, TransactionResult
from card_studio.transactions.recovery import RecoveryClass


ISSUER = "A1A2A3A4A5A6A7A8"
COMMAND = "00DA000001A5"


class TemplateKernelDouble(LabKernelAdapter):
    def __init__(self, legacy=False):
        super().__init__()
        self.legacy = legacy
        self.complete_calls = []

    def complete_online(self, session, request, host_response):
        self._open(session)
        self.complete_calls.append((session.session_id, request.fingerprint(), host_response))
        if self.legacy:
            decision = host_response.get("decision")
            if decision not in {"APPROVED", "DECLINED"}:
                raise KernelError("LEGACY_DECISION_REQUIRED", "Se requiere decisión legacy.")
            arc = "00" if decision == "APPROVED" else "05"
            source = "LEGACY_DECISION"
        else:
            template = host_response.get("response_template")
            if not template:
                raise KernelError("RESPONSE_TEMPLATE_REQUIRED", "El doble exige response_template.")
            arc = validate_response_template(template)["authorization_response_code"]
            source = "KERNEL_ARC"
        approved = arc in {"00", "Y3"}
        result = TransactionResult(
            request=request,
            execution_state=ExecutionState.SUCCEEDED if approved else ExecutionState.FAILED,
            outcome="APPROVED" if approved else "DECLINED",
            recovery=RecoveryClass.NON_RETRYABLE,
            error_code=None if approved else "KERNEL_ARC_DECLINE",
            details={
                "selected_aid": request.application_aid or "A0000000031010",
                "action_result": {"final_decision_source": source, "arc": arc},
                "response_template": host_response.get("response_template"),
                "host_response": {
                    "response_template": host_response.get("response_template"),
                    "authorization_response_code": arc,
                },
            },
        )
        self._results[request.operation_id] = result
        return result


class TrackingHost(AuthorizationHostAdapter):
    def __init__(self, delegate, legacy_response=False, contradictory_decision=False, corrupt_digest=False):
        self.delegate = delegate
        self.legacy_response = legacy_response
        self.contradictory_decision = contradictory_decision
        self.corrupt_digest = corrupt_digest
        self.host_ref = delegate.host_ref
        self.host_version = delegate.host_version
        self.mode = delegate.mode
        self.authorize_calls = []
        self.reverse_calls = []

    def authorize(self, request, deadline=None):
        self.authorize_calls.append(request)
        response = dict(self.delegate.authorize(request, deadline))
        if self.legacy_response:
            for key in ("response_template", "authorization_response_code", "response_template_sha256"):
                response.pop(key, None)
        if self.contradictory_decision:
            response["decision"] = "APPROVED"
            response["outcome"] = "APPROVED"
            response["functional_result"] = "APPROVED"
        if self.corrupt_digest:
            response["response_template_sha256"] = "0" * 64
        return response

    def query(self, operation_id):
        return self.delegate.query(operation_id)

    def reverse(self, request, deadline=None):
        self.reverse_calls.append(request)
        return self.delegate.reverse(request, deadline)

    def cancel(self, operation_id):
        return self.delegate.cancel(operation_id)


class Block5Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.kernel = TemplateKernelDouble()
        self.profile = laboratory_profile(self.kernel.kernel_ref)
        self.profile_ref = f"{self.profile.profile_id}:{self.profile.profile_version}:{self.profile.profile_digest}"

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, operation_id="op-block5", scenario="online_required", **kwargs):
        return TransactionRequest.create(
            money=Money(1000, "MXN", 2),
            profile_ref=self.profile_ref,
            interface="VIRTUAL",
            kernel_ref=self.kernel.kernel_ref,
            scenario=scenario,
            operation_id=operation_id,
            idempotency_key=f"key-{operation_id}",
            **kwargs,
        )

    def application(self, host):
        return CardStudioApplication(
            data_dir=self.root,
            config_dir=self.root / "config",
            profiles=[self.profile],
            kernels=self.kernel,
            host=host,
        )

    def host(self, name="host.sqlite3", **kwargs):
        delegate = MockAuthorizationHost(
            self.root / name,
            evidence_dir=self.root / "evidence" / "host",
            issuer_authentication_data=ISSUER,
            issuer_script_commands={"71": [COMMAND]},
            **kwargs,
        )
        return delegate

    def test_approved_template_full_flow_audit_persistence_and_redaction(self):
        delegate = self.host()
        host = TrackingHost(delegate)
        app = self.application(host)
        request = self.request()
        result = app.create_transaction(request)
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["functional_result"], "APPROVED")
        self.assertEqual(result["details"]["action_result"]["final_decision_source"], "KERNEL_ARC")
        self.assertEqual(result["details"]["authorization_response_code"], "00")
        self.assertTrue(result["details"]["host_reference"].startswith("hr-"))
        self.assertEqual(len(host.authorize_calls), 1)
        canonical = host.authorize_calls[0]
        self.assertEqual(canonical.operation_id, request.operation_id)
        self.assertEqual(canonical.idempotency_key, request.idempotency_key)
        self.assertEqual(canonical.correlation_id, request.correlation_id)
        self.assertEqual(canonical.profile_ref, request.profile_ref)
        self.assertEqual(canonical.kernel_ref, request.kernel_ref)
        self.assertEqual(canonical.money, request.money)
        self.assertEqual(len(self.kernel.complete_calls), 1)
        self.assertIn("response_template", self.kernel.complete_calls[0][2])
        events = {entry["event_type"] for entry in app.audit(request.operation_id)}
        self.assertTrue({
            "ONLINE_REQUESTED",
            "HOST_AUTHORIZATION_COMPLETED",
            "ISSUER_PROCESSING",
            "ONLINE_COMPLETED",
            "KERNEL_CLOSED",
            "INTERFACE_CLOSED",
        } <= events)
        raw_template = self.kernel.complete_calls[0][2]["response_template"]
        serialized = json.dumps(result)
        evidence_path = self.root / "evidence" / "transactions" / result["result_reference"]
        evidence = evidence_path.read_text()
        host_evidence_path = self.root / "evidence" / "host" / f"{result['details']['host_reference']}.json"
        host_evidence = host_evidence_path.read_text()
        self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o600)
        for content in (serialized, evidence, host_evidence):
            for forbidden in (raw_template, ISSUER, COMMAND):
                self.assertNotIn(forbidden, content)

        def assert_payload_keys_absent(value):
            if isinstance(value, dict):
                self.assertNotIn("response_template", value)
                self.assertNotIn("issuer_authentication_data", value)
                self.assertNotIn("script_commands", value)
                for item in value.values():
                    assert_payload_keys_absent(item)
            elif isinstance(value, list):
                for item in value:
                    assert_payload_keys_absent(item)

        for value in (result, json.loads(evidence), json.loads(host_evidence)):
            assert_payload_keys_absent(value)
        self.assertIn("response_template_sha256", evidence)
        self.assertIn("host_reference", evidence)
        transaction_db = (self.root / "transactions-v1.sqlite3").read_bytes()
        self.assertNotIn(b'"response_template":', transaction_db)
        self.assertNotIn(raw_template.encode(), transaction_db)
        self.assertNotIn(ISSUER.encode(), transaction_db)

    def test_declined_unable_and_explicit_fallbacks_final_result_comes_from_arc(self):
        scenarios = [
            ("declined", "declined", "05", "FAILED", "DECLINED"),
            ("unable", "declined", "Z3", "FAILED", "DECLINED"),
            ("unable", "approved", "Y3", "SUCCEEDED", "APPROVED"),
        ]
        for index, (policy, fallback, arc, state, functional) in enumerate(scenarios):
            with self.subTest(arc=arc):
                self.kernel = TemplateKernelDouble()
                delegate = self.host(
                    f"policy-{index}.sqlite3",
                    authorization_policy=policy,
                    fallback_policy=fallback,
                )
                host = TrackingHost(delegate, contradictory_decision=arc == "05")
                app = self.application(host)
                request = self.request(f"op-policy-{index}")
                result = app.create_transaction(request)
                self.assertEqual(result["execution_state"], state)
                self.assertEqual(result["functional_result"], functional)
                self.assertEqual(result["details"]["authorization_response_code"], arc)
                self.assertEqual(result["details"]["action_result"]["final_decision_source"], "KERNEL_ARC")
                self.assertEqual(len(self.kernel.complete_calls), 1)

    def test_uncertain_fallback_and_host_timeout_never_complete_or_reverse(self):
        self.kernel = TemplateKernelDouble()
        query_delegate = self.host(
            "query-fallback.sqlite3",
            authorization_policy="unable",
            fallback_policy="query",
        )
        query_host = TrackingHost(query_delegate)
        app = self.application(query_host)
        fallback = app.create_transaction(self.request("op-fallback-query"))
        self.assertEqual(fallback["execution_state"], "UNCERTAIN")
        self.assertEqual(fallback["recovery_action"], "QUERY_REQUIRED")
        self.assertEqual(len(self.kernel.complete_calls), 0)

        self.kernel = TemplateKernelDouble()
        timeout_delegate = self.host("timeout.sqlite3", timeout_operations={"op-timeout"})
        timeout_host = TrackingHost(timeout_delegate)
        timeout_app = self.application(timeout_host)
        timeout = timeout_app.create_transaction(self.request("op-timeout"))
        self.assertEqual(timeout["execution_state"], "UNCERTAIN")
        self.assertEqual(timeout["recovery_action"], "QUERY_REQUIRED")
        self.assertNotEqual(timeout["functional_result"], "DECLINED")
        self.assertEqual(len(self.kernel.complete_calls), 0)
        self.assertEqual(len(timeout_host.authorize_calls), 1)
        self.assertEqual(timeout_host.reverse_calls, [])
        events = {entry["event_type"] for entry in timeout_app.audit("op-timeout")}
        self.assertIn("HOST_AUTHORIZATION_UNCERTAIN", events)
        self.assertNotIn("ONLINE_COMPLETED", events)
        self.assertEqual(timeout_app.query("op-timeout")["execution_state"], "UNCERTAIN")
        self.assertEqual(len(timeout_host.authorize_calls), 1)

    def test_online_replay_restart_and_conflict_do_not_resend(self):
        delegate = self.host()
        first_host = TrackingHost(delegate)
        first_app = self.application(first_host)
        request = self.request()
        first = first_app.create_transaction(request)
        self.kernel = TemplateKernelDouble()
        restarted_host = TrackingHost(self.host())
        restarted = self.application(restarted_host)
        replay = restarted.create_transaction(request)
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(replay["result_reference"], first["result_reference"])
        self.assertEqual(replay["details"]["host_reference"], first["details"]["host_reference"])
        self.assertEqual(restarted_host.authorize_calls, [])
        self.assertEqual(self.kernel.complete_calls, [])
        conflict = replace(request, operation_id="op-conflict", money=Money(2000, "MXN", 2))
        with self.assertRaises(TransactionError) as caught:
            restarted.create_transaction(conflict)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(restarted_host.authorize_calls, [])

    def test_unavailable_pre_send_is_safe_and_does_not_call_kernel(self):
        delegate = self.host(unavailable_operations={"op-block5"})
        host = TrackingHost(delegate)
        app = self.application(host)
        request = self.request()
        result = app.create_transaction(request)
        self.assertEqual(result["execution_state"], "FAILED")
        self.assertEqual(result["recovery_action"], "SAFE_TO_RETRY")
        self.assertEqual(result["error_code"], "HOST_UNAVAILABLE_BEFORE_SEND")
        self.assertEqual(self.kernel.complete_calls, [])
        self.assertEqual(host.reverse_calls, [])
        replay = app.retry_transaction(request)
        self.assertEqual(replay["recovery_action"], "SAFE_TO_RETRY")
        self.assertEqual(self.kernel.complete_calls, [])
        self.assertEqual(host.reverse_calls, [])

    def test_legacy_decision_contract_double_remains_supported(self):
        self.kernel = TemplateKernelDouble(legacy=True)
        delegate = self.host()
        host = TrackingHost(delegate, legacy_response=True)
        app = self.application(host)
        result = app.create_transaction(self.request())
        self.assertEqual(result["execution_state"], "SUCCEEDED")
        self.assertEqual(result["details"]["action_result"]["final_decision_source"], "LEGACY_DECISION")
        self.assertEqual(len(self.kernel.complete_calls), 1)
        self.assertNotIn("response_template", self.kernel.complete_calls[0][2])

    def test_invalid_template_digest_does_not_complete_or_approve(self):
        delegate = self.host()
        host = TrackingHost(delegate, corrupt_digest=True)
        app = self.application(host)
        result = app.create_transaction(self.request())
        self.assertEqual(result["execution_state"], "FAILED")
        self.assertEqual(result["error_code"], "HOST_RESPONSE_DIGEST_MISMATCH")
        self.assertNotEqual(result["functional_result"], "APPROVED")
        self.assertEqual(self.kernel.complete_calls, [])
        events = {entry["event_type"] for entry in app.audit("op-block5")}
        self.assertIn("HOST_AUTHORIZATION_UNCERTAIN", events)
        self.assertNotIn("ONLINE_COMPLETED", events)

    def test_online_event_evidence_contains_arc_metadata_without_payloads(self):
        delegate = self.host()
        app = self.application(TrackingHost(delegate))
        result = app.create_transaction(self.request())
        event_paths = list((self.root / "evidence" / "transactions").glob("op-block5-event-*.json"))
        self.assertTrue(event_paths)
        online_completed = []
        for path in event_paths:
            value = json.loads(path.read_text())
            if value["event_type"] == "ONLINE_COMPLETED":
                online_completed.append(value)
        self.assertEqual(len(online_completed), 1)
        value = online_completed[0]
        self.assertEqual(value["authorization_response_code"], "00")
        self.assertEqual(value["host_reference"], result["details"]["host_reference"])
        self.assertEqual(value["response_template_sha256"], result["details"]["response_template_sha256"])
        self.assertNotIn("response_template", value)
        self.assertNotIn(ISSUER, json.dumps(value))


if __name__ == "__main__":
    unittest.main()
