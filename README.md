# Swift Ledger Card Studio

**Definición objetivo:** estación de personalización y aprovisionamiento de Java Cards propias. Card Studio controla perfiles, applet, identidad, personalización, read-back, registro, tokenización, activación y evidencia auditable. **Card Bridge es únicamente la capa de transporte y abstracción entre Card Studio y el hardware físico**, no un producto independiente.

**Estado actual:** la versión 1.2.3 distingue la API real privada de VENUS (`simulation=false`, PostgreSQL e identidad fijada) del POS sandbox (`simulation=true`). Conserva tarjetas virtuales y propias Swift Ledger, recargas y cobros de demostración, historial local y sincronización. El registro en la API real no representa captura ni movimiento de fondos. No procesa PAN, CVV ni EMV. El frente Java Card está preparado para validación física, pero el Instance AID continúa `UNRESOLVED` y no existe evidencia de personalización física real.

La arquitectura de transición y los 18 entregables iniciales están en [docs/JAVA_CARD_PERSONALIZATION.md](docs/JAVA_CARD_PERSONALIZATION.md).

## Arquitectura actual

- `card_studio/applet.py`: candidatos AID con origen, SELECT builder, parser y status words.
- `card_studio/pcsc.py`: ReaderAdapter PC/SC para Linux y macOS mediante `pyscard`.
- `card_studio/hardware_validation.py`: validación read-only, decisión de AID y evidencia portable.
- `card_studio/dry_run.py`: escenarios deterministas marcados `SIMULATED`.
- `card_studio/readiness.py`: comprobación de preparación sin hardware.
- `card_studio/startup.py`: primer arranque, configuración y evidencia escribible.
- `card_studio/model.py`: validaciones, saldo MXN y contrato APDU legacy del applet propio.
- `card_studio/service.py`: operaciones, auditoría e idempotencia sandbox.
- `card_studio/backends.py`: tarjeta virtual, Cardpeek y puente SUNMI.
- `card_studio/ledger.py`: clientes real y sandbox separados, firma HMAC e identidad de backend.
- `card_studio/app.py` / `cli.py`: interfaz gráfica y línea de comandos.
- `card_studio/config.py`: configuración no secreta versión 2 con permisos `0600`.

## Linux

Instala los paquetes del sistema GTK4, libadwaita y PyGObject. En Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pcscd libpcsclite-dev build-essential swig
python3 -m pip install --user -r requirements.txt
./run.sh
```

El modo virtual no necesita lector, ADB, API ni PostgreSQL:

```bash
./run.sh --cli read
./run.sh --cli --data-dir /ruta/de/prueba reload 50.00
./run.sh --cli --data-dir /ruta/de/prueba charge 17.25
```

Las operaciones se guardan localmente. Sin API o credenciales quedan como `PENDING` y pueden reintentarse con `sync`, conservando su clave de idempotencia.

## macOS

Versión actual: **1.2.3, build 2**. La guía de ejecución y compilación está en [docs/MACOS.md](docs/MACOS.md). El paquete usa PyInstaller y no exige Python al usuario final. Se preparan DMG nativos separados para `arm64` y `x86_64`; no se genera un universal artificial porque `pyscard` es nativo.

El `.app` y el `.dmg` deben generarse en una Mac. Un script creado en Linux no demuestra que exista un instalador macOS válido.

## Java Card y Hardware Validation

La aplicación incluye **Hardware Validation** y **Dry-run · SIMULATED** en la ventana principal. La validación física es read-only: detecta PC/SC, selecciona lector, abre tarjeta, obtiene ATR, prueba ambos candidatos, interpreta SW1SW2, genera conclusión y guarda evidencia. No instala ni borra applets, no escribe ni cambia claves.

Candidatos incluidos, sin AID canónico:

- `D4534C504D4401` — origen PC/SC / Cardpeek.
- `D4534C504D44` — origen Android Card Bridge.

Para la validación técnica desde el repositorio, el único comando necesario al conectar Rocketek e insertar la Java Card de desarrollo es:

```bash
./scripts/validate_applet_hardware
```

Sin lector, el resultado correcto es `HARDWARE_NOT_CONNECTED` y `PHYSICAL_VALIDATION=PENDING`, no un PASS. Para comprobar la preparación sin hardware:

```bash
./scripts/validate_applet_hardware --readiness
./scripts/validate_applet_hardware --dry-run
```

La evidencia se guarda en la carpeta de datos del usuario, nunca dentro del `.app`. En macOS: `~/Library/Application Support/Swift Ledger Card Studio/Evidence/applet-validation/`.

CardLab, Registry y Write Validation cargan evidencias históricas desde `Evidence/cardlab`, `Evidence/globalplatform` y `Evidence/write-validation`, con fallback a las rutas legacy de VENUS y overrides `CARD_STUDIO_CARDLAB_EVIDENCE`, `CARD_STUDIO_REGISTRY_EVIDENCE` y `CARD_STUDIO_WRITE_EVIDENCE`. La interfaz identifica fecha y origen. Write Validation no ejecuta el ejercicio físico: sólo muestra el resultado registrado, incluidos FAIL e INCONCLUSIVE.

GlobalPlatform incluye JPype y `gp.jar` en el bundle. La JVM externa compatible sigue siendo necesaria; en macOS arm64 se recomienda JDK 17 arm64. `--cli globalplatform-status` verifica JAR, JPype, JVM y clases Java sin abrir PC/SC ni enviar APDUs.

## Configuración segura de Swift Ledger

En la interfaz usa **Guardar configuración no secreta**. Solo se guardan URL, modo de API, Backend ID, código de terminal, transporte, lector y preferencias no secretas de hardware. La llave de terminal nunca se guarda en `settings.json` ni se incluye en el APP/DMG. La configuración versión 2 incorpora los campos de API y migra el antiguo valor local `127.0.0.1:5300` a la API real privada de VENUS; el sandbox POS sigue disponible como modo explícito y nunca se presenta como backend real.

Por CLI:

```bash
export SWIFT_LEDGER_TERMINAL_KEY='<llave-base64url-de-terminal>'
./run.sh --cli --ledger-url http://admin1-venus-series.tail7dc5a9.ts.net:5200 --terminal-code VENUS-MAC-001 health
```

Las URL con usuario/contraseña, parámetros o fragmentos se rechazan. HTTP se conserva para POS local/LAN y para la IP/DNS de Tailscale, donde WireGuard proporciona el canal cifrado; fuera de un túnel privado, un servicio remoto requiere HTTPS.

## NFC y diagnóstico

```bash
./run.sh --cli diagnose
./run.sh --cli --mode pcsc detect
./run.sh --cli --mode pcsc --reader 'Nombre exacto del lector' read
```

La detección o las pruebas simuladas no demuestran compatibilidad física. Cada lector y tarjeta debe validarse por separado. Cardpeek queda como transporte alternativo y SUNMI V3 conserva su puente ADB.

## Evaluación EMVCo SBMP

Card Studio permanece **NO EVALUADO POR EMVCo y SIN SECN**. Los documentos SBMP recibidos describen un proceso de evaluación de seguridad y su registro; no convierten el sandbox en una Mobile Payment Application, no certifican un kernel EMV y no habilitan pagos reales ni aceptación por redes.

El análisis está en [docs/EMVCO_SBMP.md](docs/EMVCO_SBMP.md) y el borrador administrativo no enviado en [docs/EMVCO_REGISTRO_BORRADOR.md](docs/EMVCO_REGISTRO_BORRADOR.md).

## Pruebas

```bash
python3 -m unittest discover -s tests -v
```

La suite no usa red, lectores, tarjetas, la base de SUNMI V3 Cobros ni credenciales. Los escenarios PC/SC y Hardware Validation usan mocks explícitos; nunca se reportan como pruebas físicas.

## Live Pairing reader ↔ server

Card Studio incluye un esquema compartido para que un lector remoto publique metadatos permitidos en un servidor. El handshake usa HMAC, cada sesión deriva claves con HKDF y cada evento se transporta y conserva como un sobre AES-256-GCM idempotente y atómico. No se aceptan PAN, Track 2, PIN, CVV, APDU completos ni llaves.

```bash
./run.sh --cli pairing generate-key
./run.sh --cli pairing serve --reader-id paco-reader
./run.sh --cli pairing publish --server http://127.0.0.1:8765 --reader-id paco-reader --atr 3B0000 --aid D4534C504D4401
```

Los tres comandos requieren la misma `CARD_STUDIO_PAIRING_KEY` en servidor y lector, salvo `generate-key`. La guía, el esquema y la recuperación ante timeouts están en [docs/LIVE_PAIRING.md](docs/LIVE_PAIRING.md). El pairing no accede al lector físico ni ejecuta operaciones de pago.
