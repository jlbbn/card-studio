import contextlib
import json
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from card_studio.application import laboratory_profile
from card_studio.host.base import HostError
from card_studio.host.mock import MockAuthorizationHost
from card_studio.host.response import build_response_template, validate_response_template
from card_studio.money import Money
from card_studio.transactions.model import TransactionRequest, TransactionType


ISSUER = "A1A2A3A4A5A6A7A8"
COMMAND = "00000000"
SCRIPT_ID = "DEADBEEF"


class HostAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        profile = laboratory_profile("mock-kernel-v1")
        self.profile_ref = f"{profile.profile_id}:{profile.profile_version}:{profile.profile_digest}"

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, operation_id="op-host-1", scenario="online_required", **kwargs):
        return TransactionRequest.create(
            money=Money(1000, "MXN", 2),
            profile_ref=self.profile_ref,
            interface="VIRTUAL",
            scenario=scenario,
            operation_id=operation_id,
            idempotency_key=f"key-{operation_id}",
            **kwargs,
        )

    def host(self, name="host.sqlite3", **kwargs):
        return MockAuthorizationHost(self.root / name, **kwargs)

    def test_approved_template_identity_restart_and_private_evidence(self):
        evidence = self.root / "evidence"
        host = self.host(
            evidence_dir=evidence,
            issuer_authentication_data=ISSUER,
            issuer_script_commands={"71": [COMMAND], "72": [COMMAND]},
            issuer_script_identifiers={"71": SCRIPT_ID},
        )
        request = self.request()
        response = host.authorize(request)
        self.assertEqual(response["execution_state"], "SUCCEEDED")
        self.assertEqual(response["authorization_response_code"], "00")
        self.assertTrue(response["response_template"].startswith("8A023030"))
        self.assertEqual(response["response_template_sha256"], validate_response_template(response["response_template"])["response_template_sha256"])
        self.assertTrue(response["host_reference"].startswith("hr-"))
        self.assertEqual(response["host_version"], "2")
        self.assertTrue(response["simulation"])
        replay = self.host().authorize(request)
        self.assertEqual(replay, response)
        self.assertEqual(replay["host_reference"], response["host_reference"])
        with contextlib.closing(sqlite3.connect(self.root / "host.sqlite3")) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        evidence_path = evidence / f"{response['host_reference']}.json"
        self.assertEqual(stat.S_IMODE(evidence_path.stat().st_mode), 0o600)
        value = json.loads(evidence_path.read_text())
        self.assertNotIn("response_template", value)
        self.assertEqual(value["response_template_sha256"], response["response_template_sha256"])
        self.assertNotIn("8A023030", evidence_path.read_text())

    def test_issuer_and_scripts_template_is_strict_and_sanitized(self):
        template = build_response_template(
            "00",
            ISSUER,
            {"71": [COMMAND, COMMAND], "72": [COMMAND]},
            {"71": SCRIPT_ID, "72": SCRIPT_ID},
        )
        metadata = validate_response_template(template)
        self.assertEqual(metadata["authorization_response_code"], "00")
        self.assertEqual(metadata["response_tags"], ["8A", "91", "71", "72"])
        self.assertEqual(metadata["script_count"], 3)
        self.assertEqual(metadata["script_identifier_count"], 2)
        response = self.host(
            evidence_dir=self.root / "evidence",
            issuer_authentication_data=ISSUER,
            issuer_script_commands={"71": [COMMAND, COMMAND], "72": [COMMAND]},
            issuer_script_identifiers={"71": SCRIPT_ID, "72": SCRIPT_ID},
        ).authorize(self.request())
        serialized = (self.root / "evidence" / f"{response['host_reference']}.json").read_text()
        for forbidden in (ISSUER, COMMAND, SCRIPT_ID):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('"response_template":', serialized)
        self.assertEqual(response["script_count"], 3)
        self.assertEqual(response["script_identifier_count"], 2)

    def test_declined_and_unable_fallback_policies(self):
        declined = self.host("declined.sqlite3", authorization_policy="declined").authorize(self.request())
        self.assertEqual(declined["authorization_response_code"], "05")
        self.assertEqual(declined["execution_state"], "FAILED")
        self.assertEqual(declined["functional_result"], "DECLINED")
        unable = self.host("unable.sqlite3", authorization_policy="unable").authorize(self.request())
        self.assertEqual(unable["authorization_response_code"], "Z3")
        self.assertEqual(unable["execution_state"], "FAILED")
        self.assertEqual(unable["functional_result"], "DECLINED")
        self.assertEqual(unable["response_origin"], "LOCAL_FALLBACK")
        self.assertFalse(unable["command_sent"])
        approved = self.host(
            "fallback-approved.sqlite3",
            authorization_policy="unable",
            fallback_policy="approved",
        ).authorize(self.request())
        self.assertEqual(approved["authorization_response_code"], "Y3")
        self.assertEqual(approved["execution_state"], "SUCCEEDED")
        query = self.host(
            "fallback-query.sqlite3",
            authorization_policy="unable",
            fallback_policy="query",
        ).authorize(self.request())
        self.assertEqual(query["authorization_response_code"], "Z3")
        self.assertEqual(query["execution_state"], "UNCERTAIN")
        self.assertEqual(query["recovery_action"], "QUERY_REQUIRED")
        with self.assertRaises(HostError):
            self.host("bad-policy.sqlite3", authorization_policy="automatic")
        with self.assertRaises(HostError):
            self.host("bad-fallback.sqlite3", authorization_policy="unable", fallback_policy="retry")

    def test_malformed_ber_and_arc_are_rejected(self):
        cases = {
            "": "MALFORMED_RESPONSE_TEMPLATE",
            "8A": "MALFORMED_RESPONSE_TEMPLATE",
            "8A81023030": "MALFORMED_RESPONSE_TEMPLATE",
            "8A80023030": "MALFORMED_RESPONSE_TEMPLATE",
            "8A0130": "INVALID_AUTHORIZATION_RESPONSE_CODE",
            "8A024747": "INVALID_AUTHORIZATION_RESPONSE_CODE",
            "8A0230": "MALFORMED_RESPONSE_TEMPLATE",
            "9108A1A2A3A4A5A6A7A8": "MISSING_AUTHORIZATION_RESPONSE_CODE",
        }
        for template, code in cases.items():
            with self.subTest(template=template), self.assertRaises(HostError) as caught:
                validate_response_template(template)
            self.assertEqual(caught.exception.code, code)

    def test_duplicate_nested_and_unsupported_objects_are_rejected(self):
        cases = {
            "8A0230308A023035": "DUPLICATE_RESPONSE_OBJECT",
            "8A0230309108A1A2A3A4A5A6A7A89108A1A2A3A4A5A6A7A8": "DUPLICATE_RESPONSE_OBJECT",
            "70048A023030": "NESTED_RESPONSE_OBJECT",
            "8A02303071048A023030": "NESTED_RESPONSE_OBJECT",
            "95050000000000": "UNSUPPORTED_RESPONSE_OBJECT",
            "8A023030710495020000": "UNSUPPORTED_RESPONSE_OBJECT",
        }
        for template, code in cases.items():
            with self.subTest(template=template), self.assertRaises(HostError) as caught:
                validate_response_template(template)
            self.assertEqual(caught.exception.code, code)

    def test_invalid_issuer_and_script_shapes_are_rejected(self):
        cases = {
            "8A0230309107A1A2A3A4A5A6A7": "INVALID_ISSUER_AUTHENTICATION_DATA",
            "8A0230307100": "INVALID_ISSUER_SCRIPT",
            "8A02303071058603000000": "INVALID_ISSUER_SCRIPT",
            "8A02303071069F1803AABBCC": "INVALID_ISSUER_SCRIPT",
            "8A023030710E9F1804AABBCCDD9F1804AABBCCDD": "DUPLICATE_RESPONSE_OBJECT",
        }
        for template, code in cases.items():
            with self.subTest(template=template), self.assertRaises(HostError) as caught:
                validate_response_template(template)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(HostError):
            build_response_template("00", issuer_script_commands={"73": [COMMAND]})
        with self.assertRaises(HostError):
            build_response_template("00", issuer_authentication_data="00")

    def test_timeout_after_send_has_no_issuer_response_or_auto_action(self):
        host = self.host(timeout_operations={"op-host-1"})
        request = self.request()
        first = host.authorize(request)
        self.assertEqual(first["execution_state"], "UNCERTAIN")
        self.assertEqual(first["recovery_action"], "QUERY_REQUIRED")
        self.assertIsNone(first["response_template"])
        self.assertIsNone(first["authorization_response_code"])
        self.assertTrue(first["command_sent"])
        self.assertEqual(host.authorize(request), first)
        self.assertEqual(self.host().query(request.operation_id), first)
        cancelled = host.cancel(request.operation_id)
        self.assertEqual(cancelled["execution_state"], "UNCERTAIN")
        self.assertEqual(cancelled["cancellation_status"], "NOT_CONFIRMED")

    def test_unavailable_cancel_and_sent_cancellation_rules(self):
        host = self.host(unavailable_operations={"op-host-1"})
        request = self.request()
        unavailable = host.authorize(request)
        self.assertEqual(unavailable["recovery_action"], "SAFE_TO_RETRY")
        self.assertIsNone(unavailable["response_template"])
        cancelled = host.cancel(request.operation_id)
        self.assertEqual(cancelled["execution_state"], "CANCELLED")
        self.assertIsNone(cancelled["response_template"])
        self.assertEqual(self.host().query(request.operation_id)["execution_state"], "CANCELLED")
        approved = self.host().authorize(self.request("op-other"))
        not_cancelled = self.host().cancel("op-other")
        self.assertEqual(not_cancelled["execution_state"], "SUCCEEDED")
        self.assertEqual(not_cancelled["cancellation_status"], "NOT_CANCELLED_ALREADY_SENT")
        self.assertEqual(approved["execution_state"], "SUCCEEDED")

    def test_reversal_original_reference_duplicate_and_conflict_restart(self):
        host = self.host()
        original = self.request("op-original")
        original_response = host.authorize(original)
        reversal = self.request(
            "op-reversal",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        )
        response = host.reverse(reversal)
        self.assertEqual(response["host_operation"], "reverse")
        self.assertEqual(response["original_operation_id"], original.operation_id)
        self.assertEqual(response["original_host_reference"], original_response["host_reference"])
        self.assertNotEqual(response["host_reference"], original_response["host_reference"])
        self.assertEqual(self.host().query(reversal.operation_id)["original_host_reference"], original_response["host_reference"])
        duplicate = self.request(
            "op-reversal-2",
            transaction_type=TransactionType.REVERSAL.value,
            original_operation_id=original.operation_id,
            original_amount=original.money,
        )
        with self.assertRaises(HostError) as caught:
            self.host().reverse(duplicate)
        self.assertEqual(caught.exception.code, "DUPLICATE_REVERSAL")
        with self.assertRaises(HostError) as conflict:
            self.host().authorize(replace(original, money=Money(2000, "MXN", 2)))
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_unknown_query_is_uncertain_and_simulated(self):
        response = self.host().query("op-unknown")
        self.assertEqual(response["execution_state"], "UNCERTAIN")
        self.assertEqual(response["recovery_action"], "QUERY_REQUIRED")
        self.assertIsNone(response["response_template"])
        self.assertIsNone(response["host_reference"])
        self.assertTrue(response["simulation"])
        self.assertEqual(response["mode"], "SIMULATED")

    def test_legacy_host_record_migrates_to_v2_with_arc_template(self):
        path = self.root / "legacy.sqlite3"
        request = self.request("op-legacy")
        old_response = {
            "operation_id": request.operation_id,
            "correlation_id": request.correlation_id,
            "host_ref": "mock-authorization-host-v1",
            "host_version": "1",
            "execution_state": "SUCCEEDED",
            "functional_result": "APPROVED",
            "decision": "APPROVED",
            "outcome": "APPROVED",
            "simulation": True,
            "synthetic": True,
            "recovery_action": "NON_RETRYABLE",
            "error_code": None,
            "command_sent": True,
            "mode": "SIMULATED",
            "evidence_level": "SIMULATED",
            "physical_validation": "NOT_EXECUTED",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        from card_studio.transactions.store import canonical_request_digest
        digest = canonical_request_digest(request)
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.executescript("""
                CREATE TABLE host_requests (
                    operation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    command_sent INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            connection.execute(
                "INSERT INTO host_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.operation_id,
                    request.idempotency_key,
                    digest,
                    json.dumps(request.to_dict()),
                    json.dumps(old_response),
                    1,
                    old_response["timestamp"],
                    old_response["timestamp"],
                ),
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        migrated = self.host("legacy.sqlite3").query(request.operation_id)
        self.assertEqual(migrated["host_version"], "2")
        self.assertEqual(migrated["authorization_response_code"], "00")
        self.assertEqual(migrated["response_template"], "8A023030")
        self.assertTrue(migrated["host_reference"].startswith("hr-"))
        with contextlib.closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
