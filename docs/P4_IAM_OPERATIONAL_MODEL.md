# P4 — IAM operational model

Alcance: modelo recomendado y auditado documentalmente para operación. No se creó, modificó ni eliminó IAM externo. La evidencia AWS P3.11 sigue siendo externa autorizada; los inventarios root/IAM pendientes continúan `DEFERRED` hasta lectura autorizada. Este documento no afirma que las políticas reales ya cumplan el modelo.

## Principios obligatorios

- `ROOT = break-glass only`, con MFA resistente a phishing, custodia documentada y alarma para cualquier uso.
- Sin usuarios compartidos; identidades individuales mediante Identity Center cuando esté disponible.
- Credenciales cortas y roles asumidos; MFA para operadores humanos.
- Sin access keys de root ni uso rutinario de root.
- Sin secretos en shell history, URLs, manifests, logs o evidencias.
- Políticas con acciones, recursos y condiciones mínimas; sin `Action=*` / `Resource=*` salvo excepción documentada y aprobada.
- Separación entre despliegue, runtime, seguridad, DB e incidentes; ninguna persona necesita todos los roles.

## Roles

| Rol | Finalidad | Permisos máximos recomendados | Restricciones |
|---|---|---|---|
| `ReadOnlyAudit` | Evidencia y assessment | `Describe/List/Get` en ECS, RDS, ALB, IAM, CloudTrail, Config, CloudWatch, Secrets Manager metadata y KMS metadata | Sin contenido de secretos, sin mutaciones, sin `GetSecretValue`. |
| `DeployOperator` | RC y rollback en staging | Registro de imágenes sólo lectura, actualización de services/task revisions aprobadas, lectura de manifests y logs | Sin IAM, SG, RDS, secrets ni producción; requiere digest y manifest. |
| `RuntimeOperator` | Ejecución de aplicación | Task role con acceso únicamente a DB y recursos de aplicación necesarios | Sin administración de infraestructura ni lectura de secretos fuera del secreto asignado. |
| `SecurityAdmin` | Guardrails y respuesta de seguridad | Revisión de IAM/Config/CloudTrail/KMS y cambios de seguridad aprobados | Sin despliegue ni operaciones DB; cambios con MFA y ticket. |
| `DBOperator` | Backup/migración/recovery | Lectura de RDS, snapshots y restore en destinos aislados autorizados | Sin restore sobre producción, sin modificación SG/IAM, sin secretos de aplicación. |
| `IncidentResponder` | Contención y recuperación | Restart controlado, lectura de health/logs/métricas, ejecución de runbooks aprobados | Sin cambios de política, rotación o restauración fuera del incidente autorizado. |

## Condiciones mínimas

- `aws:MultiFactorAuthPresent=true` para acciones humanas sensibles.
- `aws:PrincipalArn` limitado al rol previsto; `aws:RequestedRegion` sólo regiones autorizadas.
- Recursos ECS/RDS/Secrets limitados por ARN y tags de entorno; producción separada de staging por cuenta o boundary.
- Permisos de lectura de secretos limitados a `DescribeSecret` y metadata KMS; nunca incluir valores en evidencia.
- `DeployOperator` debe exigir `image@sha256:<digest>` y task revision exacta; tags mutables no bastan.
- Sesiones de incidente con duración mínima, registro CloudTrail y revisión posterior.

## Flujos operativos

### Break-glass

1. Verificar incidente y autorización humana.
2. Recuperar acceso root custodiado con MFA.
3. Registrar motivo, inicio, fin y acciones.
4. Resolver el bloqueo mínimo y volver a roles operativos.
5. Revisar CloudTrail y cerrar acceso. Uso rutinario sería `BLOCKER` o `HIGH-PRIORITY IMPROVEMENT` según evidencia P3.11.

### Deploy / rollback

`DeployOperator` valida manifest, regresión, digest, entorno y compatibilidad de migración. Rollback utiliza el digest previo; no requiere privilegios IAM, SG o RDS adicionales. Toda mutación de IAM queda fuera del deploy.

### Backup / restore

`DBOperator` verifica backup y ejecuta restore sólo en un destino aislado autorizado. `IncidentResponder` valida health y reconciliación. Ningún rol puede restaurar encima de producción por defecto.

### Secret rotation

`SecurityAdmin` prepara metadata y ventana; runtime admite previous/current durante overlap; `RuntimeOperator` verifica readiness y replay de nonce; se retira previous sólo después de convergencia. La rotación externa real no se ejecutó en P4.

## Evidencia P4

- `test_p4_load_security.py::test_rotation_accepts_previous_and_current_without_replay`: previous/current PASS y duplicate nonce rechazado.
- `test_p4_release.py::test_staging_refuses_production_or_secrets`: configuración productiva o secretos literales rechazados.
- `test_p4_release.py::test_secret_metadata_never_accepts_values`: `SecretString`, passwords y valores prohibidos fallan cerrado.
- `evidence/p4/rc1/secrets-config.json`: inventario sintético sólo metadata, scans de manifests/logs PASS.

**IAM Operational Model: PASS** como modelo documental y de pruebas staging. Configuración IAM real externa: **NOT MODIFIED / EVIDENCE PENDING AS DOCUMENTED IN P3.11**.
