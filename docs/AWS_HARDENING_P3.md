# AWS hardening assessment — P3.11

Fecha: 2026-11-08. Alcance: **auditar y clasificar**, no certificar compliance ni aprobar una postura productiva completa. La evidencia externa fue autorizada y sanitizada por el operador; el agente no ejecutó llamadas AWS ni verificación independiente de cuenta. No se modificó producción, IAM, SG, secrets ni releases.

Evidencia estructurada: `evidence/aws-p3/external-assessment.json`. Provenance: `EXTERNAL_AUTHORIZED_BY_OPERATOR` / `SANITIZED`; verificación AWS independiente por el agente: `NOT_EXECUTED`.

## Resultado del gate

**P3.11: PASS.** Todos los dominios tienen estado, los hallazgos restantes están clasificados y no existe un `BLOCKER` vigente sin mitigación/documentación. Las mejoras y la evidencia read-only pendiente no se presentan como trabajo ya implementado.

`PASS` aquí significa que el assessment está completo según su gate. No significa `AWS certified`, `production certified`, `EMVCo certified`, `scheme certified` ni `PHYSICAL_VALIDATED`.

## Evidencia externa registrada

| Dominio | Estado | Evidencia sanitizada / clasificación |
|---|---|---|
| Región | PASS | Infraestructura principal verificada en `us-east-2`. |
| ECS / SUNMI | PASS | Cluster `sunmi-v3`; `sunmi-pos` y `sunmi-tesoreria` `ACTIVE`; Fargate; task definition `sunmi-v3-cobros:7`; puertos 5300/5301. |
| ECS / Swift Ledger | PASS | Cluster `swiftledger`; `swiftledger-app ACTIVE`; ECS Express Gateway; task `RUNNING`; puerto 5000; `/api/readyz` HTTP 200; target healthy. |
| ALB | PASS | HTTPS 443, default 404 y host-header routing para `pos.swiftledger.us`, `tesoreria.swiftledger.us`, `swiftledger.us` y `www.swiftledger.us`. |
| RDS security | PASS | `swiftledger-db`, PostgreSQL 16.15, puerto 5432; `PubliclyAccessible=false`; `StorageEncrypted=true`; Performance Insights; retención 7 días; Enhanced Monitoring 60s. |
| RDS least-privilege ingress | PASS | Cambio autorizado ya ejecutado por el operador: app SG egress TCP/5432 → DB SG; DB SG ingress ← app SG; ingress amplio `10.20.0.0/16` retirado; Swift Ledger permaneció HTTP 200. **No repetido ni modificado por el agente.** |
| AWS Config recorder | PASS | Recorder activo, `CONTINUOUS`, todos los tipos soportados y recursos globales. Reglas/delivery/compliance quedan en checklist read-only. |
| CloudTrail operations | PASS | Bucket de auditoría y buckets security/config verificados; event lookup operacional; sin eventos SG authorize/revoke en la ventana investigada. Configuración detallada queda en checklist. |
| VPC Flow Logs | IMPROVEMENT | `AWS-P3-IMP-001`: no se encontraron Flow Logs VPC/subnet/ENI. Evaluar habilitación con retención y destino controlados. |
| Swift Ledger public IPv4 | IMPROVEMENT / REVIEW_REQUIRED | `AWS-P3-IMP-002`: task con IP privada y IPv4 pública observada. Revisar necesidad; no se cambió configuración. |
| Application egress | IMPROVEMENT | `AWS-P3-IMP-003`: application SG conserva outbound amplio. Definir destinos y restringir mediante cambio humano autorizado; no modificar automáticamente. |
| RDS Multi-AZ | IMPROVEMENT / AVAILABILITY_GAP | `AWS-P3-IMP-004`: `MultiAZ=false`. Evaluar según RTO/RPO y ventana autorizada; no modificar producción automáticamente. |
| IAM / root | DEFERRED / READ_ONLY_EVIDENCE_REQUIRED | MFA/access keys root, usuarios, roles, Identity Center, políticas ECS/RDS y least privilege requieren la evidencia del checklist. No se afirma postura segura ni insegura. |
| Backups inventory | DEFERRED / READ_ONLY_EVIDENCE_REQUIRED | Retención 7 días registrada; window, automated backups, snapshots, deletion protection y copy-tags pendientes de inventario. |
| CloudWatch alarms | DEFERRED / READ_ONLY_EVIDENCE_REQUIRED | Monitoring RDS registrado; inventario ECS/ALB/RDS de CPU, memoria, target health, conexiones, storage, errores y 5xx pendiente. |
| Secrets metadata | DEFERRED / READ_ONLY_METADATA_REQUIRED | Nombres, rotación, last-changed y metadata KMS segura pendientes. Nunca se solicita ni registra contenido. |
| RDS restore readiness | DEFERRED / DOCUMENTARY_REVIEW | Runbook local y restore aislado de laboratorio PASS; no equivalen a restore AWS. Sólo revisión de configuración/documentación; restore AWS no ejecutado. |

## Mejoras pendientes explícitas

1. `AWS-P3-IMP-001`: habilitar/evaluar VPC Flow Logs.
2. `AWS-P3-IMP-002`: revisar IPv4 pública de la task Swift Ledger.
3. `AWS-P3-IMP-003`: restringir application egress según destinos necesarios.
4. `AWS-P3-IMP-004`: evaluar RDS Multi-AZ por disponibilidad.

Estas mejoras permanecen abiertas como trabajo operativo; no son gaps del assessment documental P3.11 y no se promueven a PASS.

## Reglas para nuevos hallazgos

- Uso rutinario de root: `BLOCKER` o `HIGH-PRIORITY IMPROVEMENT`, según frecuencia, justificación y controles demostrados.
- Root sin MFA o con access keys activas: `BLOCKER` hasta mitigación/documentación; si aparece, P3.11 debe reabrirse.
- Políticas administrativas ECS/RDS sin justificación least-privilege: `HIGH-PRIORITY IMPROVEMENT`.
- Alarma crítica confirmada ausente: `HIGH-PRIORITY IMPROVEMENT`. La ausencia de inventario no se etiqueta como alarma faltante confirmada.
- Recursos AWS Config noncompliant: clasificar por severidad y política; un blocker de seguridad sin tratamiento reabre el gate.
- Restore readiness AWS permanece `DEFERRED` hasta revisión documental; nunca se ejecuta restore productivo para cerrar este assessment.

## Checklist read-only A–H

Script: `scripts/aws_p3_readonly_evidence.sh`. Ejecutarlo únicamente con una identidad autorizada de lectura y revisar/sanitizar la salida antes de publicarla.

```bash
export AWS_REGION=us-east-2
bash scripts/aws_p3_readonly_evidence.sh
```

Variables opcionales para consultas concretas, nunca valores de secretos:

```bash
export CLOUDTRAIL_NAME='<trail-name>'
export IAM_USER='<iam-user-name>'
export POLICY_ARN='<policy-arn>'
export POLICY_VERSION='<policy-version>'
export IDENTITY_STORE_ID='<identity-store-id>'
export SECRET_ID='<secret-name-or-arn>'
export KMS_KEY_ID='<kms-key-id>'
export ALB_DIMENSION='<alb-dimension>'
```

El checklist contiene sólo operaciones `describe`, `list`, `get` y `lookup-events`, todas read-only:

- **A — IAM/root:** account summary para root MFA/access keys, usuarios, roles, Identity Center, task/execution roles y políticas ECS/RDS. No imprime credentials ni access-key IDs.
- **B — Backups:** retención, window, automated backups, snapshots, deletion protection y copy-tags.
- **C — CloudWatch:** alarmas métricas/compuestas e inventario ECS, ALB y RDS; cobertura de CPU, memoria disponible, target health, conexiones, storage, errores/5xx.
- **D — Secrets:** únicamente nombres, rotación, last-changed y metadata KMS segura. No incluye `get-secret-value`, `SecretString`, passwords, tokens, key material ni credential values.
- **E — CloudTrail:** trails, multi-region, logging, log validation, destination y última entrega/error.
- **F — AWS Config:** recorder/status, delivery channel, reglas y compliance summary.
- **G — Restore readiness:** revisión documental; no contiene comandos restore.
- **H — Root finding:** aplicar las reglas anteriores sin cambios IAM automáticos.

No se incluyen comandos create/delete/modify/update/revoke/authorize/deploy/rotate/restore/terminate. No se vuelven a tocar los SG endurecidos.

## Manejo de datos sensibles

La evidencia no incluye account IDs, IPs asignadas, ARNs completos, credenciales, secret values, key material, PAN, Track 1/2, CVV/CVC ni PIN. Los nombres de recursos publicados fueron autorizados explícitamente por el operador. Las salidas nuevas del checklist deben revisarse antes de incorporarlas.

**Remaining P3: NONE.** Physical Validation: **DEFERRED / NOT EXECUTED**. Formal Certification: **NOT COMPLETED**. Release `p3`: **NOT DEPLOYED**.
