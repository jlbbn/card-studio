# Card Studio / Swift Ledger P3 — execution evidence

Fecha: 2026-11-08. La evidencia local es `SIMULATED` / `LAB_ONLY`; la evidencia AWS es `EXTERNAL_AUTHORIZED_BY_OPERATOR` / `SANITIZED`, sin verificación AWS independiente por el agente. No hubo operaciones monetarias reales. Physical Validation: **DEFERRED / NOT EXECUTED**. Formal Certification: **NOT COMPLETED**.

## Baselines preservados

- P0: **35/35 PASS**.
- P1: **29/29 PASS**.
- P2 integración: **7/7 PASS**.
- L3 hardened matrix: **20/20 PASS**, usando `InternalEmvKernel`; fake `LabKernelAdapter`: `NON_CONFORMANT`.
- Baseline inicial P3: **377/377 PASS**.
- Regresión final P3: **429/429 PASS**; las 8 pruebas PostgreSQL se omiten en la ejecución sin DSN, no se cuentan como ejecución PostgreSQL.
- Matriz PostgreSQL explícita: **16/16 PASS** (8 SQLite + 8 PostgreSQL), sin skips al proporcionar el DSN aislado.
- Hash P3 de `card_studio/**/*.py` y `tests/**/*.py`: `7f1d92cdda0b3970bac00837521103176694238013dea472a3e5426c6614fc04`.
- Hash del árbol de release: `5eab502d67964bc5686759a35b60c918f1d85dd6113923cda65919185c112858`.
- Hash de evidencias históricas P0/P1/P2: `05c75d9a97bd671d8feab6ce6f93f7a8cd1fc34ffb332203bf31dd19e6fe5a35`.
- Hash de evidencia AWS P3.11: `193445fbad4de1839fd59eacca58475bc25040ace69fc4ad6fc0dc720b69f129`.

No se reescribieron resultados históricos P0/P1/P2.

## Cierre por bloque

| gap_id | implementation | tests / negative cases | integration y evidencia | status |
|---|---|---|---|---|
| P3.1 | `production/{contract,security,http,store}.py` | `test_p3_contract.py`: auth válida/inválida, replay, timestamp, nonce, payload, firma, correlación | HTTP service-to-service real; HMAC SHA-256; secretos fuera de URL/logs; contrato con terminal/correlación/transacción/idempotencia/tipo/importe/moneda/estado/timestamps/resultado/recovery | PASS |
| P3.2 | `integration/sunmi.py`; `production/contract.py` | E2E approved/declined/failed/uncertain/reversed; transición inválida histórica | CARD separado de CASH; se añade `FAILED` documentado; `FAILED != DECLINED`; post-send timeout permanece `UNCERTAIN` | PASS |
| P3.3 | `production/{pos,treasury,recovery}.py`; Internal kernel/host/readers existentes | `test_p3_e2e.py`: 20 escenarios obligatorios + logs | Synthetic terminal → SUNMI POS → CARD → Swift Ledger HTTP → host/result → persistencia → POS → tesorería → reconciliación → evidencia | PASS |
| P3.4 | Constraints y transacciones en `production/{store,postgres}.py` | Duplicados en instancia/tasks/procesos P1, restart, retry, concurrencia y delayed messages | Una clave lógica produce como máximo un efecto; estado durable; replay converge; conflicto falla cerrado | PASS |
| P3.5 | `production/recovery.py`; query/resolve en store/HTTP/POS | Timeout post-send, late result, restart, duplicate suppression, reversal tras incierto | `SAFE_TO_RETRY`, `QUERY_REQUIRED`, `READ_BACK_REQUIRED`, `OPERATOR_REVIEW`, `NON_RETRYABLE`; nunca UNCERTAIN → DECLINED automático | PASS |
| P3.6 | Reglas originales en `transactions/{store,service}.py` y `production/{store,postgres}.py` | Refund original/límite/duplicado/acumulado/carrera; reversal correlación/duplicado/restart/late/incierto | Límites atómicos; un efecto por operación; reversal de compra incierto sin segunda autorización de la original | PASS |
| P3.7 | `production/treasury.py`; tablas cursor/consumed/reconciliation | Cursor, duplicados, ordering, restart, missing, discrepancy, late | `MATCHED`, `MISSING`, `DUPLICATE`, `LATE`, `MISMATCH`, `REVIEW_REQUIRED`; discrepancias persistidas, nunca corregidas silenciosamente | PASS |
| P3.8 | `production/postgres.py`; `migrations/001_p3_postgresql.sql`; pool | PostgreSQL 16 aislado: restart, boundaries, unique/idempotency, carreras, partial failure, reconnect y pool recovery | 16/16 matriz común; reintento acotado sólo de transacciones DB abortadas por serialización; datos preservados tras reinicio real del contenedor | PASS |
| P3.9 | `production/http.py`; `server.py` | `/livez` y `/readyz` positivo/DB no disponible/malformed health | Respuestas públicas exactamente `{"ok":bool}`; sin hostname DB, usuarios, stack, secretos, topology o IPs | PASS |
| P3.10 | `production/observability.py`; audit persistente | Logs estructurados, correlación E2E y campos prohibidos | service/operation/state/correlation/transaction/elapsed/outcome; enlace authorization → persistence → treasury → reconciliation | PASS |
| P3.11 | `production/aws.py`; `docs/AWS_HARDENING_P3.md`; `evidence/aws-p3/external-assessment.json`; `scripts/aws_p3_readonly_evidence.sh` | `test_p3_operations.py::test_aws_external_evidence_closes_gate_with_classified_follow_up`; dominios, estados, blockers y acciones read-only | Evidencia externa autorizada/sanitizada registrada; RDS least-privilege ingress PASS; 4 IMPROVEMENT y pendientes DEFERRED clasificados; ningún BLOCKER vigente; assessment PASS sin afirmar compliance | PASS |
| P3.12 | `production/backup.py`; `docs/BACKUP_RECOVERY_P3.md` | Manifest/retención/restore SQLite; dump/restore PostgreSQL; runbook | Restore aislado PostgreSQL `PASS`, 4 operaciones/4 efectos preservados; sin restore productivo; redeploy/task replacement/rollback documentados | PASS |
| P3.13 | `production/release.py`; `Dockerfile.p3`; `scripts/build_p3_release.py`; lock/manifest | Digest inválido, regresión roja, mismatch, rollback, build repetido | Build reproducible `PASS`; manifest y migration/regression gates; imagen `sha256:a08947ffd2e341e3740995234cc072d211456a40f96a124de9fa4dc23f574c47`; `deployed=false` | PASS |
| P3.14 | `test_p3_operations.py`; stores transaccionales | 8 terminales, 32 compras paralelas, duplicados, retries, refunds/reversals y reconciliación concurrente | 32 operaciones/32 efectos únicos; 8 consumers con límite 4; consumo idempotente; `MATCHED`; sin dinero real | PASS |
| P3.15 | Validadores contract/security/store y alcances por terminal | Auth, replay, tamper, wrong terminal/correlation, stale/nonce, refund/reversal no autorizados, importe/moneda/estado inválidos | Fallo cerrado; sin efecto en casos rechazados; aislamiento cross-terminal | PASS |

**P3: 15/15 PASS; P3.11 AWS Hardening Assessment PASS con mejoras y evidencia read-only diferida explícitas.**

## Matriz E2E obligatoria

| Caso | Resultado | Invariante verificada |
|---|---|---|
| 1 APPROVED | PASS | Efecto único y reconciliación MATCHED |
| 2 DECLINED | PASS | Decisión funcional DECLINED; sin efecto |
| 3 timeout antes de enviar | PASS | FAILED + SAFE_TO_RETRY; no DECLINED |
| 4 timeout después de enviar | PASS | UNCERTAIN + QUERY_REQUIRED; sin retry automático |
| 5 late response | PASS | Reconcilia a final sin nueva autorización |
| 6 duplicate transaction | PASS | Misma respuesta, una operación/efecto |
| 7 concurrent duplicate | PASS | Un ganador durable; convergencia |
| 8 application restart | PASS | Persistencia y sin segundo envío |
| 9 backend restart | PASS | Misma DB e idempotencia tras recrear HTTP |
| 10 host unavailable | PASS | Clasificación antes de envío, no decline |
| 11 database unavailable | PASS | Entrega incierta; sync posterior sin repetir kernel |
| 12 malformed backend response | PASS | Fallo cerrado; estado CARD no se degrada a DECLINED |
| 13 authentication failure | PASS | INVALID_SIGNATURE; cero operaciones remotas |
| 14 reconciliation delayed | PASS | Cursor posterior sin duplicados |
| 15 duplicate reversal | PASS | Un reversal y convergencia |
| 16 refund | PASS | Efecto firmado y neto correcto |
| 17 refund over limit | PASS | EXCESSIVE_REFUND; sin efecto excedente |
| 18 duplicate refund | PASS | Un host refund y un efecto |
| 19 reversal after uncertain | PASS | Sin crédito tardío duplicado tras reversal |
| 20 recovery after restart | PASS | Late result sin segunda autorización |

## Evidencia de contenedor y release

- Dos builds consecutivos `docker build --provenance=false -f Dockerfile.p3` produjeron el mismo digest de imagen.
- Contenedor local no root: `/livez {"ok":true}`, `/readyz {"ok":true}`, contrato firmado `RECORDED`, `created=true`, `simulation=true`.
- Logs del contenedor sin secreto ni traceback.
- Manifest: `release/p3/deployment-manifest.json`, `regression_gate=429/429 PASS`, migration gate `PASS`, rollback digest fijado, `deployed=false`.
- No se desplegó una release ni el agente cambió infraestructura externa. El cambio RDS SG registrado fue autorizado y ejecutado previamente por el operador.

## Comandos reproducibles

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
P3_TEST_POSTGRES_DSN='postgresql://postgres:[SYNTHETIC_ONLY]@127.0.0.1:<puerto-aislado>/card_p3' \
  PYTHONDONTWRITEBYTECODE=1 /tmp/card-studio-p3-venv/bin/python -B \
  -m unittest discover -s tests -p 'test_p3_persistence.py' -v
python3 -B scripts/build_p3_release.py \
  --image-digest sha256:a08947ffd2e341e3740995234cc072d211456a40f96a124de9fa4dc23f574c47 \
  --regression-passed 429 --regression-total 429
```

El DSN real de laboratorio no se conserva en evidencia. No contiene credenciales productivas.

## Mejoras y seguimiento — sin bloqueos P3

- `AWS-P3-IMP-001`: VPC Flow Logs ausentes — `IMPROVEMENT`.
- `AWS-P3-IMP-002`: IPv4 pública en task Swift Ledger — `IMPROVEMENT / REVIEW_REQUIRED`.
- `AWS-P3-IMP-003`: application egress amplio — `IMPROVEMENT`.
- `AWS-P3-IMP-004`: RDS `MultiAZ=false` — `IMPROVEMENT / AVAILABILITY_GAP`.
- IAM/root, backups inventory, alarmas, secrets metadata, CloudTrail/Config detalle y restore documental: `DEFERRED`, con checklist estrictamente read-only en `scripts/aws_p3_readonly_evidence.sh`.

**Remaining P3: NONE.** Las mejoras permanecen abiertas y no se afirma que estén implementadas. El gate P3.11 es de auditoría y clasificación, no de compliance productiva.

Physical Validation: **DEFERRED / NOT EXECUTED**. No se ejecutó PC/SC, Rocketek, Java Card, SUNMI físico ni escritura física. Formal Certification: **NOT COMPLETED**.
