# P4 — Disaster recovery evidence

Alcance: PostgreSQL 16.15 en Docker staging aislado, datos exclusivamente sintéticos. No se ejecutó restore sobre producción ni sobre el volumen fuente.

## Procedimiento ejecutado

1. Verificar backup/snapshot `pg_dump --format=custom` de `card_p4_staging`.
2. Registrar `BACKUP_VERIFIED` y crear una operación posterior al backup para medir la ventana de datos no incluida.
3. Crear la base aislada `card_p4_restore` y ejecutar `pg_restore --no-owner --no-privileges` en ella.
4. Confirmar integridad: 219 operaciones y 219 efectos; `smoke-rc1` permaneció `APPROVED`.
5. Inicializar el adaptador PostgreSQL de aplicación contra el restore y ejecutar replay del contrato persistido: `duplicate_created=false`.
6. Confirmar que el elemento posterior al backup no estaba en el restore, como se esperaba para la medición RPO.
7. Registrar `VERIFICATION_COMPLETE` y reconciliación sin duplicados.

## Mediciones finales

| Métrica | Resultado |
|---|---|
| Backup exists / snapshot | PASS |
| Restore aislado | PASS |
| Restored DB integrity | PASS — 219 operaciones / 219 efectos |
| Application reconnect | PASS |
| Synthetic transaction verification | PASS — replay sin segundo efecto |
| Start time | `2026-11-08T20:21:17.259563Z` |
| Restore complete | `2026-11-08T20:21:17.692561Z` |
| Verification complete | `2026-11-08T20:21:18.284545Z` |
| Restore seconds | **0.433013 s** |
| Measured RTO | **1.024989 s** |
| Measured data-loss window / RPO | **0.134282 s** |
| Production restore | **NO** |

Estas cifras son mediciones de un staging local pequeño, no objetivos ni garantías para AWS/RDS producción. No prueban Multi-AZ ni un restore AWS.

## Criterios de aceptación

- Backup legible y snapshot presente.
- Destino aislado, nunca sobrescritura del origen.
- Esquema y conteos válidos.
- Aplicación capaz de reconectar y leer una operación conocida.
- Replay idempotente, sin nuevo efecto financiero lógico.
- RPO/RTO medidos con timestamps.
- Evidencia sanitizada sin DSN, passwords ni secretos.

Evidencia machine-readable: `evidence/p4/rc1/disaster-recovery.json`. Runbook operativo complementario: `docs/BACKUP_RECOVERY_P3.md`.

**Disaster Recovery: PASS.**
