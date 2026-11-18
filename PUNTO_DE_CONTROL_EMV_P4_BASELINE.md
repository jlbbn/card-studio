# Punto de control — baseline inmutable P3 para P4

Fecha: 2026-11-08. Este checkpoint congela la entrada a P4; no reescribe evidencia P0–P3.

## Baseline

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- P2: **7/7 PASS**.
- P3: **15/15 PASS**.
- L3 Hardened Matrix: **20/20 PASS** con `InternalEmvKernel`.
- Full Regression: **429/429 PASS**.
- Physical Validation: **DEFERRED / NOT EXECUTED**.
- Formal Certification: **NOT COMPLETED**.

## Provenance y release P3

No existe repositorio VCS en el directorio; no se inventa un commit Git. Provenance de contenido: `tree-sha256:5eab502d67964bc5686759a35b60c918f1d85dd6113923cda65919185c112858`.

- Manifest: `release/p3/deployment-manifest.json`.
- Imagen: `sha256:a08947ffd2e341e3740995234cc072d211456a40f96a124de9fa4dc23f574c47`.
- DB schema version: **1**.
- Migration state: **001_p3_postgresql PASS**.
- Regression gate: **429/429 PASS**.
- Deployment P3: **NOT DEPLOYED**.

## Hashes congelados

| Artefacto | SHA-256 |
|---|---|
| `release/p3/deployment-manifest.json` | `67c853d6f4a9c64e47c9446b782269f636d5ead8ce60651c62d1ec21778c74cc` |
| `migrations/001_p3_postgresql.sql` | `6a5c482514c13cbdbbe86470e9091d5fbccf68d4dfb119f11480c813467ab8c8` |
| `docs/EMV_P3_EXECUTION_EVIDENCE.md` | `aa5b74fc00685f379098c49949b6af5dc2fb3eb3b13eeb48b7a76502401aba59` |
| `docs/AWS_HARDENING_P3.md` | `ed61e88b32b69ebe4d1acf1d98f69326dc707b021e2ef1e96a17f156c907ae38` |
| `PUNTO_DE_CONTROL_EMV_P3.md` | `ac1c29510f1dffd0070d99c1b1d936bc015c2c409c54087de7d2798ad70ea6ed` |
| `evidence/aws-p3/external-assessment.json` | `193445fbad4de1839fd59eacca58475bc25040ace69fc4ad6fc0dc720b69f129` |

Copia machine-readable: `release/p4/p3-baseline-freeze.json`. Cualquier cambio P4 se registra en artefactos nuevos; los hashes anteriores no se actualizan para aparentar estabilidad.
