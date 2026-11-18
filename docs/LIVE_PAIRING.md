# Card Studio Live Pairing — reader ↔ server

## Alcance

El pairing transporta metadatos de chip entre un lector controlado por Paco y un servidor Card Studio. El canal es autenticado y cifrado de extremo a extremo a nivel de aplicación. No ejecuta pagos, personalización, escritura de tarjetas ni validación física por sí mismo.

Estados que no cambian:

- Formal Certification: `NOT COMPLETED`.
- Physical Validation: `NOT_EXECUTED_BY_PAIRING`.
- `SIMULATED` identifica dry-runs; `LIVE_READ_ONLY` sólo declara el origen de una evidencia de lectura previamente obtenida.
- Un AID, una etiqueta comercial o un ATR no demuestran esquema, certificación ni compatibilidad universal.

## Dependencia

El protocolo usa `cryptography>=46,<47`, declarada en `requirements.txt`. AES-256-GCM cifra y autentica cada evento; HKDF-SHA256 deriva claves independientes para cifrado y acuses. HMAC-SHA256 firma el handshake y los acuses.

## Preparar la clave compartida

Genera una clave una sola vez:

```bash
./run.sh --cli pairing generate-key
```

Compártela con Paco por un canal separado y configura el mismo valor en ambos procesos. No la guardes en `settings.json`, evidencia, Git ni en el comando de publicación.

```bash
export CARD_STUDIO_PAIRING_KEY='<base64url-de-32-bytes>'
```

La clave maestra no se transmite en el handshake.

## Servidor

En el servidor:

```bash
./run.sh --cli --data-dir /ruta/servidor pairing serve \
  --bind 0.0.0.0 --port 8765 \
  --server-id server-main --reader-id paco-reader
```

El valor predeterminado de `--bind` es `127.0.0.1`. Exponer el puerto en LAN es una decisión del operador. Puede colocarse detrás de HTTPS o una red privada; el sobre conserva cifrado e integridad aun cuando se use HTTP.

El servidor crea `pairing-events/events/<event_id>.json`. Cada archivo contiene un sobre cifrado y se publica mediante reemplazo atómico con permiso `0600`. No escribe el evento descifrado, la clave maestra ni las claves de sesión.

## Publicar desde el lector

Desde una evidencia generada previamente por Card Studio:

```bash
./run.sh --cli pairing publish \
  --server http://IP-DEL-SERVIDOR:8765 \
  --reader-id paco-reader \
  --evidence /ruta/validation.json \
  --protocol T=1
```

Para una prueba sintética sin lector:

```bash
./run.sh --cli pairing publish \
  --server http://IP-DEL-SERVIDOR:8765 \
  --reader-id paco-reader \
  --atr 3B0000 \
  --aid D4534C504D4401 \
  --reader-name 'Lector sintético' \
  --protocol T=1
```

El comando de publicación no abre PC/SC ni accede al lector. Sólo empaqueta los metadatos permitidos y los envía al servidor. Una evidencia `SIMULATED` nunca se promueve a `LIVE_READ_ONLY`.

## Esquema compartido

Handshake `card-studio.live-pairing.v1`:

- Solicitud: `schema`, `reader_id`, `reader_nonce`, `requested_at`, `signature`.
- Respuesta: `schema`, `session_id`, `reader_id`, `server_id`, `reader_nonce`, `server_nonce`, `expires_at`, `signature`.
- Los nonces son valores aleatorios de 32 bytes en base64url. Un nonce de handshake repetido se rechaza.

Evento descifrado `card-studio.chip-metadata.v1`:

```json
{
  "schema": "card-studio.chip-metadata.v1",
  "kind": "chip.metadata",
  "event_id": "evt-identificador-estable",
  "reader_id": "paco-reader",
  "captured_at": "2026-11-08T00:00:00.000000Z",
  "evidence_class": "SYNTHETIC",
  "source_result": "SIMULATED_MATCH",
  "metadata": {
    "atr": "3B0000",
    "selected_aid": "D4534C504D4401",
    "reader_name": "Lector sintético",
    "interface": "CONTACT",
    "protocol": "T=1",
    "standard": "ISO/IEC 7816-4"
  }
}
```

`metadata` sólo admite `atr`, `selected_aid`, `reader_name`, `application_label`, `interface`, `protocol` y `standard`. Se exige ATR o AID. No se admiten campos adicionales, APDU completos, TLV arbitrarios, PAN, Track 1/2, PIN, CVV, criptogramas, IAD ni llaves. `application_label` es opcional y nunca se infiere del AID.

Sobre `card-studio.encrypted-envelope.v1`:

- `schema`, `session_id`, `message_id`, `sequence`, `sent_at`, `sender_id` forman los datos asociados autenticados.
- `nonce` contiene 12 bytes aleatorios.
- `ciphertext` contiene el evento cifrado y el tag GCM.
- El servidor exige secuencia estricta por sesión y comprueba que el emisor y el lector del evento coincidan con el pairing.

Acuse `card-studio.pairing-ack.v1`:

- `schema`, `session_id`, `message_id`, `event_id`, `sequence`, `status`, `server_time`, `signature`.
- `status` es `STORED` o `DUPLICATE`; el lector verifica la firma antes de aceptarlo.

## Idempotencia, timeout y reintento

`event_id` identifica el mismo hecho aunque se abra otra sesión. Al usar una evidencia se deriva de su contenido estable; para datos introducidos manualmente puede fijarse con `--event-id`.

- El mismo `event_id` y el mismo contenido producen `DUPLICATE`, sin un segundo archivo.
- El mismo `event_id` con contenido distinto produce `EVENT_ID_CONFLICT`.
- El mismo `message_id` con otro sobre produce `MESSAGE_ID_CONFLICT`.
- Después de `SERVER_TIMEOUT_AFTER_SEND` o `SERVER_UNAVAILABLE_UNCERTAIN`, el cliente de biblioteca conserva el sobre y exige `retry()` explícito. No reenvía automáticamente.
- Al reintentar desde otro proceso, conserva el mismo `--event-id`; no cambies la evidencia ni `captured_at`.

El acuse se devuelve únicamente después de completar la escritura atómica. El servidor no afirma disponibilidad si el almacenamiento no está listo.
