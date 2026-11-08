from __future__ import annotations

import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from card_studio.production.aws import CONTROLS, assess_aws, assessment_status
from card_studio.production.backup import create_backup_manifest, restore_sqlite_isolated, validate_runbook
from card_studio.production.release import build_manifest, deployment_decision, source_tree_digest, validate_manifest
from card_studio.production.store import LedgerStore
from card_studio.production.treasury import TreasuryService

from test_p3_persistence import contract


class P3OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LedgerStore(self.root / "ledger.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_aws_assessment_classifies_pass_improvement_blocker_and_deferred(self):
        secure = {key: True for _, key in CONTROLS}
        findings = assess_aws(secure)
        self.assertEqual(assessment_status(findings), "PASS")
        insecure = dict(secure, root_usage_disabled=False, task_public_ip_disabled=False)
        statuses = {item["control"]: item["status"] for item in assess_aws(insecure)}
        self.assertEqual(statuses["Root usage"], "BLOCKER")
        self.assertEqual(statuses["Task public IP"], "IMPROVEMENT")
        deferred = assess_aws(dict(secure, restore_tested="DEFERRED"))
        self.assertEqual(deferred[10]["status"], "DEFERRED")
        unknown = assess_aws({})
        self.assertEqual(len(unknown), 16)
        self.assertEqual(assessment_status(unknown), "PARTIAL")
        self.assertTrue(all(item["status"] == "BLOCKER" for item in unknown))

    def test_aws_external_evidence_closes_gate_with_classified_follow_up(self):
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / "evidence/aws-p3/external-assessment.json").read_text())
        self.assertEqual(evidence["assessment_status"], "PASS")
        self.assertEqual(evidence["gate_scope"], "AUDIT_AND_CLASSIFY")
        self.assertEqual(evidence["region"], "us-east-2")
        self.assertEqual(evidence["blockers"], [])
        self.assertFalse(evidence["infrastructure_compliance_certified"])
        domains = {item["domain_id"]: item for item in evidence["domains"]}
        required = {
            "ECS-SUNMI", "ECS-SWIFT-LEDGER", "ALB-ROUTING", "RDS-SECURITY",
            "RDS-LEAST-PRIVILEGE-INGRESS", "AWS-CONFIG-RECORDER", "CLOUDTRAIL-OPERATIONS",
            "VPC-FLOW-LOGS", "SWIFTLEDGER-PUBLIC-IP", "APP-EGRESS", "RDS-MULTI-AZ",
            "IAM-AND-ROOT", "BACKUP-INVENTORY", "CLOUDWATCH-ALARMS", "SECRETS-METADATA",
            "RDS-RESTORE-READINESS",
        }
        self.assertTrue(required <= set(domains))
        self.assertEqual(len(evidence["pending_improvements"]), 4)
        self.assertTrue(all(domains[finding]["status"] == "IMPROVEMENT" for finding in (
            "VPC-FLOW-LOGS", "SWIFTLEDGER-PUBLIC-IP", "APP-EGRESS", "RDS-MULTI-AZ",
        )))
        self.assertEqual(domains["RDS-LEAST-PRIVILEGE-INGRESS"]["status"], "PASS")
        self.assertEqual(domains["IAM-AND-ROOT"]["status"], "DEFERRED")
        script = (root / "scripts/aws_p3_readonly_evidence.sh").read_text()
        actions = re.findall(r"^\s*aws\s+[a-z0-9-]+\s+([a-z0-9-]+)", script, re.MULTILINE)
        self.assertGreater(len(actions), 20)
        self.assertTrue(all(action.startswith(("get-", "list-", "describe-", "lookup-")) for action in actions))
        self.assertNotIn("get-secret-value", script)
        self.assertNotIn("SecretString", script)
        document = (root / "docs/AWS_HARDENING_P3.md").read_text()
        for finding in evidence["pending_improvements"]:
            self.assertIn(finding, document)
        self.assertIn("Remaining P3: NONE", document)

    def test_backup_manifest_and_isolated_restore(self):
        self.store.record(contract("backup-original"))
        manifest = create_backup_manifest([self.store.path], 35)
        self.assertEqual(manifest["retention_days"], 35)
        self.assertFalse(manifest["contains_secrets"])
        restored = self.root / "isolated" / "restored.sqlite3"
        result = restore_sqlite_isolated(self.store.path, restored)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["operations"], 1)
        self.assertTrue(restored.is_file())
        with self.assertRaises(ValueError):
            restore_sqlite_isolated(restored, restored)
        runbook = (Path(__file__).resolve().parents[1] / "docs" / "BACKUP_RECOVERY_P3.md").read_text()
        self.assertTrue(validate_runbook(runbook))

    def test_release_manifest_reproducibility_and_rollback_gate(self):
        root = Path(__file__).resolve().parents[1]
        first = source_tree_digest(root)
        self.assertEqual(first, source_tree_digest(root))
        image = "sha256:" + "a" * 64
        manifest = build_manifest(root, image, 424, 424)
        self.assertTrue(validate_manifest(manifest))
        self.assertEqual(manifest["rollback"]["image_digest"], image)
        self.assertTrue(deployment_decision(manifest, image)["allowed"])
        self.assertFalse(deployment_decision(manifest, "sha256:" + "b" * 64)["allowed"])
        self.assertFalse(manifest["deployment"]["deployed"])
        with self.assertRaises(ValueError):
            build_manifest(root, "latest", 424, 424)
        with self.assertRaises(ValueError):
            build_manifest(root, image, 423, 424)
        lock = (root / "requirements-p3-lock.txt").read_text().splitlines()
        self.assertTrue(all("==" in line for line in lock if line.strip()))

    def test_load_multiple_terminals_parallel_transactions_and_reconciliation(self):
        requests = [
            contract(f"load-{terminal}-{operation}", terminal_id=f"terminal-{terminal}")
            for terminal in range(8) for operation in range(4)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(self.store.record, requests))
        self.assertEqual(sum(created for _, created in results), 32)
        self.assertEqual(self.store.operation_count(), 32)
        self.assertEqual(self.store.effect_count(), 32)
        with ThreadPoolExecutor(max_workers=8) as executor:
            batches = list(executor.map(lambda _: self.store.consume("treasury-load", 4), range(8)))
        consumed = [item for batch in batches for item in batch]
        self.assertEqual(len(consumed), 32)
        self.assertEqual(len({item["effect_id"] for item in consumed}), 32)
        report = TreasuryService(self.store, "treasury-load").reconcile(
            TreasuryService.expectations(consumed),
        )
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(self.store.consume("treasury-load"), [])


if __name__ == "__main__":
    unittest.main()
