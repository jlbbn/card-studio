from __future__ import annotations

from typing import Any, Mapping


CONTROLS = (
    ("IAM least privilege", "iam_least_privilege"),
    ("Root usage", "root_usage_disabled"),
    ("ECS task roles", "ecs_task_role_scoped"),
    ("ECS execution roles", "ecs_execution_role_scoped"),
    ("Security groups", "security_groups_scoped"),
    ("Task public IP", "task_public_ip_disabled"),
    ("Egress", "egress_restricted"),
    ("RDS security", "rds_private_encrypted"),
    ("RDS Multi-AZ", "rds_multi_az"),
    ("Backups", "backups_enabled"),
    ("Restore readiness", "restore_tested"),
    ("CloudWatch", "cloudwatch_enabled"),
    ("Alarms", "alarms_configured"),
    ("Secrets rotation", "secrets_rotation_enabled"),
    ("CloudTrail", "cloudtrail_enabled"),
    ("AWS Config", "aws_config_enabled"),
)


def assess_aws(configuration: Mapping[str, Any]) -> list[dict[str, str]]:
    findings = []
    for name, key in CONTROLS:
        value = configuration.get(key)
        if value is True:
            status = "PASS"
            detail = "Control afirmativo aportado para revisión; requiere evidencia de cuenta para validación externa."
        elif value is False:
            status = "BLOCKER" if key in {"root_usage_disabled", "rds_private_encrypted", "backups_enabled"} else "IMPROVEMENT"
            detail = "La configuración aportada no cumple el control."
        elif value == "DEFERRED":
            status = "DEFERRED"
            detail = "Control aplazado explícitamente; no cuenta como aprobación productiva."
        else:
            status = "BLOCKER"
            detail = "Sin evidencia de cuenta/configuración; no se puede cerrar el control."
        findings.append({"control": name, "status": status, "detail": detail})
    return findings


def assessment_status(findings: list[dict[str, str]]) -> str:
    if findings and all(item["status"] == "PASS" for item in findings):
        return "PASS"
    if any(item["status"] == "BLOCKER" for item in findings):
        return "PARTIAL"
    return "PARTIAL"
