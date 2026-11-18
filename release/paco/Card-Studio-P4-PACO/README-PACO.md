# Card Studio P4 — PACO macOS

Versión: `1.2.0-p4-rc1`  
Commit: `296fa89457590903488e0055953cbd1d171f52f0`  
Tag: `card-studio-p4-baseline`

La única fuente del build es `source/Card-Studio-P4-PACO.bundle`. El clean-room Linux clonado desde ese bundle completó **441/441 tests, 0 skips** y la matriz PostgreSQL/SQLite **16/16 PASS** con PostgreSQL 16.15 aislado.

## Build nativo en una Mac

Requisitos: macOS 12 o posterior, arquitectura `arm64` o `x86_64`, Xcode Command Line Tools y Homebrew. El build instala mediante Homebrew las dependencias declaradas si faltan; crea un entorno PostgreSQL 16 temporal con datos sintéticos y lo elimina al terminar.

```bash
cd Card-Studio-P4-PACO
./build-macos.sh
```

El proceso verifica bundle, commit, tag y working tree; instala dependencias fijadas; ejecuta las 16 pruebas de persistencia y los 441 tests sin skips; compila `Card Studio.app`; crea el DMG; monta e instala en un destino aislado; ejecuta smoke sintético, cierre y reapertura; compara paridad y vuelve a verificar todos los hashes. Si un gate falla, el build falla.

PyInstaller no genera binarios macOS cruzados: este paso debe ejecutarse en macOS. El APP y DMG son unsigned y no notarized (`CODE_SIGNING = NOT_CONFIGURED`, `NOTARIZATION = NOT_CONFIGURED`).

## Instalar desde el DMG

1. Abre `Card-Studio-P4-PACO.dmg`.
2. Copia `Card Studio.app` a `/Applications` mediante el enlace `Applications`.
3. Abre la aplicación. El modo inicial es `virtual`; no necesita lector.
4. Cierra y vuelve a abrir la aplicación.
5. Desde esta carpeta ejecuta:

```bash
./verify-release.sh
./compare-release.sh
```

Si la app está en otro destino:

```bash
CARD_STUDIO_APP="/ruta/Card Studio.app" ./verify-release.sh
CARD_STUDIO_APP="/ruta/Card Studio.app" ./compare-release.sh
```

La verificación no escribe en tarjetas ni ejecuta validación física. `PCSC environment: UNAVAILABLE` informa que PC/SC o un lector no está disponible; no promueve evidencia física.

## Configuración y secretos

Sólo se incluyen `.env.example` y `config.example`. No se incluyen `.env`, credenciales AWS, contraseñas, llaves privadas ni llaves HMAC. Guarda configuración local fuera del APP; no edites los ejemplos para incluir secretos.

## Estados que no cambian

- Physical Validation: **DEFERRED / NOT EXECUTED**.
- Formal Certification: **NOT COMPLETED**.
- Los dry-runs son **SIMULATED** y no constituyen certificación ni evidencia física.
