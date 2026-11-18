# P4 — RC1 release readiness

Fecha: 2026-11-08. Alcance: release candidate y operational readiness en staging Docker aislado. No producción, hardware físico ni operaciones monetarias reales.

## RC1 inmutable

- Versión: `1.2.0-p4-rc1`.
- Commit SHA de contenido: `6ff7100fdc86d9c1d55b6a113d0a495082fcc9342d7df1642b7f1aac5f9b140f`.
- Provenance: `CONTENT_SHA256_NOT_VCS`; el directorio no es un repositorio Git y no se inventa un commit VCS.
- Build hash: `e58715b17de2f43cc2ccf136b7590329677e40d373d4bd95f333a9759bf09131`.
- Imagen: `card-studio-p4@sha256:06ee83091d664449271052c788f72e220fb18fe73af604cf6e9f6ded271bbbd8`.
- Rollback: `card-studio-p4@sha256:53545abd91b278962abfb1ee138bc319f20851064851ddfd388e4fb562a0a309`.
- Dependency lock: `requirements-p4-lock.txt`, `psycopg[binary]==3.2.12`.
- Migration: `001_p3_postgresql`, schema 1, `ADDITIVE_BACKWARD_COMPATIBLE`.
- Config: `STAGING_ISOLATED`, `production=false`, `external_endpoints=[]`, datos `SYNTHETIC_ONLY`.
- Revisiones exactas: `sunmi-pos=rc1-001`, `tesoreria=rc1-001`, `swiftledger=rc1-001`.
- Regresión: **441/441 PASS**.
- Dos builds consecutivos de RC1 produjeron el mismo digest.

## Readiness por bloque

| Bloque | Evidencia | Estado |
|---|---|---|
| P4.1 Freeze P3 | `PUNTO_DE_CONTROL_EMV_P4_BASELINE.md`; `release/p4/p3-baseline-freeze.json` | PASS |
| P4.2 RC1 | Deployment manifest, lock, build/source/image hashes y rollback target | PASS |
| P4.3 Staging | Red privada, tres servicios y PostgreSQL; sin endpoints productivos | PASS |
| P4.4 Deployment gate | PRECHECK → DEPLOY → HEALTH → READINESS → MIGRATION_VERIFY → E2E → OBSERVABILITY | PASS |
| P4.5 Migrations | Interrupción/rollback, forward, idempotencia, advisory lock, restart e incompatible schema | PASS |
| P4.6 Rollback | RC1 → previous → RC1 con tráfico, idempotencia, evidencia y reconciliación preservadas | PASS |
| P4.7 Failure injection | Restarts, DB, timeout, late/malformed response, red, storm, duplicados, UNCERTAIN, reconciliation, refund/reversal/persistencia | PASS |
| P4.8 Disaster recovery | Backup/restore aislado, integridad y replay; RTO 1.024989 s, RPO 0.134282 s | PASS |
| P4.9 Observability | Trazabilidad completa, métricas y load report; reconciliation lag 0 | PASS |
| P4.10 Alerting | 11 definiciones y prueba `ALARM → OK`; CloudWatch externo no modificado | PASS |
| P4.11 IAM model | Root break-glass y seis roles least-privilege; IAM externo no modificado | PASS |
| P4.12 Secrets/config | Metadata, scans PASS, rotación previous/current y nonce; rotación externa no ejecutada | PASS |
| P4.13 Load/performance | 1/10/25/50/100 terminales; 186 requests, 0 errores | PASS |
| P4.14 Concurrency/chaos | Restart bajo carga, retries, duplicados y reconciliación; un efecto lógico | PASS |
| P4.15 Readiness/liveness | DB down: livez=true/readyz=false; DB restored: readyz=true | PASS |
| P4.16 Security regression | 12 casos negativos, incluidos refund y reversal no autorizados | PASS |
| P4.17 Evidence | Manifests y documentos P4 con hashes | PASS |
| P4.18 Regression | **441/441 PASS**; baseline P0–P3 preservado | PASS |

## Condiciones y límites

- `deployed=true` significa únicamente staging aislado; `production=false` y `staging_only=true`.
- Autoscaling no configurado en staging: una task por servicio. No se afirma escalado ECS.
- CloudWatch, IAM y Secrets Manager externos no fueron modificados. Las pruebas de alarmas y rotación son locales/staging.
- RPO/RTO son mediciones locales, no objetivos de producción.
- Las pruebas destructivas se limitaron a recursos staging creados para P4.
- Physical Validation: **DEFERRED / NOT EXECUTED**.
- Formal Certification: **NOT COMPLETED**.

**P4: 18/18 PASS. Remaining P4: NONE.**
