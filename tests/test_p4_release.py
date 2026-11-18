from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from card_studio.p4.deployment import DeploymentGate
from card_studio.p4.manifests import build_rc1_manifest, p4_source_digest, validate_rc1_manifest, validate_staging_config
from card_studio.p4.observability import AlarmEvaluator, REQUIRED_ALARMS, RequestMetrics, percentile
from card_studio.p4.recovery import DisasterRecoveryClock
from card_studio.p4.security import validate_secret_metadata
from card_studio.p4.server import TreasuryRuntime


class P4ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.image = "sha256:" + "a" * 64
        self.rollback = "sha256:" + "b" * 64

    def manifest(self):
        return build_rc1_manifest(self.root, self.image, self.rollback, 429, 429)

    def test_frozen_p3_baseline_and_rc1_reproducible_manifest(self):
        freeze = json.loads((self.root / "release/p4/p3-baseline-freeze.json").read_text())
        self.assertEqual(freeze["full_regression"], "429/429 PASS")
        self.assertFalse(freeze["vcs_repository"])
        manifest = self.manifest()
        self.assertTrue(validate_rc1_manifest(manifest))
        self.assertEqual(manifest["commit_type"], "CONTENT_SHA256_NOT_VCS")
        self.assertEqual(manifest["commit_sha"], p4_source_digest(self.root))
        self.assertEqual(manifest["rollback_target"]["image_digest"], self.rollback)
        self.assertTrue(manifest["dependency_lock"]["pinned"])
        self.assertFalse(manifest["configuration_manifest"]["contains_secrets"])
        self.assertFalse(manifest["deployment"]["production"])

    def test_staging_refuses_production_or_secrets(self):
        config = json.loads((self.root / "staging/config.json").read_text())
        self.assertTrue(validate_staging_config(config))
        self.assertFalse(validate_staging_config({**config, "production": True}))
        self.assertFalse(validate_staging_config({**config, "external_endpoints": ["https://production.invalid"]}))
        self.assertFalse(validate_staging_config({**config, "service_secret_reference": "literal-secret"}))

    def test_deployment_gate_sequence_and_fail_closed(self):
        calls = []
        actions = {
            name: lambda name=name: calls.append(name) or {"revision": "rc1-001"}
            for name in ("PRECHECK", "DEPLOY", "HEALTH", "READINESS", "MIGRATION_VERIFY", "E2E_SYNTHETIC", "OBSERVABILITY_VERIFY")
        }
        result = DeploymentGate(self.manifest(), "correlation-p4").run(actions)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["gates"]), 7)
        self.assertEqual(result["correlation_id"], "correlation-p4")
        self.assertEqual(result["task_revisions"]["swiftledger"], "rc1-001")
        calls = []
        def fail():
            raise RuntimeError("synthetic")
        actions["READINESS"] = fail
        failed = DeploymentGate(self.manifest()).run(actions)
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["gates"][-1]["gate"], "READINESS")
        self.assertNotIn("E2E_SYNTHETIC", calls)

    def test_manifest_rejects_mutable_image_or_red_regression(self):
        with self.assertRaises(ValueError):
            build_rc1_manifest(self.root, "latest", self.rollback, 429, 429)
        with self.assertRaises(ValueError):
            build_rc1_manifest(self.root, self.image, self.rollback, 428, 429)

    def test_metrics_percentiles_and_controlled_alarm(self):
        self.assertEqual(percentile([10, 20, 30, 40], 50), 25.0)
        metrics = RequestMetrics()
        for status in (200, 200, 400, 503):
            metrics.record(status, 10)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["4xx"], 1)
        self.assertEqual(snapshot["5xx"], 1)
        evaluator = AlarmEvaluator()
        self.assertEqual(len(REQUIRED_ALARMS), 11)
        self.assertEqual(evaluator.evaluate("service-unavailable", 1)["state"], "ALARM")
        self.assertEqual(evaluator.evaluate("service-unavailable", 0)["state"], "OK")
        self.assertEqual(evaluator.evaluate("rds-free-storage", 512)["state"], "ALARM")
        self.assertEqual(evaluator.evaluate("rds-cpu", None)["state"], "INSUFFICIENT_DATA")

    def test_disaster_recovery_requires_complete_isolated_cycle(self):
        clock = DisasterRecoveryClock()
        with self.assertRaises(ValueError):
            clock.report(0)
        clock.mark("BACKUP_VERIFIED")
        clock.mark("RESTORE_COMPLETE")
        clock.mark("VERIFICATION_COMPLETE")
        report = clock.report(1.25)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["rpo_seconds"], 1.25)
        self.assertFalse(report["production_restore"])

    def test_treasury_settle_drains_batches_and_flags_backlog(self):
        class Store:
            def __init__(self, effects):
                self.effects = effects
            def effect_count(self):
                return self.effects
            def consumed(self, _consumer):
                return [{"terminal_id": "terminal", "transaction_id": str(index), "amount_minor": 1, "currency": "MXN"} for index in range(250)]
        class Treasury:
            consumer_id = "tesoreria-p3"
            def __init__(self):
                self.batches = [list(range(100)), list(range(100)), list(range(50)), []]
                self.calls = 0
            def consume(self):
                self.calls += 1
                return self.batches.pop(0)
            def reconcile(self, _expected):
                return {"status": "MATCHED", "review_required": False}
        store = Store(250)
        runtime = TreasuryRuntime(store)
        runtime.treasury = Treasury()
        result = runtime.settle()
        self.assertEqual(result["backlog"], 0)
        self.assertEqual(result["reconciliation"]["status"], "MATCHED")
        self.assertEqual(runtime.treasury.calls, 4)
        runtime = TreasuryRuntime(Store(251))
        runtime.treasury = Treasury()
        result = runtime.settle()
        self.assertEqual(result["backlog"], 1)
        self.assertEqual(result["reconciliation"]["status"], "REVIEW_REQUIRED")

    def test_secret_metadata_never_accepts_values(self):
        self.assertTrue(validate_secret_metadata({
            "name": "staging-service", "rotation_enabled": True,
            "last_changed": "2026-11-08", "kms_key_reference": "local-synthetic",
        }))
        self.assertFalse(validate_secret_metadata({"name": "staging", "secret_string": "forbidden"}))
        self.assertFalse(validate_secret_metadata({"name": "staging", "password": "forbidden"}))


if __name__ == "__main__":
    unittest.main()
