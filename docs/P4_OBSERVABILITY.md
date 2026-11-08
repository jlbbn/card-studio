# P4 — Observability, metrics and alerting

Alcance: staging aislado, servicios `sunmi-pos`, `tesoreria`, `swiftledger` y PostgreSQL. Logs estructurados y evidencia sin secretos.

## Trazabilidad extremo a extremo

El deployment gate `OBSERVABILITY_VERIFY` pasó con la cadena:

`terminal_id → correlation_id → transaction_id → authorization → persistence → treasury → reconciliation`.

Cada operación E2E conserva contrato firmado, host reference, estado persistente, efecto de tesorería y reconciliación. Los identificadores de release son `image_digest=sha256:06ee83091d664449271052c788f72e220fb18fe73af604cf6e9f6ded271bbbd8`, revisiones `rc1-001` y `correlation_id=p4-rc1-staging`.

## Métricas y fuentes

- Request rate, throughput y latencias p50/p95/p99: mediciones cliente HTTP.
- 4xx/5xx/errores/retries: respuestas service-to-service y reintentos de chaos.
- Unhealthy targets/task restarts: health checks y escenarios de restart.
- DB connections: `pg_stat_activity`.
- DB CPU/memoria y contenedores: `docker stats --no-stream`.
- DB storage: `pg_database_size` y espacio disponible del volumen staging.
- Reconciliation lag: efectos menos consumos durables del cursor.
- UNCERTAIN/refund/reversal: consulta agregada por `transaction_state` / `operation_type`.
- Autoscaling: `FIXED_SINGLE_TASK_STAGING_NO_AUTOSCALING_CONFIGURED`; no se afirma comportamiento autoscaling.

## Carga progresiva final

| Terminales | Requests | Throughput req/s | p50 ms | p95 ms | p99 ms | Errors | Retries |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 182.316 | 5.193 | 5.193 | 5.193 | 0 | 0 |
| 10 | 10 | 265.307 | 16.091 | 30.768 | 34.997 | 0 | 0 |
| 25 | 25 | 479.928 | 14.065 | 31.818 | 43.051 | 0 | 0 |
| 50 | 50 | 423.720 | 12.109 | 55.694 | 72.365 | 0 | 0 |
| 100 | 100 | 479.678 | 13.395 | 48.907 | 101.834 | 0 | 0 |

Total: **186 requests, 0 errors, reconciliation MATCHED, lag 0**. CPU/memoria medidos: Ledger 0.01%/0.06%, PostgreSQL 0.02%/0.06%. Conexiones DB 12, DB size 9,477,143 bytes, free storage 502,009 MB. Los valores son una muestra local, no benchmarks producción ni SLOs.

## Alarmas definidas

`card_studio/p4/observability.py` define 11 alarmas:

1. Service unavailable: valor >0, CRITICAL.
2. ALB 5xx: valor >0.
3. Target unhealthy: valor >0, CRITICAL.
4. High latency: p95 >2000 ms.
5. ECS CPU >80%.
6. ECS memory >85%.
7. RDS CPU >80%.
8. RDS free storage <1024 MB.
9. DB connections >80.
10. Reconciliation backlog >10.
11. Repeated authentication failures >5.

La evaluación de carga dejó las 11 en `OK`. Un hallazgo `INSUFFICIENT_DATA` es explícito y nunca se convierte en `OK`.

## Prueba controlada

`service-unavailable` se inyectó con valor 1 → `ALARM`; recuperación con valor 0 → `OK`. Evidencia: `evidence/p4/rc1/alerting.json`. No se crearon ni modificaron alarmas CloudWatch externas. Las métricas Docker staging alimentan los mismos contratos de evaluación, sin afirmar cobertura CloudWatch.

## Readiness y health

Durante DB unavailable, `/livez` permaneció `{"ok":true}` y `/readyz` devolvió `{"ok":false}` con HTTP 503. Al restaurar DB, readiness se recuperó automáticamente. Las respuestas públicas no incluyen hostname, usuario, IP, topology, stack trace ni secretos.

**Observability: PASS. Alerting: PASS en staging; CloudWatch externo NOT MODIFIED.**
