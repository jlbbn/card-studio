# Card Studio 1.2.0 — macOS

Build: 1.

## macOS 1.2.0 packaging hotfix

- macOS packaging path resolution fix: corrected PyInstaller project-root resolution (`parents[2]` → `parents[1]`).
- Validated native macOS launch, clean build and installed-app dry-run.
- PC/SC stack ready.
- ReaderAdapter ready.
- Hardware Validation ready.
- Physical Java Card validation still pending.

## Incorporado

- Java Card hardware readiness.
- PC/SC ReaderAdapter para macOS con sesiones, ATR, transmisión y errores físicos explícitos.
- Hardware Validation integrado en Card Studio, con dry-run y evidencia portable.
- AID candidates `D4534C504D4401` (PC/SC / Cardpeek) y `D4534C504D44` (Android Card Bridge).
- SELECT APDU builder, response parser y status-word handling.
- Flujo de validación para Rocketek CSCR3 y Java Card de desarrollo.
- Migración compatible de preferencias a configuración versión 2.

## Estado del applet

- Canonical AID: **UNRESOLVED**.
- Physical validation: **PENDING**.
- La release no declara selección física ni compatibilidad de tarjeta sin evidencia real.

## Arquitecturas

Se generan DMG nativos separados para Apple Silicon `arm64` e Intel `x86_64`. No se produce un binario universal artificial porque PyInstaller y `pyscard` incluyen componentes nativos. Cada artefacto debe compilarse y probarse en su arquitectura.

## Instalación y uso

Abre el DMG correspondiente a la arquitectura del Mac y copia `Swift Ledger Card Studio.app` a `/Applications`. La aplicación incluye Hardware Validation y Dry-run; no requiere clonar el proyecto, crear un entorno virtual ni copiar scripts.

La evidencia se guarda en `~/Library/Application Support/Swift Ledger Card Studio/Evidence/applet-validation/`, fuera del `.app`.

## Firma y notarización

- Developer ID configurado únicamente si el entorno de build proporciona `CODESIGN_IDENTITY` legítimo.
- Sin esa variable se usa únicamente firma local/ad hoc para la comprobación técnica.
- Notarización: **NOT CONFIGURED** en este proyecto.

## Límites

La validación solo selecciona AID candidates y lee respuestas. No instala o borra applets, no personaliza, no cambia claves y no ejecuta provisioning. El usuario debe conectar Rocketek CSCR3 e insertar una Java Card de desarrollo para completar la validación física.
