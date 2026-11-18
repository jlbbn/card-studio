# Punto de control — Card Studio / Swift Ledger P4

Fecha: 2026-11-08

## Baselines preservadas

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- P2: **7/7 PASS**.
- P3: **15/15 PASS**.
- L3 Hardened Matrix: **20/20 PASS** con `InternalEmvKernel`.
- Baseline inicial P4: **429/429 PASS**.
- Full Regression final: **441/441 PASS**.
- Physical Validation: **DEFERRED / NOT EXECUTED**.
- Formal Certification: **NOT COMPLETED**.

## Resultado P4

- P4: **18/18 PASS**.
- RC1 Build: **PASS**, reproducible.
- Staging Deployment: **PASS**, siete gates.
- Database Migration: **PASS** — forward/idempotent/lock/interruption/restart/incompatible detection.
- Rollback: **PASS** — RC1 → previous → RC1 con idempotencia y evidencia preservadas.
- Failure Injection: **PASS** — 15 fallos cubiertos.
- Disaster Recovery: **PASS** — RTO 1.024989 s, RPO 0.134282 s.
- Observability: **PASS** — traza completa y reconciliation lag 0.
- Alerting: **PASS** — 11 alarmas y prueba ALARM → OK.
- IAM Operational Model: **PASS** documental/staging; IAM externo no modificado.
- Secrets/Config: **PASS** — metadata/scans/rotación sintética; externa no ejecutada.
- Load/Performance: **PASS** — 186 requests, 0 errores, escalones 1/10/25/50/100.
- Concurrency/Chaos: **PASS** — 20/20 retries convergentes, un efecto lógico.
- Readiness/Liveness: **PASS** — DB down y recuperación correctos.
- Security Regression: **PASS** — 12 casos, incluidos refund/reversal no autorizados.
- Remaining P4: **NONE**.

## RC1 y rollback

- Versión: `1.2.0-p4-rc1`.
- Content commit SHA: `6ff7100fdc86d9c1d55b6a113d0a495082fcc9342d7df1642b7f1aac5f9b140f` (`CONTENT_SHA256_NOT_VCS`).
- Build hash: `e58715b17de2f43cc2ccf136b7590329677e40d373d4bd95f333a9759bf09131`.
- RC1 digest: `sha256:06ee83091d664449271052c788f72e220fb18fe73af604cf6e9f6ded271bbbd8`.
- Rollback digest: `sha256:53545abd91b278962abfb1ee138bc319f20851064851ddfd388e4fb562a0a309`.
- Migration: schema 1, `001_p3_postgresql`, additive/backward-compatible.
- Revisiones: `rc1-001` para `sunmi-pos`, `tesoreria`, `swiftledger`.
- Deployment: **STAGING_ISOLATED ONLY**, `production=false`, `staging_only=true`.

## Evidencia

- `release/p4/rc1/deployment-manifest.json`.
- `release/p4/rc1/evidence-manifest.json`.
- `evidence/p4/rc1/{deployment,migration,rollback,failure-injection,disaster-recovery,alerting,load,chaos,secrets-config,security-regression}.json`.
- `docs/{EMV_P4_EXECUTION_EVIDENCE,P4_RELEASE_READINESS,P4_DISASTER_RECOVERY,P4_OBSERVABILITY,P4_IAM_OPERATIONAL_MODEL}.md`.
- `PUNTO_DE_CONTROL_EMV_P4_BASELINE.md` y `release/p4/p3-baseline-freeze.json`.

## Reanudación

P4 está cerrado. No iniciar P5 como parte de este checkpoint. Mantener validación física diferida y certificación formal no completada. Producción, IAM externo, CloudWatch, Secrets Manager y rotación real no fueron modificados. Cualquier despliegue productivo futuro requiere autorización humana y evidencia independiente.
