# Swift Ledger Card Studio 1.2.0 en macOS

Build: 1. Arquitecturas: Apple Silicon `arm64` e Intel `x86_64`, en instaladores nativos separados.

## Instalar el paquete

1. Abre el DMG correspondiente a la arquitectura del Mac: `Card-Studio-1.2.0-macOS-arm64.dmg` o `Card-Studio-1.2.0-macOS-x86_64.dmg`.
2. Arrastra **Swift Ledger Card Studio.app** a `/Applications`.
3. Expulsa la imagen y abre la aplicación desde Aplicaciones.
4. Si el paquete solo tiene firma local, macOS puede pedir confirmación. Para distribución pública se requiere configurar Developer ID y, si aplica, notarización; ninguna credencial de firma se incluye en el proyecto.

El paquete no necesita que el usuario instale Python, cree un entorno virtual, clone el repositorio ni copie scripts. Hardware Validation y sus dependencias están integradas en el `.app`.

## Primer arranque

Card Studio comprueba configuración, módulos PC/SC, subsistema de validación y acceso de escritura a evidencia. La ausencia de Rocketek no bloquea la aplicación. El estado inicial es:

```text
Software readiness: PASS
Canonical AID: UNRESOLVED
Physical validation: PENDING
```

Las preferencias anteriores compatibles se conservan. La configuración se guarda con permisos `0600` en:

```text
~/Library/Application Support/Swift Ledger Card Studio/Config/settings.json
```

La migración a configuración versión 2 añade las preferencias de hardware sin incorporar secretos al bundle.

## Hardware Validation

1. Conecta Rocketek CSCR3 e inserta una Java Card de desarrollo.
2. Abre **Hardware Validation** en la barra superior.
3. Pulsa **Run Validation**.
4. Revisa el modo `PHYSICAL`, lector, tarjeta, ATR, respuestas SW1SW2, resultado y ruta de evidencia.

La operación prueba automáticamente:

- `D4534C504D4401`, origen PC/SC / Cardpeek;
- `D4534C504D44`, origen Android Card Bridge.

Solo una respuesta `9000` para un candidato y `6A82` para el otro permite una conclusión física. Dos `9000` producen `MULTIPLE_AID_MATCH`; dos `6A82`, `APPLET_NOT_FOUND`. Ningún resultado se escribe como AID canónico automáticamente.

Sin lector se muestra `Reader: NOT CONNECTED` y validación física pendiente. Con varios lectores se selecciona Rocketek por la información PC/SC disponible o el lector explícito configurado; no se escoge arbitrariamente un dispositivo desconocido.

**Dry-run · SIMULATED** ejecuta la misma orquestación con mocks y nunca produce `PHYSICALLY_CONFIRMED` ni `Physical validation: PASS`.

La evidencia se guarda fuera del `.app` en:

```text
~/Library/Application Support/Swift Ledger Card Studio/Evidence/applet-validation/
```

La validación solo realiza detección, ATR y SELECT. No instala/borra applets, no escribe, no cambia claves y no ejecuta provisioning.

## Runtime GlobalPlatform

El bundle incluye JPype, su extensión nativa, `org.jpype.jar` y `gp.jar`. La JVM no se empaqueta: se requiere un JDK 17 arm64 compatible y descubrible mediante `JAVA_HOME` o Java del sistema. La comprobación no abre PC/SC ni transmite APDUs:

```bash
'/Applications/Swift Ledger Card Studio.app/Contents/MacOS/Swift Ledger Card Studio' --cli globalplatform-status
```

Debe informar `status=PASS`, ambas clases Java cargadas y `gp.jar` dentro del `.app`. Si Java o una dependencia falta, el comando devuelve `FAIL` con la fase exacta del error.

## Evidencias CardLab, GlobalPlatform y Write Validation

Los botones **CardLab**, **Registry** y **Write Validation** leen evidencias existentes; no crean validaciones físicas ni convierten archivos en PASS. Sus ubicaciones portables son:

- `~/Library/Application Support/Swift Ledger Card Studio/Evidence/cardlab/validate-all.json`.
- `~/Library/Application Support/Swift Ledger Card Studio/Evidence/globalplatform/globalplatform-registry.json`.
- `~/Library/Application Support/Swift Ledger Card Studio/Evidence/write-validation/write-exercise-5969-restore-5968.json`.

Se conservan las rutas legacy de VENUS como fallback. `CARD_STUDIO_CARDLAB_EVIDENCE`, `CARD_STUDIO_REGISTRY_EVIDENCE` y `CARD_STUDIO_WRITE_EVIDENCE` permiten seleccionar otra fuente explícita. La interfaz muestra fecha, origen y alcance `HISTORICAL_RECORD`. Si la evidencia falta o es inválida, el error es obligatorio y no se ejecuta hardware como sustituto. **Write Validation es exclusivamente un visor de evidencia** desde esta revisión; el ejercicio de escritura permanece fuera de la interfaz.

## Modo virtual

Selecciona **Virtual** para leer la tarjeta, escribir un identificador propio `SL-…`, simular recargas y cobros MXN y consultar historial. No es dinero real y no se inician cargos bancarios.

## Conexión con Swift Ledger

1. Conéctate a la tailnet autorizada. Selecciona **API real · VENUS privada** y conserva la URL Tailscale `http://admin1-venus-series.tail7dc5a9.ts.net:5200` y el Backend ID `venus-swift-ledger-real`. WireGuard cifra el canal; HTTP no se expone a Internet.
2. Conserva el código de terminal `VENUS-MAC-001` e introduce su llave base64url; la llave no se guarda.
3. Pulsa **Test API**. El resultado debe mostrar `API REAL`, `swift-ledger-payment-engine`, el Backend ID y `VENUS_PRIVATE`.
4. Pulsa **Guardar configuración no secreta**. Para pruebas locales deliberadas, el modo **Sandbox POS · local** es independiente y se identifica como sandbox.

No se incluyen llaves privadas, tokens, `.env` ni credenciales de producción en el APP o DMG. Las URL con usuario/contraseña, parámetros o fragmentos se rechazan. El registro real no implica captura ni movimiento de fondos.

## Diagnóstico

Pulsa **Diagnosticar** para revisar GTK, PC/SC, lectores, Cardpeek, ADB y rutas de logs. En terminal también puedes ejecutar:

```bash
'/Applications/Swift Ledger Card Studio.app/Contents/MacOS/Swift Ledger Card Studio' --cli diagnose
```

La compatibilidad física requiere validación en una Mac con lector y tarjeta propios. Las pruebas automatizadas usan lectores simulados y no certifican ningún modelo concreto.

Los registros se encuentran en `~/Library/Logs/Swift Ledger Card Studio/card-studio.log`.

## Compilar en macOS

Requisitos: macOS 12 o posterior, Xcode Command Line Tools y Homebrew.

```bash
brew install python pkg-config gobject-introspection pygobject3 gtk4 libadwaita swig
cd /ruta/a/card-studio
chmod +x packaging/macos/build.sh
./packaging/macos/build.sh
```

En Apple Silicon genera:

```text
dist/macos-arm64/Swift Ledger Card Studio.app
dist/macos-arm64/Card-Studio-1.2.0-macOS-arm64.dmg
release/1.2.0/macos-arm64/Card-Studio-1.2.0-macOS-arm64.dmg
release/1.2.0/macos-arm64/checksums.txt
release/1.2.0/macos-arm64/RELEASE_NOTES.md
```

En una Mac Intel:

```bash
CARD_STUDIO_ARCH=x86_64 ./packaging/macos/build.sh
```

Genera el equivalente `macos-x86_64`. PyInstaller no genera binarios macOS cruzados; cada variante debe compilarse y probarse en su arquitectura. No se crea un universal artificial porque `pyscard` contiene una extensión nativa.

El build instala `pyscard` y PyInstaller en un entorno aislado, ejecuta pruebas y dry-run, genera el APP, aplica firma Developer ID solo si `CODESIGN_IDENTITY` está configurado legítimamente —o firma local/ad hoc en caso contrario— y crea DMG y checksum. Notarización: `NOT CONFIGURED`.

## Ejecutar desde el código

```bash
brew install python pkg-config gobject-introspection pygobject3 gtk4 libadwaita swig
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m card_studio
.venv/bin/python -m card_studio --cli diagnose
```

## Pruebas y validación pendiente

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Antes de distribuir, valida en una Mac física:

- apertura del APP y del DMG en una cuenta sin Python;
- carga/migración de configuración y primer arranque;
- dry-run y hardware-readiness desde el artefacto;
- ausencia de lector sin bloquear la aplicación;
- recarga, cobro, escritura `SL-…` y persistencia virtual;
- conexión a una API sandbox controlada e idempotencia;
- Hardware Validation con Rocketek y Java Card de desarrollo;
- firma, Gatekeeper y, si aplica, notarización;
- variante Intel en hardware o runner Intel.

Estas comprobaciones no se pueden completar desde Ubuntu y no deben darse por aprobadas solo porque el script de build exista.
