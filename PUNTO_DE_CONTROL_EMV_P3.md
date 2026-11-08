# Punto de control — Card Studio / Swift Ledger P3

Fecha: 2026-11-08

## Baselines históricas preservadas

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- P2: **7/7 PASS**.
- L3 hardened: **20/20 PASS** con `InternalEmvKernel`; fake `NON_CONFORMANT`.
- Baseline inicial P3: **377/377 PASS**.
- Hash P2 code/tests preservado: `e58e53b39e1156ffbb95cba5444c028d5334271d401a0a4e11d9840d45bae828`.

## Resultado P3

- P3: **15/15 PASS**.
- P3.1–P3.15: **PASS**.
- P3.11 AWS Hardening Assessment: **PASS** bajo gate de auditoría y clasificación; no es certificación de infraestructura.
- Full Regression: **429/429 PASS**; 8 pruebas PostgreSQL omitidas sin DSN en la suite normal.
- PostgreSQL aislado con DSN explícito: **16/16 PASS**, sin skips.
- Reinicio PostgreSQL real de laboratorio: **PASS**, datos preservados y pool recuperado.
- E2E sintético obligatorio: **20/20 PASS**; archivo de pruebas **21/21 PASS** incluyendo logs.
- Carga: **32/32 operaciones**, 8 terminales; efectos únicos y reconciliación `MATCHED`.
- Backup/restore aislado: **PASS**.
- Build reproducible y servicio en contenedor: **PASS**.
- Physical Validation: **DEFERRED / NOT EXECUTED**.
- Formal Certification: **NOT COMPLETED**.
- Remaining P3: **NONE**.

## Cierre P3.11

- Evidencia externa autorizada/sanitizada registrada en `evidence/aws-p3/external-assessment.json`.
- Región, ECS SUNMI/Swift Ledger, ALB, RDS security, RDS least-privilege ingress, AWS Config recorder y CloudTrail operations: **PASS**.
- El cambio RDS SG fue ejecutado previamente por el operador; el agente no lo repitió ni modificó SG.
- Mejoras abiertas: `AWS-P3-IMP-001` Flow Logs; `AWS-P3-IMP-002` IPv4 pública; `AWS-P3-IMP-003` egress amplio; `AWS-P3-IMP-004` Multi-AZ.
- IAM/root, backups inventory, alarmas, secrets metadata y restore AWS documental: **DEFERRED**, sin afirmar postura favorable ni desfavorable.
- Checklist read-only: `scripts/aws_p3_readonly_evidence.sh`; sintaxis validada y pruebas de acciones permitidas PASS.
- Ningún `BLOCKER` vigente. Uso rutinario de root, root sin MFA/access keys activas u otro hallazgo crítico documentado reabre el gate según `docs/AWS_HARDENING_P3.md`.

## Hashes y release

- P3 code/tests SHA-256: `7f1d92cdda0b3970bac00837521103176694238013dea472a3e5426c6614fc04`.
- Release source tree SHA-256: `5eab502d67964bc5686759a35b60c918f1d85dd6113923cda65919185c112858`.
- Evidencias históricas P0/P1/P2 SHA-256: `05c75d9a97bd671d8feab6ce6f93f7a8cd1fc34ffb332203bf31dd19e6fe5a35`.
- Evidencia AWS P3.11 SHA-256: `193445fbad4de1839fd59eacca58475bc25040ace69fc4ad6fc0dc720b69f129`.
- Imagen local: `sha256:a08947ffd2e341e3740995234cc072d211456a40f96a124de9fa4dc23f574c47`.
- Manifest: `release/p3/deployment-manifest.json`; `deployed=false`; regression `429/429 PASS`.

## Modificaciones P3 principales

- Paquete `card_studio/production`: contrato, HMAC/nonces, HTTP, POS, Ledger SQLite/PostgreSQL, recovery, tesorería, observabilidad, AWS assessment, backup y release.
- Pruebas `tests/test_p3_{contract,e2e,persistence,operations}.py`.
- Migración `migrations/001_p3_postgresql.sql` y lock `requirements-p3-lock.txt`.
- `Dockerfile.p3`, entrypoint sanitizado y `scripts/build_p3_release.py`.
- `integration/sunmi.py`: estado `FAILED` separado de `DECLINED`.
- `transactions/{store,service}.py`: reversal explícito de original UNCERTAIN, sin reautorizar la compra original.
- `store.py`: nombres temporales únicos para escritura atómica concurrente.
- Evidencia: `docs/EMV_P3_EXECUTION_EVIDENCE.md`, `docs/AWS_HARDENING_P3.md`, `docs/BACKUP_RECOVERY_P3.md`.

## Reanudación

P3 está cerrado. Las mejoras AWS abiertas requieren cambios humanos separados y autorizados; no ejecutar automáticamente. No reanudar PC/SC ni validación física: permanecen diferidos. No ejecutar operaciones monetarias reales, escritura física, activación real ni despliegue productivo sin autorización expresa. No iniciar P4 como parte de este cierre.
