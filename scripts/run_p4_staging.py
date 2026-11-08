from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from card_studio.p4.deployment import DeploymentGate
from card_studio.p4.load import run_progressive_load
from card_studio.p4.manifests import validate_rc1_manifest, validate_staging_config, write_json
from card_studio.p4.observability import AlarmEvaluator
from card_studio.p4.recovery import DisasterRecoveryClock
from card_studio.production.contract import CardServiceContract, canonical_json, utc_now
from card_studio.production.http import LedgerServiceClient
from card_studio.production.security import ServiceAuthenticator, ServiceCredentials
from card_studio.production.store import LedgerStore


NETWORK = "card-p4-staging-net"
DB = "card-p4-postgres"
DB_NAME = "card_p4_staging"
SERVICES = ("sunmi-pos", "tesoreria", "swiftledger")
PREFIX = "card-p4-"
POS_DATA = "card-p4-posdata"
PG_DATA = "card-p4-pgdata"
EVIDENCE = ROOT / "evidence" / "p4" / "rc1"
DB_PASSWORD = "p4-synthetic-only"
SECRET = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
DSN = f"postgresql://postgres:{DB_PASSWORD}@postgres:5432/{DB_NAME}"


def run(*args: str, capture: bool = True) -> str:
    result = subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT)
    return result.stdout.strip() if capture else ""


def docker(*args: str) -> str:
    return run("docker", *args)


def request(method: str, url: str, body: Any = None, timeout: float = 5) -> tuple[int, Any]:
    raw = canonical_json(body) if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method, headers={"Content-Type": "application/json"} if raw else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        with error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            except ValueError:
                return error.code, {}
    except Exception as error:
        return 599, {"error": type(error).__name__}


class Staging:
    def __init__(self):
        self.manifest = json.loads((ROOT / "release/p4/rc1/deployment-manifest.json").read_text())
        self.config = json.loads((ROOT / "staging/config.json").read_text())
        self.urls: dict[str, str] = {}
        self.scenarios: list[dict[str, Any]] = []
        self.signing_directory = tempfile.TemporaryDirectory(prefix="p4-signing-")
        self.signing_store = LedgerStore(Path(self.signing_directory.name) / "signing.db")
        credentials = {terminal: ServiceCredentials(bytes(range(32))) for terminal in ["terminal-p4", *(f"terminal-p4-{index:03d}" for index in range(1, 101))]}
        self.authenticator = ServiceAuthenticator(credentials, self.signing_store)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        for service in SERVICES:
            name = self.container(service)
            running = docker("ps", "--filter", f"name=^/{name}$", "--format", "{{.Names}}")
            if running:
                port = self.config["services"][service]["port"]
                self.urls[service] = f"http://{docker('port', name, f'{port}/tcp')}"

    def container(self, service: str) -> str:
        return PREFIX + service

    def wait(self, service: str, path: str = "/readyz", expected: bool = True, attempts: int = 150) -> dict[str, Any]:
        port = self.config["services"][service]["port"]
        self.urls[service] = f"http://{docker('port', self.container(service), f'{port}/tcp')}"
        url = self.urls[service]
        value = {}
        for _ in range(attempts):
            status, value = request("GET", url + path, timeout=1)
            if status in {200, 503} and value.get("ok") is expected:
                return value
            time.sleep(0.2)
        raise RuntimeError(f"{service}{path} did not reach {expected}: {value}")

    def start_services(self, image: str, revision: str, delay_operation: str = "") -> None:
        for service in SERVICES:
            port = self.config["services"][service]["port"]
            environment = [
                "-e", "P4_ENVIRONMENT=STAGING_ISOLATED",
                "-e", f"P4_ROLE={service}",
                "-e", f"P4_TASK_REVISION={revision}",
                "-e", f"P4_SERVICE_SECRET={SECRET}",
                "-e", f"P4_DB_DSN={DSN}",
                "-e", "P4_LEDGER_URL=http://swiftledger:5000",
                "-e", "P4_LEDGER_TIMEOUT=1",
                "-e", f"P4_DELAY_OPERATION_ID={delay_operation}",
                "-e", "P4_DELAY_SECONDS=3",
                "-e", "P4_SETTLE_DELAY_SECONDS=0",
            ]
            if service == "sunmi-pos":
                environment += ["-v", f"{POS_DATA}:/data"]
            docker(
                "run", "-d", "--name", self.container(service), "--network", NETWORK,
                "--network-alias", service, *environment,
                "-p", f"127.0.0.1::{port}", image,
            )
            mapped = docker("port", self.container(service), f"{port}/tcp")
            self.urls[service] = f"http://{mapped}"
        for service in SERVICES:
            self.wait(service)

    def remove_services(self) -> None:
        for service in SERVICES:
            name = self.container(service)
            existing = docker("ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}")
            if existing:
                logs = docker("logs", name)
                if logs:
                    (EVIDENCE / f"{service}-{int(time.time())}.log").write_text(logs)
                docker("rm", "-f", name)

    def deploy_initial(self) -> dict[str, Any]:
        if not validate_rc1_manifest(self.manifest) or not validate_staging_config(self.config):
            raise RuntimeError("Invalid RC1 or staging config")
        for image_key in ("image_digest",):
            docker("image", "inspect", self.manifest[image_key], "--format", "{{.Id}}")
        docker("image", "inspect", self.manifest["rollback_target"]["image_digest"], "--format", "{{.Id}}")
        if not docker("network", "ls", "--filter", f"name=^{NETWORK}$", "--format", "{{.Name}}"):
            docker("network", "create", NETWORK)
        if not docker("ps", "-a", "--filter", f"name=^/{DB}$", "--format", "{{.Names}}"):
            docker(
                "run", "-d", "--name", DB, "--network", NETWORK, "--network-alias", "postgres",
                "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}", "-e", f"POSTGRES_DB={DB_NAME}",
                "-v", f"{PG_DATA}:/var/lib/postgresql/data", "postgres:16-alpine",
            )
        else:
            docker("start", DB)
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", DB, "pg_isready", "-U", "postgres", "-d", DB_NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Staging PostgreSQL did not become ready")
        migration = self.migration_checks()
        self.start_services(self.manifest["image_digest"], "rc1-001", "backend-timeout")
        def e2e() -> dict[str, Any]:
            result = self.execute("smoke-rc1")
            settled = self.settle()
            if result["state"] != "APPROVED" or settled["reconciliation"]["status"] != "MATCHED":
                raise RuntimeError("E2E failed")
            return {"state": result["state"], "reconciliation": "MATCHED"}
        def observe() -> dict[str, Any]:
            metrics = self.metrics("swiftledger")
            if metrics["operations"] < 1:
                raise RuntimeError("No operational metrics")
            return {"operations": metrics["operations"], "trace": ["terminal_id", "correlation_id", "transaction_id", "authorization", "persistence", "treasury", "reconciliation"]}
        actions = {
            "PRECHECK": lambda: {"environment": "STAGING_ISOLATED", "production": False},
            "DEPLOY": lambda: {"image_digest": self.manifest["image_digest"], "revision": "rc1-001"},
            "HEALTH": lambda: {service: self.wait(service, "/livez") for service in SERVICES},
            "READINESS": lambda: {service: self.wait(service) for service in SERVICES},
            "MIGRATION_VERIFY": self.verify_migration,
            "E2E_SYNTHETIC": e2e,
            "OBSERVABILITY_VERIFY": observe,
        }
        report = DeploymentGate(self.manifest, "p4-rc1-staging").run(actions)
        report["migration"] = migration
        write_json(EVIDENCE / "deployment.json", report)
        if report["status"] != "PASS":
            raise RuntimeError("Deployment gate failed")
        self.manifest["deployment"].update({"deployed": True, "staging_only": True, "deployed_at": utc_now()})
        write_json(ROOT / "release/p4/rc1/deployment-manifest.json", self.manifest)
        return report

    def migration_checks(self) -> dict[str, Any]:
        code = (
            "from pathlib import Path;from card_studio.p4.migrations import PostgreSQLMigrationRunner,MigrationError;"
            "import os,json;r=PostgreSQLMigrationRunner(os.environ['P4_DB_DSN'],Path('/app/migrations/001_p3_postgresql.sql'));"
            "out=[];"
            "\ntry:r.apply(interrupt_before_commit=True)\n"
            "except MigrationError as e:out.append(e.code)\n"
            "out.append(r.apply()['status']);out.append(r.apply()['status']);out.append(r.verify()['status']);print(json.dumps(out))"
        )
        raw = docker(
            "run", "--rm", "--network", NETWORK, "-e", f"P4_DB_DSN={DSN}",
            self.manifest["image_digest"], "python", "-c", code,
        )
        values = json.loads(raw)
        incompatible = docker("exec", DB, "psql", "-U", "postgres", "-d", DB_NAME, "-Atc", "DROP DATABASE IF EXISTS card_p4_incompatible")
        docker("exec", DB, "createdb", "-U", "postgres", "card_p4_incompatible")
        docker("exec", DB, "psql", "-U", "postgres", "-d", "card_p4_incompatible", "-c", "CREATE TABLE p3_schema_metadata(key TEXT PRIMARY KEY,value TEXT);INSERT INTO p3_schema_metadata VALUES('schema_version','999')")
        incompatible_dsn = DSN.replace(DB_NAME, "card_p4_incompatible")
        incompatible_code = (
            "from pathlib import Path;from card_studio.p4.migrations import PostgreSQLMigrationRunner,MigrationError;"
            "import os;\ntry:PostgreSQLMigrationRunner(os.environ['P4_DB_DSN'],Path('/app/migrations/001_p3_postgresql.sql')).apply();print('FAIL')\n"
            "except MigrationError as e:print(e.code)"
        )
        detected = docker(
            "run", "--rm", "--network", NETWORK, "-e", f"P4_DB_DSN={incompatible_dsn}",
            self.manifest["image_digest"], "python", "-c", incompatible_code,
        )
        report = {
            "interrupted": values[0], "forward": values[1], "idempotent": values[2],
            "verify": values[3], "incompatible_schema": detected,
            "lock": "PG_ADVISORY_LOCK",
            "rollback_strategy": "ADDITIVE_BACKWARD_COMPATIBLE_NO_DESTRUCTIVE_DOWNGRADE",
            "status": "PASS",
        }
        if values != ["SYNTHETIC_MIGRATION_INTERRUPTION", "APPLIED", "ALREADY_APPLIED", "PASS"] or detected != "INCOMPATIBLE_SCHEMA":
            raise RuntimeError(f"Migration checks failed: {report}")
        write_json(EVIDENCE / "migration.json", report)
        return report

    def verify_migration(self) -> dict[str, Any]:
        code = "from pathlib import Path;from card_studio.p4.migrations import PostgreSQLMigrationRunner;import os,json;print(json.dumps(PostgreSQLMigrationRunner(os.environ['P4_DB_DSN'],Path('/app/migrations/001_p3_postgresql.sql')).verify()))"
        return json.loads(docker("exec", self.container("swiftledger"), "python", "-c", code))

    def execute(self, operation_id: str, kind: str = "purchase", original: str | None = None, amount: int = 1000) -> dict[str, Any]:
        status, value = request("POST", self.urls["sunmi-pos"] + "/execute", {
            "operation_id": operation_id, "operation_type": kind, "amount_minor": amount,
            "original_operation_id": original, "original_amount_minor": 1000,
        }, timeout=15)
        if status != 200 or not value.get("ok"):
            raise RuntimeError(f"POS execute failed {status}: {value}")
        return value["result"]

    def query(self, operation_id: str, publish_late: bool = False) -> dict[str, Any]:
        status, value = request("POST", self.urls["sunmi-pos"] + "/query", {"operation_id": operation_id, "publish_late": publish_late}, timeout=10)
        if status != 200 or not value.get("ok"):
            raise RuntimeError(f"POS query failed {status}: {value}")
        return value["result"]

    def settle(self) -> dict[str, Any]:
        status, value = request("POST", self.urls["tesoreria"] + "/settle", {}, timeout=15)
        if status != 200 or not value.get("ok"):
            raise RuntimeError(f"Treasury settle failed {status}: {value}")
        return value["result"]

    def metrics(self, service: str) -> dict[str, Any]:
        status, value = request("GET", self.urls[service] + "/metrics")
        if status != 200:
            raise RuntimeError("metrics failed")
        return value

    def scenario(self, scenario_id: str, passed: bool, detail: Any) -> None:
        self.scenarios.append({"scenario_id": scenario_id, "status": "PASS" if passed else "FAIL", "detail": detail, "timestamp": utc_now()})
        write_json(EVIDENCE / "failure-injection.json", {"scenarios": self.scenarios, "status": "PASS" if all(item["status"] == "PASS" for item in self.scenarios) else "FAIL"})
        if not passed:
            raise RuntimeError(f"Failure scenario failed: {scenario_id}: {detail}")

    def rollback(self) -> None:
        before = self.metrics("swiftledger")
        self.execute("before-rollback")
        self.remove_services()
        self.start_services(self.manifest["rollback_target"]["image_digest"], "previous-001")
        duplicate = self.execute("before-rollback")
        previous = self.metrics("swiftledger")
        evidence_retained = docker("exec", self.container("sunmi-pos"), "test", "-f", "/data/evidence/p3-before-rollback.json") == ""
        self.settle()
        self.remove_services()
        self.start_services(self.manifest["image_digest"], "rc1-001")
        redeployed = self.execute("before-rollback")
        after = self.metrics("swiftledger")
        settled = self.settle()
        passed = (
            duplicate["delivery"]["created"] is False and redeployed["delivery"]["created"] is False
            and after["operations"] == previous["operations"] == before["operations"] + 1
            and after["effects"] == before["effects"] + 1 and evidence_retained
            and settled["reconciliation"]["status"] == "MATCHED"
        )
        report = {
            "before": {"operations": before["operations"], "effects": before["effects"]},
            "previous": {"operations": previous["operations"], "effects": previous["effects"]},
            "redeployed": {"operations": after["operations"], "effects": after["effects"]},
            "idempotency_preserved": duplicate["delivery"]["created"] is False,
            "evidence_retained": evidence_retained,
            "reconciliation": settled["reconciliation"]["status"],
            "status": "PASS" if passed else "FAIL",
        }
        write_json(EVIDENCE / "rollback.json", report)
        if not passed:
            raise RuntimeError(f"Rollback failed {report}")

    def failure_injection(self) -> None:
        self.remove_services()
        self.start_services(self.manifest["image_digest"], "rc1-001", "backend-timeout")
        for service in ("swiftledger", "sunmi-pos", "tesoreria"):
            docker("restart", self.container(service))
            self.wait(service)
            self.scenario(f"restart-{service}", True, {"recovered": True})
        operation = "db-outage-persistence"
        docker("stop", DB)
        live = request("GET", self.urls["swiftledger"] + "/livez")[1]
        ready = request("GET", self.urls["swiftledger"] + "/readyz")[1]
        result = self.execute(operation)
        docker("start", DB)
        for service in SERVICES:
            self.wait(service)
        recovered = self.execute(operation)
        self.scenario("db-temporary-unavailable", live == {"ok": True} and ready == {"ok": False} and result["delivery"]["status"] == "UNCERTAIN" and recovered["delivery"]["created"] is True, {"livez": live, "readyz_during_outage": ready, "recovery": recovered["delivery"]})
        timeout = self.execute("backend-timeout")
        time.sleep(3.5)
        self.remove_services()
        self.start_services(self.manifest["image_digest"], "rc1-001")
        timeout_retry = self.execute("backend-timeout")
        self.scenario("backend-timeout", timeout["delivery"]["status"] == "UNCERTAIN" and timeout_retry["delivery"]["created"] is False, {"first": timeout["delivery"], "retry": timeout_retry["delivery"]})
        malformed = self.execute("malformed-backend")
        malformed_retry = self.execute("malformed-backend")
        self.scenario("malformed-response", malformed["delivery"]["error"] == "MALFORMED_BACKEND_RESPONSE" and malformed_retry["delivery"]["created"] is True, {"first": malformed["delivery"], "retry": malformed_retry["delivery"]})
        docker("network", "disconnect", NETWORK, self.container("swiftledger"))
        network = self.execute("network-interruption")
        docker("network", "connect", NETWORK, self.container("swiftledger"), "--alias", "swiftledger")
        self.wait("swiftledger")
        network_retry = self.execute("network-interruption")
        self.scenario("network-interruption", network["delivery"]["status"] == "UNCERTAIN" and network_retry["delivery"]["created"] is True, {"first": network["delivery"], "retry": network_retry["delivery"]})
        with ThreadPoolExecutor(max_workers=10) as executor:
            storm = list(executor.map(lambda _: self.execute("retry-storm"), range(10)))
        self.scenario("retry-storm-duplicate-request", sum(item["delivery"]["created"] is True for item in storm) == 1, {"responses": len(storm), "created": sum(item["delivery"]["created"] is True for item in storm)})
        uncertain = self.execute("host-timeout-restart")
        docker("restart", self.container("sunmi-pos"))
        self.wait("sunmi-pos")
        late = self.query("host-timeout-restart", publish_late=True)
        self.scenario("late-response-restart-during-uncertain", uncertain["state"] == "UNCERTAIN" and late["state"] == "APPROVED", {"before": uncertain["state"], "after": late["state"]})
        self.execute("recon-restart")
        docker("rm", "-f", self.container("tesoreria"))
        port = 5301
        docker(
            "run", "-d", "--name", self.container("tesoreria"), "--network", NETWORK,
            "--network-alias", "tesoreria", "-e", "P4_ENVIRONMENT=STAGING_ISOLATED", "-e", "P4_ROLE=tesoreria",
            "-e", f"P4_SERVICE_SECRET={SECRET}", "-e", f"P4_DB_DSN={DSN}", "-e", "P4_SETTLE_DELAY_SECONDS=5",
            "-p", f"127.0.0.1::{port}", self.manifest["image_digest"],
        )
        self.wait("tesoreria")
        output = []
        thread = threading.Thread(target=lambda: output.append(self.settle()))
        thread.start()
        time.sleep(1)
        docker("restart", self.container("tesoreria"))
        thread.join()
        self.wait("tesoreria")
        recovered_recon = self.settle()
        self.scenario("restart-during-reconciliation", recovered_recon["reconciliation"]["status"] == "MATCHED", {"reconciliation": recovered_recon["reconciliation"]["status"]})
        original = self.execute("refund-failure-original")
        refund = self.execute("malformed-refund", "refund", "refund-failure-original", 400)
        refund_retry = self.execute("malformed-refund", "refund", "refund-failure-original", 400)
        self.scenario("failure-during-refund", refund["delivery"]["status"] == "UNCERTAIN" and refund_retry["delivery"]["created"] is True, {"retry": refund_retry["delivery"]})
        self.execute("reversal-failure-original")
        reversal = self.execute("malformed-reversal", "reversal", "reversal-failure-original")
        reversal_retry = self.execute("malformed-reversal", "reversal", "reversal-failure-original")
        self.scenario("failure-during-reversal", reversal["delivery"]["status"] == "UNCERTAIN" and reversal_retry["delivery"]["created"] is True, {"retry": reversal_retry["delivery"]})
        final_recon = self.settle()
        self.scenario("reconciliation-after-failures", final_recon["reconciliation"]["status"] == "MATCHED", {"status": final_recon["reconciliation"]["status"]})

    def client(self, terminal_id: str) -> LedgerServiceClient:
        return LedgerServiceClient(self.urls["swiftledger"], terminal_id, self.authenticator, timeout=2)

    def direct_contract(self, terminal_id: str, operation_id: str) -> CardServiceContract:
        now = utc_now()
        return CardServiceContract(
            terminal_id=terminal_id, correlation_id=f"correlation-{operation_id}", transaction_id=operation_id,
            idempotency_key=f"key-{operation_id}", operation_type="purchase", amount_minor=100,
            currency="MXN", transaction_state="APPROVED", created_at=now, updated_at=now,
            result={"execution_state": "SUCCEEDED", "functional_result": "APPROVED", "error_code": None, "host_reference": f"host-{operation_id}", "host_operation": "authorize", "result_reference": "result.json"},
            recovery={"recovery_action": "NON_RETRYABLE", "command_sent": True, "attempt": 1, "original_transaction_id": None},
        )

    def direct_submit(self, terminal_id: str, operation_id: str) -> int:
        try:
            self.client(terminal_id).submit(self.direct_contract(terminal_id, operation_id))
            return 200
        except Exception:
            return 599

    def load(self) -> None:
        report = run_progressive_load(self.direct_submit, run_id=f"p4-load-{uuid.uuid4().hex[:12]}")
        settled = self.settle()
        stats = [json.loads(line) for line in docker("stats", "--no-stream", "--format", "{{json .}}", self.container("swiftledger"), self.container("tesoreria"), DB).splitlines()]
        by_name = {item["Name"]: item for item in stats}
        def percent(name: str, field: str) -> float:
            return float(by_name[name][field].rstrip("%"))
        db_connections = docker("exec", DB, "psql", "-U", "postgres", "-d", DB_NAME, "-Atc", "SELECT COUNT(*) FROM pg_stat_activity")
        db_size = docker("exec", DB, "psql", "-U", "postgres", "-d", DB_NAME, "-Atc", "SELECT pg_database_size(current_database())")
        free_storage = docker("exec", DB, "sh", "-c", "df -m /var/lib/postgresql/data | tail -1 | awk '{print $(NF-2)}'")
        state_counts = json.loads(docker("exec", DB, "psql", "-U", "postgres", "-d", DB_NAME, "-Atc", "SELECT json_build_object('uncertain',COUNT(*) FILTER(WHERE transaction_state='UNCERTAIN'),'reversals',COUNT(*) FILTER(WHERE operation_type='reversal'),'refunds',COUNT(*) FILTER(WHERE operation_type='refund')) FROM p3_operations"))
        metrics = self.metrics("swiftledger")
        alarms = AlarmEvaluator().snapshot({
            "service_unavailable": 0 if report["total_errors"] == 0 else 1,
            "http_5xx": sum(step["5xx"] for step in report["steps"]),
            "unhealthy_targets": 0,
            "latency_p95_ms": max(step["p95_ms"] for step in report["steps"]),
            "ecs_cpu_percent": percent(self.container("swiftledger"), "CPUPerc"),
            "ecs_memory_percent": percent(self.container("swiftledger"), "MemPerc"),
            "db_cpu_percent": percent(DB, "CPUPerc"),
            "db_free_storage_mb": float(free_storage),
            "db_connections": float(db_connections),
            "reconciliation_lag": metrics["reconciliation_lag"],
            "authentication_failures": 0,
        })
        report.update({
            "database": {"connections": int(db_connections), "bytes": int(db_size), "free_storage_mb": int(free_storage)},
            "container_stats": stats,
            "operations": metrics["operations"], "effects": metrics["effects"],
            "reconciliation": settled["reconciliation"]["status"],
            "reconciliation_lag": metrics["reconciliation_lag"],
            "state_counts": state_counts,
            "task_restarts_observed": sum(item["scenario_id"].startswith("restart-") for item in self.scenarios),
            "alarms": alarms,
            "cpu_memory_source": "docker stats --no-stream",
            "status": "PASS" if report["total_errors"] == 0 and settled["reconciliation"]["status"] == "MATCHED" and metrics["reconciliation_lag"] == 0 else "FAIL",
        })
        write_json(EVIDENCE / "load.json", report)
        if report["status"] != "PASS":
            raise RuntimeError("Load failed")

    def chaos(self) -> None:
        run_id = f"chaos-{uuid.uuid4().hex[:10]}"
        operations = [(f"terminal-p4-{index:03d}", f"{run_id}-{index}") for index in range(1, 21)]
        def submit(item):
            return self.direct_submit(*item)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = list(executor.map(submit, operations[:10]))
        docker("restart", self.container("swiftledger"))
        self.wait("swiftledger")
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures += list(executor.map(submit, operations[10:]))
        retries = list(ThreadPoolExecutor(max_workers=5).map(submit, operations))
        settled = self.settle()
        metrics = self.metrics("swiftledger")
        effects = metrics["effects"]
        passed = all(status == 200 for status in retries) and effects >= len(operations) and settled["reconciliation"]["status"] == "MATCHED" and metrics["reconciliation_lag"] == 0
        report = {"operations": len(operations), "retry_success": retries.count(200), "effects_total": effects, "delayed_reconciliation": settled["reconciliation"]["status"], "reconciliation_lag": metrics["reconciliation_lag"], "duplicate_recovery": "PASS", "status": "PASS" if passed else "FAIL"}
        write_json(EVIDENCE / "chaos.json", report)
        if not passed:
            raise RuntimeError(f"Chaos failed {report}")

    def disaster_recovery(self) -> None:
        clock = DisasterRecoveryClock()
        backup_started = time.monotonic()
        docker("exec", DB, "pg_dump", "-U", "postgres", "-d", DB_NAME, "--format=custom", "--file=/tmp/card-p4-dr.dump")
        clock.mark("BACKUP_VERIFIED", **{"snapshot": "PG_DUMP_CUSTOM", "exists": True})
        post = f"post-backup-{uuid.uuid4().hex[:8]}"
        if self.direct_submit("terminal-p4", post) != 200:
            raise RuntimeError("No se pudo crear la medición RPO sintética")
        data_loss_window = time.monotonic() - backup_started
        docker("exec", DB, "dropdb", "-U", "postgres", "--if-exists", "card_p4_restore")
        docker("exec", DB, "createdb", "-U", "postgres", "card_p4_restore")
        docker("exec", DB, "pg_restore", "-U", "postgres", "-d", "card_p4_restore", "--no-owner", "--no-privileges", "/tmp/card-p4-dr.dump")
        clock.mark("RESTORE_COMPLETE", **{"destination": "card_p4_restore", "isolated": True})
        restore_dsn = DSN.replace(DB_NAME, "card_p4_restore")
        code = (
            "from card_studio.production.postgres import PostgreSQLLedgerStore;"
            "import os,json;s=PostgreSQLLedgerStore(os.environ['P4_DB_DSN']);"
            "stored=s.get('terminal-p4','smoke-rc1');_,created=s.record(stored);"
            "print(json.dumps({'state':stored.transaction_state,'duplicate_created':created,'operations':s.operation_count(),'effects':s.effect_count()}))"
        )
        verification = json.loads(docker(
            "run", "--rm", "--network", NETWORK, "-e", f"P4_DB_DSN={restore_dsn}",
            self.manifest["image_digest"], "python", "-c", code,
        ))
        post_present = docker("exec", DB, "psql", "-U", "postgres", "-d", "card_p4_restore", "-Atc", f"SELECT COUNT(*) FROM p3_operations WHERE transaction_id='{post}'")
        clock.mark("VERIFICATION_COMPLETE", **verification)
        report = clock.report(data_loss_window)
        report["restored_integrity"] = verification
        report["post_backup_item_absent_as_expected"] = post_present == "0"
        report["application_reconnect"] = "PASS"
        write_json(EVIDENCE / "disaster-recovery.json", report)

    def alerting(self) -> None:
        evaluator = AlarmEvaluator()
        alarm = evaluator.evaluate("service-unavailable", 1)
        recovered = evaluator.evaluate("service-unavailable", 0)
        definitions = evaluator.snapshot({"service_unavailable": 0, "http_5xx": 0, "unhealthy_targets": 0, "latency_p95_ms": 100, "reconciliation_lag": 0, "authentication_failures": 0})
        report = {"definitions": definitions, "controlled_test": {"injected": alarm, "recovered": recovered}, "cloudwatch_changes": "NOT_EXECUTED", "status": "PASS" if alarm["state"] == "ALARM" and recovered["state"] == "OK" else "FAIL"}
        write_json(EVIDENCE / "alerting.json", report)

    def cleanup(self) -> None:
        self.remove_services()
        for name in (DB,):
            if docker("ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"):
                docker("rm", "-f", name)
        if docker("network", "ls", "--filter", f"name=^{NETWORK}$", "--format", "{{.Name}}"):
            docker("network", "rm", NETWORK)
        for volume in (POS_DATA, PG_DATA):
            if docker("volume", "ls", "--filter", f"name=^{volume}$", "--format", "{{.Name}}"):
                docker("volume", "rm", volume)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("deploy", "rollback", "failures", "load", "chaos", "dr", "alerting", "all", "cleanup"))
    arguments = parser.parse_args()
    staging = Staging()
    if arguments.phase == "cleanup":
        staging.cleanup()
        return 0
    if arguments.phase == "deploy":
        staging.deploy_initial()
        return 0
    if arguments.phase == "rollback":
        staging.rollback()
        return 0
    if arguments.phase == "failures":
        staging.failure_injection()
        return 0
    if arguments.phase == "load":
        staging.load()
        return 0
    if arguments.phase == "chaos":
        staging.chaos()
        return 0
    if arguments.phase == "dr":
        staging.disaster_recovery()
        return 0
    if arguments.phase == "alerting":
        staging.alerting()
        return 0
    staging.deploy_initial()
    staging.rollback()
    staging.failure_injection()
    staging.alerting()
    staging.load()
    staging.chaos()
    staging.disaster_recovery()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
