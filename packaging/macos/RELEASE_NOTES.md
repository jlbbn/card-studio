# Card Studio 1.2.3 — macOS

Build: 2.

## API real privada

- Cliente separado para `swift-ledger-payment-engine` mediante `/api/card-studio/v1/health` y `/api/card-studio/v1/operations`.
- Canal WireGuard por Tailscale sin exposición a Internet, HMAC-SHA256, timestamp, nonce anti-replay e idempotencia PostgreSQL.
- Backend esperado: `venus-swift-ledger-real`; entorno: `VENUS_PRIVATE`; respuestas reales exigen `simulation=false`.
- Test API muestra servicio, Backend ID y entorno. Un POS sandbox o un health genérico no se aceptan como backend real.
- El modo sandbox POS permanece separado y explícito. El registro real no representa captura ni movimiento de fondos.
- La llave de terminal no se guarda ni se incluye en el APP, DMG o ZIP.

## Configuración y recursos

- Configuración no secreta versión 2 ampliada; el antiguo `127.0.0.1:5300` se migra a la URL privada real.
- PC/SC, Hardware Validation y dry-run se conservan. La validación física en macOS requiere evidencia ejecutada en una Mac con lector y tarjeta.
- GlobalPlatform resuelve `gp.jar` desde la raíz del proyecto/bundle, declara JPype y puede comprobar JVM y clases Java sin abrir PC/SC ni transmitir APDUs.
- CardLab y Registry usan rutas de evidencia portables con fallback legacy y muestran fecha/origen histórico.
- Write Validation es un visor read-only de evidencia; ya no fuerza plataforma Linux ni ejecuta escrituras desde la interfaz.
- Canonical AID: **UNRESOLVED**. La release no declara selección física ni compatibilidad sin evidencia.

## Arquitecturas

Se generan binarios nativos separados para Apple Silicon `arm64` e Intel `x86_64`; no se produce un universal artificial porque PyInstaller y `pyscard` incluyen componentes nativos.

## Firma y notarización

Developer ID se usa únicamente si el runner proporciona `CODESIGN_IDENTITY` legítimo. En otro caso la firma es ad hoc. La notarización no está configurada y no debe inferirse desde una firma ad hoc.
