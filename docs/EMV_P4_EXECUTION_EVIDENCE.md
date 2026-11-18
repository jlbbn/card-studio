# Card Studio / Swift Ledger P4 — execution evidence

Fecha: 2026-11-08. Evidencia `SIMULATED / STAGING_ISOLATED`, datos sintéticos. Physical Validation: **DEFERRED / NOT EXECUTED**. Formal Certification: **NOT COMPLETED**.

## Baselines y provenance

- P0 **35/35 PASS**; P1 **29/29 PASS**; P2 **7/7 PASS**; P3 **15/15 PASS**; L3 hardened **20/20 PASS**.
- Baseline inicial P4 **429/429 PASS**; regresión final **441/441 PASS**.
- P3 congelado en `release/p4/p3-baseline-freeze.json` y `PUNTO_DE_CONTROL_EMV_P4_BASELINE.md`.
- Commit/content SHA: `6ff7100fdc86d9c1d55b6a113d0a495082fcc9342d7df1642b7f1aac5f9b140f` (`CONTENT_SHA256_NOT_VCS`).
- Build hash: `e58715b17de2f43cc2ccf136b7590329677e40d373d4bd95f333a9759bf09131`.
- RC1: `sha256:06ee83091d664449271052c788f72e220fb18fe73af604cf6e9f6ded271bbbd8`.
- Rollback: `sha256:53545abd91b278962abfb1ee138bc319f20851064851ddfd388e4fb562a0a309`.
- Migration schema 1: `6a5c482514c13cbdbbe86470e9091d5fbccf68d4dfb119f11480c813467ab8c8`.
- Config hash: `6625a5ced15c366f7a96af58e4a409c2e3229a3abdf6d1c9b66aa61d5737a2ef`.

## Evidencia de ejecución

| gap_id | implementation / pruebas | integración y casos negativos | evidencia | status |
|---|---|---|---|---|
| P4.1 | Freeze JSON/documento y hashes P3 | Historia P0–P3 no reescrita | `p3-baseline-freeze.json` | PASS |
| P4.2 | `p4/manifests.py`; `build_p4_rc1.py`; `test_p4_release.py` | Mutable tag y regresión roja rechazados; dos builds idénticos | `deployment-manifest.json` | PASS |
| P4.3 | `staging/config.json`; `p4/server.py`; red/volúmenes Docker | `production=true`, endpoint externo y secreto literal rechazados | Staging real aislado | PASS |
| P4.4 | `p4/deployment.py`; `run_p4_staging.py` | Gate fallido se detiene; digest/revisiones/timestamps/correlación | `deployment.json` | PASS |
| P4.5 | `p4/migrations.py`; PostgreSQL real | Interrupción, idempotencia, lock, incompatible schema, restart | `migration.json` | PASS |
| P4.6 | Digests RC1/previous y tráfico sintético | Rollback y redeploy; duplicado devuelve `created=false`; logs/evidencia retenidos | `rollback.json` | PASS |
| P4.7 | `failure_injection()` y 13 reportes agrupados que cubren 15 fallos | Sin doble efecto/pérdida silenciosa; UNCERTAIN y recovery deterministas | `failure-injection.json` | PASS |
| P4.8 | `p4/recovery.py`; dump/restore aislado | Integridad, reconexión y replay sin efecto; RPO/RTO medidos | `disaster-recovery.json` | PASS |
| P4.9 | `p4/observability.py`; métricas HTTP/DB/Docker | Trace completo, p50/p95/p99, 4xx/5xx, restarts, DB y lag | `load.json`; `P4_OBSERVABILITY.md` | PASS |
| P4.10 | 11 alarmas y `AlarmEvaluator` | Inyección service-unavailable ALARM y recuperación OK; externo no modificado | `alerting.json` | PASS |
| P4.11 | `P4_IAM_OPERATIONAL_MODEL.md` | Root break-glass, seis roles, MFA/short-lived/no shared users | Modelo y tests IAM/secrets | PASS |
| P4.12 | `p4/security.py`; `secrets-config.json` | Metadata solamente; previous/current, nonce duplicado, scans | Rotación sintética PASS; externa NOT_EXECUTED | PASS |
| P4.13 | `p4/load.py`; 1/10/25/50/100 terminales | 186 requests, 0 errores; throughput/percentiles/CPU/memoria/DB | `load.json` | PASS |
| P4.14 | Retry storm, 20 operaciones chaos y restart bajo carga | 20/20 retries convergen; lag 0; un efecto lógico | `chaos.json` | PASS |
| P4.15 | Health durante outage/restored DB | `/livez=true`, `/readyz=false` durante DB down; recovery automático | `failure-injection.json` | PASS |
| P4.16 | Suite P3/P4 + reversal no autorizado | 12 casos security fail-closed; cero efectos rechazados | `security-regression.json` | PASS |
| P4.17 | Manifests y documentos P4 | Sanitización y hashes; `deployed=true` sólo staging | `evidence-manifest.json` | PASS |
| P4.18 | `python3 -B -m unittest discover -s tests` | Baselines preservados; security regression | **441/441 PASS** | PASS |

## Fallos inyectados — cobertura completa

1. Restart Swift Ledger — `restart-swiftledger` PASS.
2. Restart SUNMI POS — `restart-sunmi-pos` PASS.
3. Restart tesorería — `restart-tesoreria` PASS.
4. DB temporalmente no disponible — `db-temporary-unavailable` PASS.
5. Backend timeout — `backend-timeout` PASS; retry convergió `created=false`.
6. Late response — `late-response-restart-during-uncertain` PASS.
7. Malformed response — `malformed-response` PASS.
8. Network interruption — `network-interruption` PASS.
9. Retry storm — `retry-storm-duplicate-request` PASS; una creación.
10. Duplicate request — incluido en retry storm y rollback PASS.
11. Restart durante UNCERTAIN — late recovery sin segunda autorización PASS.
12. Restart durante reconciliation — cursor preservado y posterior MATCHED PASS.
13. Failure during refund — retry idempotente PASS.
14. Failure during reversal — retry idempotente PASS.
15. Failure during persistence — DB outage y posterior recuperación sin duplicado PASS.

## Mediciones operativas

- Load: 186 requests, 0 errores, 0 retries en escalones; throughput 182.316–479.928 req/s; p99 máximo 101.834 ms.
- Chaos: 20/20 retries convergentes, `delayed_reconciliation=MATCHED`, `reconciliation_lag=0`.
- Tesorería drena lotes 100/100/50; backlog cero. Remanente forzado produce `REVIEW_REQUIRED` en prueba unitaria.
- DR: RTO 1.024989 s; RPO 0.134282 s; 219 operaciones/219 efectos restaurados; `duplicate_created=false`.
- Alarmas: 11/11 `OK` en carga; inyección controlada `ALARM → OK`.
- Autoscaling: no configurado; una task por servicio en staging.

## Reproducción

```bash
python3 -B scripts/build_p4_rc1.py \
  --image-digest sha256:06ee83091d664449271052c788f72e220fb18fe73af604cf6e9f6ded271bbbd8 \
  --rollback-digest sha256:53545abd91b278962abfb1ee138bc319f20851064851ddfd388e4fb562a0a309 \
  --regression-passed 441 --regression-total 441
python3 -B scripts/run_p4_staging.py cleanup
python3 -B scripts/run_p4_staging.py all
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
```

La ejecución staging es local y crea/recrea sólo recursos `card-p4-*`. No contiene secretos reales ni operaciones monetarias. `deployed=true` está limitado a staging. No se desplegó producción, no se cambió IAM/CloudWatch/Secrets Manager externo y no se ejecutó hardware físico.

**P4: 18/18 PASS. Remaining P4: NONE.**
