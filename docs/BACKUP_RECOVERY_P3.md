# Backup / recovery P3

## Backup policy

La backup policy exige copias diarias cifradas, acceso mínimo y manifiesto SHA-256 sin secretos. Los datos de laboratorio son sintéticos. La política objetivo para PostgreSQL usa snapshots administrados y backup continuo cuando esté disponible.

## Retention

Retención mínima verificada por software: 35 días; el validador admite 7–365. La retención real de RDS queda pendiente de evidencia AWS.

## Snapshots

Los snapshots deben ser cifrados y etiquetados con versión, procedencia y fecha. Un snapshot no se considera recuperación probada hasta un restore aislado.

## Restore procedure

1. Congelar la versión y verificar digest del backup.
2. Crear un destino aislado vacío; nunca sobrescribir producción.
3. Restaurar con la cuenta de menor privilegio suficiente.
4. Aplicar la migration gate sólo si el esquema previo es compatible.
5. Verificar tablas, conteos, constraints, idempotencia y ausencia de secretos.
6. Ejecutar la regression gate contra el entorno aislado.
7. Registrar evidencia sanitizada y destruir únicamente el entorno temporal autorizado.

`card_studio/production/backup.py` implementa restore SQLite a un archivo nuevo y validación de esquema/conteos. PostgreSQL se verifica con dump/restore en bases aisladas del contenedor de laboratorio; esto no es un restore AWS.

## DB recovery

DB recovery exige reconexión con espera acotada, pool que descarte conexiones rotas y persistencia de transaction status, idempotency, recovery, reversal, refund y reconciliation. No hay estado financiero obligatorio sólo en RAM.

## Service redeploy

Service redeploy requiere el manifiesto `release/p3/deployment-manifest.json`, digest de imagen, entorno validado, secreto inyectado fuera de URL/logs y regresión verde. Arrancar primero en aislamiento y comprobar `/livez` y `/readyz`.

## Task replacement

Task replacement debe mantener volumen/DB durable, reemplazar una tarea cada vez, esperar readiness y conservar la tarea previa hasta comprobar contrato e idempotencia. No se ejecutó reemplazo ECS.

## Rollback

1. Detener el despliegue nuevo si falla migration gate, regression gate o readiness.
2. Conservar DB y evidencia; no ejecutar rollback destructivo de datos.
3. Redesplegar `rollback.image_digest` del manifiesto previo válido.
4. Verificar compatibilidad de esquema antes de arrancar.
5. Ejecutar health, consulta de una operación conocida y reconciliación.
6. Registrar el resultado; cualquier discrepancia queda `REVIEW_REQUIRED`.

## Migration gate

La migration gate exige `migrations/001_p3_postgresql.sql`, digest coincidente, transacción exitosa y constraints presentes. Una migración desconocida o parcial bloquea release.

## Regression gate

La regression gate exige todos los tests históricos y P3 verdes, incluidos P0/P1/P2 y L3 hardened. Un test omitido por dependencia no se cuenta como ejecución PostgreSQL; la matriz PostgreSQL aislada se ejecuta con DSN explícito y se registra por separado.
