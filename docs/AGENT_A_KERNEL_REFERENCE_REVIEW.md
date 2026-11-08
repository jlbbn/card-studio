# AGENT A — Revisión de referencias y gap analysis del kernel

Alcance: investigación estática y análisis de arquitectura. No se ejecutó hardware, no se incorporó código ni datos de las referencias y no se ejecutaron las suites descargadas. Las afirmaciones sobre Card Studio se verificaron en el árbol actual. Un protocolo privado `LAB_ONLY` no se considera soporte EMV real. Este documento no declara conformidad, certificación ni preparación L3.

## 1. Repositorios inspeccionados

### 1.1 Hyperion-X-EMV

- URL: https://github.com/nyimbi/Hyperion-X-EMV
- Rama: `main`
- Commit inspeccionado: `9fe50cb9281cd8699b7a94a8df3a51631c22718e`
- Árbol exacto: https://github.com/nyimbi/Hyperion-X-EMV/tree/9fe50cb9281cd8699b7a94a8df3a51631c22718e
- Licencia local inspeccionada: `LICENSE`, MIT, copyright Nyimbi Odero 2026.
- Archivos concretos revisados: `src/lib.rs`, `src/tlv.rs`, `src/dol.rs`, `src/apdu.rs`, `src/selection.rs`, `src/gpo.rs`, `src/aip.rs`, `src/afl.rs`, `src/record.rs`, `src/state.rs`, `src/restrictions.rs`, `src/cvm.rs`, `src/trm.rs`, `src/taa.rs`, `src/gac.rs`, `src/cid.rs`, `src/oda.rs`, `src/issuer.rs`, `src/c8.rs`, `src/config.rs`, `src/trace.rs`, `src/fsm.rs`, `src/terminal.rs`, `src/error.rs` y `tests/traceability_foundation.rs`.
- Pruebas: hay pruebas unitarias `#[cfg(test)]` dentro de los módulos Rust y pruebas de integración/trazabilidad en `tests/traceability_foundation.rs`. La revisión fue estática; no se infiere aprobación externa de la descripción del proyecto.

### 1.2 dcemv

- URL: https://github.com/vicente-da-silva/dcemv
- Rama: `master`
- Commit inspeccionado: `7a8ee22e434563af0703b05a679e04a1cc44e55f`
- Árbol exacto: https://github.com/vicente-da-silva/dcemv/tree/7a8ee22e434563af0703b05a679e04a1cc44e55f
- Licencia local inspeccionada: `LICENSE`, GNU AGPL v3; las cabeceras de `Kernel.cs` y otros archivos dicen versión 3 o posterior.
- Archivos concretos revisados:
  - `DCEMV_TLVProtocol/TLV.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContact/Kernel/Kernel.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContact/Kernel/States/State_3_WaitingForGPOResponse.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContact/Kernel/States/State_4_WaitingForEMVReadRecord.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContact/Kernel/States/State_8_WaitingForOnlineAuth.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContact/Kernel/States/State_11_WaitingForScriptProcessing.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelContactless/Kernels/Kernel2/Kernel2.cs`.
  - `DCEMV_EMVProtocol/EMVCard/KernelShared/Procedures/CVMSelection_7_5.cs`.
  - Se inspeccionó además la estructura real de `EMVCard/KernelShared/Instructions`, `KernelContactless/Kernels`, `SmartTags` y `Security Algorithms`.
- Pruebas: la búsqueda de atributos y clases de test solo localizó `DCEMV_PersoApp/DCEMV_PersoApp/Views/TestView.xaml.cs`, una vista de aplicación, no una suite automatizada del kernel. No se afirma cobertura de pruebas para dcemv.

### 1.3 tomkp/emv

- URL: https://github.com/tomkp/emv
- Rama: `master`
- Commit inspeccionado: `ce9f62771f558bc66a4f456c2905b9d62570a7dc`
- Árbol exacto: https://github.com/tomkp/emv/tree/ce9f62771f558bc66a4f456c2905b9d62570a7dc
- Licencia: `package.json` declara `"license": "MIT"`; no existe `LICENSE`/`COPYING` en la raíz de este commit. La declaración no suministra el texto y notice completos en el árbol inspeccionado.
- Archivos concretos revisados: `package.json`, `README.md`, `src/index.ts`, `src/types.ts`, `src/commands.ts`, `src/emv-application.ts`, `src/emv-application.test.ts`, `src/commands.test.ts`, `src/emv-tags.test.ts`.
- Dependencia relevante: BER-TLV está delegada a `@tomkp/ber-tlv`; no se atribuye al repositorio local una implementación propia completa de BER-TLV.

### 1.4 emv-card-simulator

- URL: https://github.com/mrautio/emv-card-simulator
- Rama: `master`
- Commit inspeccionado: `761321e437f2cd2237113fb615b709dc3f17ae72`
- Árbol exacto: https://github.com/mrautio/emv-card-simulator/tree/761321e437f2cd2237113fb615b709dc3f17ae72
- Licencia local inspeccionada: `LICENSE`, Zlib, copyright Mika Rautio 2020.
- Código concreto revisado: `src/main/java/emvcardsimulator/EmvApplet.java`, `PaymentSystemEnvironment.java`, `ProximityPaymentSystemEnvironment.java`, `PaymentApplication.java` y `ReadRecord.java`.
- Pruebas concretas revisadas: `src/test/java/emvcardsimulator/SimulatorTest.java`, `PaymentSystemEnvironmentTest.java`, `ProximityPaymentSystemEnvironmentTest.java`, `PaymentApplicationProtocolTest.java`, `PaymentApplicationProtocolT0Test.java`, `PaymentApplicationTest.java`, `CardRiskManagementTest.java`, `PersonalizationTest.java`, `FaultInjectionTest.java`, `FaultInjectionT0Test.java` y `ContactlessTransactionTest.java`.
- Se inspeccionaron nombres y ubicación de fixtures bajo `src/test/java/config` y `src/main/rust/config`; no se leyeron ni incorporaron claves privadas. El simulador se usó como referencia de tarjeta y fault injection, no como evidencia de comportamiento de Card Studio.

## 2. Módulos relevantes

| Referencia | Módulos y utilidad verificada | Clasificación |
|---|---|---|
| Hyperion | `tlv.rs`: parser acotado, redacción de `Debug`, duplicados directos; `dol.rs`: DOL/DataStore y políticas explícitas de padding; `apdu.rs`: comandos y transporte relacionado; `selection.rs`: candidatos, exact/partial y FCI; `gpo.rs`/`aip.rs`/`afl.rs`: respuestas 77/80, AIP y plan de registros; `record.rs`: almacenamiento atómico y provenance de datos | `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE` |
| Hyperion | `state.rs`: TVR/TSI y máscaras RFU; `restrictions.rs`: fechas/AUC; `cvm.rs`: CVM List, condiciones, resultados y handles PED; `trm.rs`: floor, velocity y muestra externa; `taa.rs`: TAC/IAC; `gac.rs`/`cid.rs`: respuestas GENERATE AC y tipo de criptograma | `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE` |
| Hyperion | `oda.rs`: selección SDA/DDA/CDA, CAPK y verificación; `issuer.rs`: host response, Issuer Authentication y scripts; `c8.rs`: outcomes contactless, límites y relay resistance; `config.rs`: perfiles y política de carga; `trace.rs`: masking/replay; `fsm.rs`: estados/eventos | `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE` |
| dcemv | `KernelContact/Kernel/Kernel.cs` y `States/*`: máquina por estados con GPO, records, CVM, TRM, dos GENERATE AC, online, EXTERNAL AUTHENTICATE y scripts; `KernelContactless/Kernels/*`: kernels y estados específicos, incluido torn log en Kernel 2 | `ARCHITECTURE_REFERENCE`, `DO_NOT_REUSE` |
| dcemv | `DCEMV_TLVProtocol/TLV.cs`, `KernelShared/Instructions/*`, `SmartTags/*`, `Security Algorithms/*`: modelo de datos y comandos; `Procedures/CVMSelection_7_5.cs`: selección CVM | `ARCHITECTURE_REFERENCE`, `DIAGNOSTIC_REFERENCE`, `DO_NOT_REUSE` |
| tomkp | `emv-application.ts`: builders APDU, PSE/PPSE, GPO/AFL/records, DOL, CVM y GENERATE AC; `commands.ts`: CLI, espera de tarjeta y descubrimiento; `emv-tags.ts` y tests: diagnóstico/formateo | `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE` |
| tomkp | `emv-application.test.ts`: comandos, T=0/auto GET RESPONSE, GPO truncado, multiple AID y CVM; `commands.test.ts`: timeout de detección y errores; `emv-tags.test.ts`: unknown tags y malformed TLV | `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE` |
| Simulator | `PaymentApplication.java`: estados de tarjeta, DOL, GENERATE AC, issuer authentication/scripts, ODA, contactless y recovery; `EmvApplet.java`: APDU, T=0/T=1/contactless y GET RESPONSE; PSE/PPSE y records | `CARD_SIMULATOR_REFERENCE`, `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE` |
| Simulator | `FaultInjectionTest.java`: malformed encodings, duplicados/omisiones, status words, delays y cadenas; `ContactlessTransactionTest.java`: qVSDC, fDDA y RECOVER AC; `SimulatorTest.java`: contact T=0/T=1 y contactless | `CARD_SIMULATOR_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE` |

## 3. Arquitectura observada y correspondencias exactas

### 3.1 Mapa solicitado de Hyperion

Todos los nombres siguientes existen en el commit inspeccionado; no se usan equivalencias hipotéticas.

| Concepto | Ruta real | Correspondencia con Card Studio verificada |
|---|---|---|
| tlv | `src/tlv.rs` | `card_studio/tlv/codec.py`, `model.py`, `errors.py`, `evidence.py`; existe parser BER estricto LAB, sin todas las reglas de provenance |
| dol | `src/dol.rs` | Ausente: no hay PDOL/CDOL/DDOL/TDOL ni DataStore equivalente |
| apdu | `src/apdu.rs` | `card_studio/emv/protocol.py`; builders privados, sin modelo APDU general ni política contextual completa |
| selection | `src/selection.rs` | `InternalEmvKernel._select_application`; solo perfil/AID explícito, sin FCI de directorio |
| gpo | `src/gpo.rs` | `parse_initialize_response` no es GPO; usa template 77 privado y no valida AIP/AFL reales |
| afl | `src/afl.rs` | Ausente: no hay parser ni plan de lectores SFI/record |
| record | `src/record.rs` | `parse_read_data_response` valida un template 70 privado; no es READ RECORD ni DataStore atómico |
| state | `src/state.rs` para TVR/TSI; `src/fsm.rs` para FSM | `card_studio/kernels/state.py` sí tiene FSM LAB; no tiene TVR/TSI |
| restrictions | `src/restrictions.rs` | El estado `PROCESSING_RESTRICTIONS` existe, pero solo vuelve a comprobar interfaz |
| cvm | `src/cvm.rs` | `card_studio/emv/decision.py`: decisión sintética de un método de perfil, no CVM List/reglas/9F34 |
| trm | `src/trm.rs` | `decide_risk`: floor, límite de interfaz y selección determinista; sin velocity counters completos |
| taa | `src/taa.rs` | `terminal_action`/`combine_actions`: precedencia local, sin TVR ni TAC/IAC |
| gac | `src/gac.rs` | `card_action_command` usa INS AE con payload privado; no implementa GENERATE AC real |
| oda | `src/oda.rs` | Ausente, incluida la frontera de selección/resultado SDA/DDA/CDA |
| issuer | `src/issuer.rs` | `complete_online` solo acepta una decisión textual; no ARPC, EXTERNAL AUTHENTICATE, scripts ni segundo GAC |
| c8 | `src/c8.rs` | Solo existe límite por interfaz en `decide_risk`; no outcomes/start/UI/restart/relay/torn |
| config | `src/config.rs` | `CardProfile` se valida por digest y capacidades; no hay configuración firmada de scheme/kernel, CAPK, TAC/IAC o CDOL |
| trace | `src/trace.rs` | Result details y `safe_tlv_evidence` evitan valores; no hay traza APDU secuenciada/replay con política equivalente |

### 3.2 dcemv

- `KernelContact/Kernel/Kernel.cs` despacha acciones a estados separados para PDOL, GPO, READ RECORD, CVM, TRM, primer y segundo GENERATE AC, online, EXTERNAL AUTHENTICATE y scripts. Es una separación arquitectónica útil para no colapsar issuer processing en una decisión booleana.
- `State_3_WaitingForGPOResponse.cs` distingue respuestas 77 y 80 y construye la AFL activa. `State_4_WaitingForEMVReadRecord.cs` recorre AFL, acumula datos estáticos de autenticación y deriva a ODA.
- `State_8_WaitingForOnlineAuth.cs` separa aprobación, rechazo y unable-to-go-online, y puede emitir EXTERNAL AUTHENTICATE. `State_11_WaitingForScriptProcessing.cs` diferencia scripts antes y después del GENERATE AC final.
- `KernelContactless/Kernels/Kernel2/Kernel2.cs` tiene estados propios de relay resistance, mag-stripe mode, RECOVER AC, CCC y PUT DATA; confirma que contactless no debe tratarse como el mismo flujo de contacto con otro nombre de interfaz.
- Riesgo observado: la validación de GPO no exige siempre AIP y AFL conjuntamente, y el estado de lectura acepta ciertos records vacíos. Estas rutas no se recomiendan como algoritmo; ver `SPEC_WINS` en riesgos.

### 3.3 tomkp

- `EmvApplication` encapsula `SmartCard.transmit` y habilita `autoGetResponse`. Los builders cubren SELECT, READ RECORD, GET DATA, GPO, GENERATE AC e INTERNAL AUTHENTICATE.
- La clase ofrece PSE y PPSE, lectura AFL, parsing DOL y CVM, pero `performTransaction` no ejecuta restrictions, TVR/TSI, TRM/TAA, ODA, issuer authentication/scripts ni una FSM completa.
- Los parsers son tolerantes: GPO desconocido o truncado devuelve valores vacíos y los records fallidos pueden omitirse. Esto es útil para un explorador, pero no para un kernel que deba distinguir datos obligatorios ausentes y fallos técnicos.
- El código PIN y los formateadores que muestran PAN/track no son referencias admisibles para Card Studio LAB_ONLY.

### 3.4 Simulador

- `PaymentSystemEnvironment` soporta SELECT y records; `ProximityPaymentSystemEnvironment` documenta y prueba que PPSE no tiene records y que las Directory Entries incluyen AID, prioridad y Kernel ID.
- `PaymentApplication` conserva estados de tarjeta `IDLE/GPO_DONE/ARQC_ISSUED/COMPLETED`, ATC, CDOL1/2, primer y segundo GENERATE AC, issuer authentication y scripts. Es una referencia de oráculo de tarjeta, no un terminal a copiar.
- `FaultInjectionTest` y su subclase T=0 aportan mutaciones de longitud, tags truncados, constructed bit, omisión/duplicado, SW, delays y cadenas GET RESPONSE. Es la referencia más directa para ampliar pruebas negativas sin usar datos reales.

## 4. Diferencias contra Card Studio

1. **BER-TLV existe, el modelo EMV de datos no.** `card_studio/tlv/codec.py` valida tags multibyte, longitudes canónicas, anidamiento, truncamiento y límites. Conserva unknown tags. No existe DOL ni un DataStore con dueños de tags, duplicados semánticos y almacenamiento atómico como en Hyperion.
2. **Los comandos actuales son privados.** `INITIALIZE` usa INS E0 y un marcador LAB; no es GPO A8. `READ_DATA` es GET DATA de ATC, no READ RECORD B2 guiado por AFL. `CARD_ACTION` usa INS AE con datos DF13 y respuesta DF04, pero no construye CDOL ni parsea CID/AC/ATC reales.
3. **SELECT por perfil no es descubrimiento PSE/PPSE.** Card Studio exige AID explícito ante varias aplicaciones, decisión contractual segura. No selecciona `1PAY.SYS.DDF01`/`2PAY.SYS.DDF01`, no lee Directory Entries y no valida FCI final 6F/84. La selección automática ordenada de Hyperion no debe sustituir silenciosamente esta regla: `REVIEW_REQUIRED` por contrato de producto.
4. **No hay GPO/AIP/AFL/records reales.** El template 77 de laboratorio contiene un valor 82 de tres bytes (`LAB`) y carece de 94; por tanto no demuestra AIP de dos bytes ni AFL. No hay formatos 77/80, PDOL, plan SFI/record, lectura atómica o acumulación ODA.
5. **Restrictions, CVM, TRM y TAA son modelos sintéticos.** Processing Restrictions solo revisa interfaz. CVM no lee 8E ni genera 9F34. TRM es determinista pero no mantiene contadores offline no volátiles. TAA no evalúa TVR contra TAC/IAC. TVR/TSI no existen.
6. **GENERATE AC y online son decisiones, no criptogramas.** No hay CID 9F27, AC 9F26, ATC 9F36, IAD, formatos 77/80, ARQC/TC/AAC real, segundo GENERATE AC ni CDOL2. El resultado textual `APPROVED/DECLINED/ONLINE_REQUIRED/ACTION_REQUIRED` no es un cryptogram result.
7. **ODA e issuer processing están ausentes.** No hay selección SDA/DDA/CDA, CAPK, INTERNAL/EXTERNAL AUTHENTICATE, Issuer Authentication ni Issuer Scripts. Estas capacidades requieren una frontera explícita y material TEST aprobado; no deben improvisarse con claves propias.
8. **Contact y contactless comparten protocolo de juguete.** Existe paridad E2E sintética y límites por interfaz, pero no kernels específicos por AID/kernel, PPSE/Kernel ID, TTQ/CTQ, outcomes C-8, relay resistance o torn transactions.
9. **La FSM y recuperación sí tienen base útil.** `kernels/state.py` valida saltos y conserva historial. `internal_emv.py` implementa deadline antes/después de envío, UNCERTAIN + QUERY_REQUIRED, idempotencia, query, cancel/close idempotentes y result loader. La cancelación no interrumpe un `execute` bloqueado por el lock y el loader restaura resultados, no una sesión APDU en curso.
10. **La redacción es defensiva pero limitada.** Los detalles incluyen transiciones y metadatos, no APDU/TLV completos. No existe traza secuenciada de comandos/respuestas, clasificación contextual de SW o replay. `safe_tlv_evidence` no se integra como una traza APDU completa.
11. **La cobertura actual es buena para el contrato LAB.** `tests/test_agent_a_kernel.py` contiene 28 tests que cubren malformed/unknown TLV, multiple AID, unsupported interface, card removal, timeout antes/después, UNCERTAIN, duplicates, restart de resultado, CVM/risk y paridad Mock/Internal. No cubre los objetos EMV ausentes. Escenarios de host timeout, partial refund y reversal están en la capa B (`tests/test_agent_b_terminal.py`, `card_studio/l3/scenarios.py`), no en el protocolo APDU del kernel.

## 5. P0 gaps

Los P0 son dependencias estructurales o de seguridad para cualquier futuro alcance EMV real; no son una declaración de que dicho alcance ya esté aprobado.

1. **P0-01 — DOL y fuentes de datos.** Implementar PDOL, CDOL1/2, DDOL/TDOL cuando aplique, DataStore con ownership, reglas de truncado/padding por especificación y Unpredictable Number suministrado por una frontera adecuada. Referencia: Hyperion `src/dol.rs`, `src/apdu.rs`.
2. **P0-02 — APDU y status contextuales.** Modelar casos/Lc/Le, SW 61xx/6Cxx y límites de cadenas; conservar card/reader removed y timeout después de envío como inciertos. Referencia: Hyperion `src/apdu.rs`, `src/error.rs`; simulator `EmvApplet.java` y `PaymentApplicationProtocolT0Test.java`.
3. **P0-03 — PSE/PPSE y AID/kernel selection.** Parsear FCI y Directory Entries, duplicados, prioridad, exact/partial y FCI final; preservar la regla anti-selección silenciosa salvo decisión contractual explícita. Referencia: Hyperion `src/selection.rs`; simulator `ProximityPaymentSystemEnvironmentTest.java`.
4. **P0-04 — GPO real.** Construir tag 83 con PDOL y parsear respuestas 77/80 exigiendo AIP de dos bytes y AFL válida. Referencia: Hyperion `src/gpo.rs`, `src/aip.rs`, `src/afl.rs`.
5. **P0-05 — READ RECORD y almacenamiento atómico.** Generar plan desde AFL, validar template 70, duplicados/provenance y datos obligatorios; nunca omitir records fallidos silenciosamente. Referencia: Hyperion `src/record.rs`; contraste negativo: tomkp `readAllRecords`.
6. **P0-06 — TVR, TSI y Processing Restrictions.** Añadir bitmaps con máscaras RFU, reinicio por transacción, fechas BCD, versión y AUC. Referencia: Hyperion `src/state.rs`, `src/restrictions.rs`.
7. **P0-07 — CVM real.** Parsear CVM List 8E, cantidades X/Y, condiciones y continue-on-failure; emitir CVM Results 9F34 y TVR sin capturar PIN. Referencia: Hyperion `src/cvm.rs`; dcemv `Procedures/CVMSelection_7_5.cs` solo como arquitectura.
8. **P0-08 — TRM/TAA basados en estado.** Mantener floor/excepciones/selección determinista y añadir el resultado a TVR/TSI; decidir AAC/TC/ARQC mediante TAC/IAC y capacidad online. Referencia: Hyperion `src/trm.rs`, `src/taa.rs`.
9. **P0-09 — GENERATE AC y cryptogram result.** Construir CDOL, distinguir AAC/TC/ARQC y parsear 77/80 con CID, AC, ATC e IAD; ampliar la FSM para primer/final GAC. Referencia: Hyperion `src/gac.rs`, `src/cid.rs`.
10. **P0-10 — ODA boundary explícita.** Definir selección/resultado SDA/DDA/CDA y trust boundary antes de cualquier criptografía. CAPK, certificados y vectores deben ser TEST aprobados; `UNKNOWN` queda bloqueado. Referencia: Hyperion `src/oda.rs`; simulator `PaymentApplicationProtocolTest.java`.
11. **P0-11 — Issuer Authentication, scripts y online final.** Parsear ARC/ARPC, EXTERNAL AUTHENTICATE, scripts antes/después y CDOL2/segundo GAC; una decisión textual del host no basta. Referencia: Hyperion `src/issuer.rs`; dcemv estados 8–11; simulator `PaymentApplicationProtocolTest.java`.
12. **P0-12 — Contactless y C-8 reales.** Seleccionar kernel por AID/Kernel ID y configuración, con outcomes, start/restart, UI, límites y comportamiento específico; no declarar paridad real por reutilizar el flujo LAB. Referencia: Hyperion `src/c8.rs`; dcemv `KernelContactless/Kernels`; simulator `ContactlessTransactionTest.java`.

## 6. P1 gaps

1. **P1-01 — Endurecer BER-TLV y provenance.** Rechazar bytes de tag inválidos 00/FF, high-tag no canónico y duplicados donde el contexto exija unicidad; añadir búsqueda directa única y pruebas de no almacenamiento parcial. Referencia: Hyperion `src/tlv.rs`, `src/record.rs`.
2. **P1-02 — Velocity y persistencia TRM.** Añadir lower/upper consecutive offline limits, excepción y merchant-forced-online con contador no volatile inyectado; la selección aleatoria actual puede conservarse como muestra determinista de prueba. Referencia: Hyperion `src/trm.rs`.
3. **P1-03 — Relay resistance y torn recovery.** Tras la base C-8, cubrir timing/respuesta, RECOVER AC/DRDOL y relectura sin crear aprobación nueva. Referencia: Hyperion `src/c8.rs`; simulator `ContactlessTransactionTest.java`.
4. **P1-04 — Cancelación en vuelo y sesión APDU.** Mantener idempotencia actual, pero permitir cancelación observada durante exchange y definir qué estados son recuperables tras restart sin reenviar comandos. Referencia: Card Studio `internal_emv.py`; Hyperion `src/fsm.rs`.
5. **P1-05 — Restart, read-back y reversal recovery.** El `result_loader` actual restaura resultados; B ya cubre IN_PROGRESS/UNCERTAIN. Falta una frontera kernel/servicio para read-back de tarjeta/host y reversal cuando corresponda, nunca aprobación inferida. Referencia: `tests/test_agent_b_terminal.py`; simulator `recoverAcTest`.
6. **P1-06 — Host timeout e idempotencia online.** B ya prueba timeout, duplicate y conflicto. Integrar esa semántica con ARPC/segundo GAC cuando exista, conservando UNCERTAIN y sin reversal ciego. Referencia: `tests/test_agent_b_terminal.py`, `card_studio/l3/scenarios.py`.
7. **P1-07 — Traza y diagnóstico redactado.** Añadir secuencia/dirección/contexto, longitudes y SW, masking por política y replay sin PIN/PAN/track/claves. Referencia: Hyperion `src/trace.rs`, `src/gac.rs` (`Debug`), y el test de logging de simulator solo como contraste de riesgo.
8. **P1-08 — Configuración por scheme/kernel.** Versionar perfiles con TAC/IAC, DOL, límites, CVM, ODA y mapeo AID-interfaz-kernel; firmas/provenance deben ser política explícita. Referencia: Hyperion `src/config.rs`; dcemv `PreProcessing/CardKernelAidCombination.cs`.
9. **P1-09 — Fuzz y oráculos de tarjeta.** Incorporar casos de fault injection con datos exclusivamente SYNTHETIC/TEST generados localmente. Referencia: simulator `FaultInjectionTest.java` y `FaultInjectionT0Test.java`.
10. **P1-10 — Semántica purchase/refund/reversal.** B ya valida referencias, importes y duplicados; definir si el kernel cambia transaction type/CVM/ARQC y evitar anunciar capacidades APDU iguales sin reglas específicas. Referencia: `tests/test_agent_b_terminal.py` y `card_studio/l3/scenarios.py`.
11. **P1-11 — Fuente de Unpredictable Number.** Inyectar y probar una fuente apropiada al modo LAB; no usar `Math.random` ni valores repetidos/cero como sustituto acrítico. Referencia positiva: Hyperion `tests/traceability_foundation.rs` y callbacks RNG; contraste: tomkp `emv-application.ts`.

## 7. Riesgos

### 7.1 Conflictos con especificación: SPEC_WINS

| Ubicación inspeccionada | Hallazgo | Decisión |
|---|---|---|
| dcemv `State_3_WaitingForGPOResponse.cs:123-127` | La condición no exige de forma consistente tanto AIP como AFL; ciertos shapes incompletos pueden continuar | **SPEC_WINS**: GPO debe validar objetos obligatorios y forma exacta; no copiar la condición |
| dcemv `State_4_WaitingForEMVReadRecord.cs:123-137` | Para SFI <= 10, una respuesta vacía se acepta como parsing correcto | **SPEC_WINS**: un record EMV debe validarse según template/contexto; vacío no se convierte en éxito genérico |
| tomkp `src/emv-application.ts:638-655` | `0x1E` se traduce como signature y `0x06` no se mapea así; Hyperion `src/cvm.rs:52-63` muestra el mapa coherente: signature 0x06 y fail-CVM 0x1E | **SPEC_WINS**: corregir cualquier tabla derivada mediante especificación y vectores, no copiar el mapa de tomkp |
| tomkp `src/emv-application.ts:985-1000,1181-1320` | Records fallidos se omiten y la transacción usa un CDOL “mínimo” predeterminado en vez del CDOL leído de tarjeta | **SPEC_WINS**: no continuar con datos obligatorios ausentes ni inventar CDOL; distinguir error técnico de decisión funcional |
| tomkp `src/emv-application.ts:243-255` | Una respuesta de menos de dos bytes produce SW por defecto en vez de error explícito de truncamiento | **SPEC_WINS**: respuesta malformada debe fallar de forma controlada, nunca fabricar estado |
| Hyperion `src/dol.rs` frente a tomkp `buildPdolData` | Las implementaciones difieren en el lado de padding para valores cortos | **SPEC_WINS** tras revisión normativa por tipo de dato; mientras tanto `REVIEW_REQUIRED`, no elegir por conveniencia |

### 7.2 Riesgos de interpretación y seguridad

- La frase descriptiva “certified protocol engine” en `Hyperion src/lib.rs` no es evidencia independiente de certificación; esta revisión no la adopta.
- La selección ordenada automática de Hyperion puede devolver varios candidatos y dejar la elección al runtime. Card Studio actualmente exige AID explícito con múltiples aplicaciones. Cambiarlo requiere decisión de contrato: `REVIEW_REQUIRED`.
- dcemv no tiene una suite automatizada localizada en este commit. Su cobertura o corrección no se deduce de la cantidad de archivos.
- El protocolo actual usa INS AE, pero esto no lo convierte en GENERATE AC. Presentarlo como tal sería un falso positivo de capacidad.
- `InternalEmvKernel.capabilities()` enumera purchase/refund/reversal, pero el flujo APDU privado no cambia por tipo. La validación financiera de B no debe confundirse con semántica EMV de esos tipos.
- El lock de ejecución protege duplicados, pero impide que `cancel()` se observe hasta que termine el exchange. Un timeout posterior al envío debe seguir siendo UNCERTAIN.
- `complete_online` no procesa ARPC ni scripts; agregar campos a un mapping sin máquina de estados real no cerraría el gap.
- Los formatadores de tomkp y el APDU log del simulador pueden emitir valores sensibles. No usar esos patrones en evidencia de Card Studio.
- ODA no debe implementarse con claves inventadas ni con fixtures de procedencia desconocida. La frontera puede quedar `BLOCKED` hasta contar con datos TEST aprobados.

### 7.3 Clasificación de datos de ejemplo

| Fuente | Clasificación | Decisión |
|---|---|---|
| Vectores inline de Hyperion en `src/*rs` bajo `#[cfg(test)]` | `TEST` para formas APDU/TLV unitarias | Se pueden usar como ideas de casos, reescritos localmente sin copiar código |
| `docs/scheme_profiles.cert.json`, CAPK y bundles de Hyperion | `UNKNOWN` para esta revisión, aunque el propio config contiene estados de fixture pendientes | No incorporar; exigir procedencia y autorización explícitas |
| Demos y datos de dcemv | `UNKNOWN` | No incorporar; AGPL además impone `DO_NOT_REUSE` inicial |
| APDU/AID simples de tests de tomkp | `TEST` | Solo como referencia de comportamiento |
| PAN, Track 1/2 y datos realistas de tests de tomkp | `UNKNOWN` | No incorporar ni usar como fixtures de Card Studio |
| Tests Java del simulador y claves RSA generadas en runtime en `PersonalizationTest.java` | `TEST` | Referencia de oráculo; generar material nuevo local si se decide |
| PEM, YAML y claves incluidas en `src/test/java/config` y `src/main/rust/config` del simulador | `UNKNOWN` en procedencia y autorización para reutilización externa | No leer, copiar ni incorporar |
| Fixtures actuales `LAB_ONLY` de Card Studio | `SYNTHETIC` | Conservar claramente separados de futuros vectores EMV TEST |

## 8. Licencias

Esta tabla es una revisión técnica de licencias, no asesoría legal. “Arquitectura” significa aprender separación y flujos sin copiar código.

| Repository | License | Can inspect? | Can derive architecture? | Can reuse code? | Conditions? | Risk? |
|---|---|---|---|---|---|---|
| https://github.com/nyimbi/Hyperion-X-EMV | MIT, `LICENSE` confirmado | Sí | Sí | Potencialmente sí, con decisión y atribución | Incluir copyright y permission notice en copias o porciones sustanciales; conservar procedencia del commit | Medio: fixtures/CAPK/vectores tienen procedencia separada; no arrastrar datos `UNKNOWN` |
| https://github.com/vicente-da-silva/dcemv | AGPL v3; cabeceras “v3 o posterior” | Sí | Sí, solo arquitectura inicialmente | **DO_NOT_REUSE** para core salvo decisión explícita del orchestrator | Copyleft fuerte, distribución de fuente y obligaciones de uso por red; revisión legal si se cambia la política | Alto: no copiar código, tablas extensas ni fixtures |
| https://github.com/tomkp/emv | MIT declarado en `package.json`; sin archivo LICENSE en el commit | Sí | Sí | No por ahora: `REVIEW_REQUIRED` hasta confirmar texto y copyright | La declaración MIT normalmente exige notice, pero el árbol no lo incluye; dependencias tienen licencias propias | Medio-alto por notice incompleto y datos de ejemplo `UNKNOWN` |
| https://github.com/mrautio/emv-card-simulator | Zlib, `LICENSE` confirmado | Sí | Sí | Potencialmente sí, con decisión y notice | No falsear origen; marcar versiones alteradas; no eliminar/alterar el notice en distribución fuente | Medio: claves/fixtures incluidos son `UNKNOWN` para reutilización externa |

Clasificación de reutilización adoptada:

- Hyperion: `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE`.
- dcemv: `ARCHITECTURE_REFERENCE`, `DIAGNOSTIC_REFERENCE`, `DO_NOT_REUSE`.
- tomkp: `ALGORITHM_REFERENCE`, `TEST_REFERENCE`, `DIAGNOSTIC_REFERENCE`; código `REVIEW_REQUIRED` por notice.
- Simulator: `CARD_SIMULATOR_REFERENCE`, `ARCHITECTURE_REFERENCE`, `ALGORITHM_REFERENCE`, `TEST_REFERENCE`.
- Ninguna referencia implica permiso para reutilizar datos financieros, claves, CAPK o fixtures `UNKNOWN`.

## 9. Cambios recomendados

Orden por dependencia técnica; cualquier implementación futura debe ser una tarea separada y explícita.

1. **Congelar la distinción de modos.** Mantener `LAB_ONLY` identificado y crear, si se aprueba, un límite de capacidad EMV real separado. No promover el protocolo actual cambiando nombres.
2. **Definir primero el modelo de datos y errores.** DOL/DataStore, APDU response estricta, ownership, mandatory objects y correspondencia entre parse error, transport failure, functional decline y UNCERTAIN (P0-01/P0-02).
3. **Implementar selección e inicialización.** PSE/PPSE, candidatos y selección explícita; después GPO/PDOL/AIP/AFL (P0-03/P0-04).
4. **Implementar lectura y estado de transacción.** AFL/READ RECORD, almacenamiento atómico, TVR/TSI y restrictions (P0-05/P0-06).
5. **Implementar decisiones.** CVM List/Results, TRM con TVR/TSI y TAA TAC/IAC (P0-07/P0-08). No capturar PIN; conservar handles externos.
6. **Implementar GENERATE AC y outcomes.** CDOL, CID/AC/ATC/IAD y mapeo coherente AAC/TC/ARQC a FunctionalResult sin equiparar fallo técnico con AAC (P0-09).
7. **Definir ODA como subsistema con frontera.** Selección, CAPK, certificados y resultados TVR/TSI antes de integrar CDA. Material `UNKNOWN` queda `BLOCKED` (P0-10).
8. **Completar online e issuer.** Host response, ARPC, EXTERNAL AUTHENTICATE, scripts, CDOL2 y final GAC; conservar host timeout/idempotencia de B (P0-11, P1-06).
9. **Separar kernels contact/contactless.** Selección AID/kernel y outcomes C-8 antes de relay/torn; compartir solo primitivas seguras, no decisiones por marcas inferidas (P0-12, P1-03).
10. **Ampliar recuperación y trazabilidad.** Cancelación en vuelo, read-back/restart, traza APDU redactada y replay (P1-04/P1-05/P1-07).
11. **Endurecer TLV/config/pruebas transversales.** Reglas de unicidad, perfiles versionados, RNG inyectado y fault injection local (P1-01/P1-08/P1-09/P1-11).
12. **No reutilizar código AGPL ni datos dudosos.** Para MIT/Zlib, cualquier reutilización futura debe conservar notice y registrar commit; tomkp queda pendiente de notice completo.

## 10. Pruebas recomendadas

Las rutas siguientes son referencias de ideas, no archivos importados. “Ya existe” evita duplicar cobertura actual y distingue coordinación B.

| Área | Prueba recomendada | Referencia concreta / situación actual |
|---|---|---|
| BER-TLV | Longitudes indefinidas/no mínimas, high-tag con prefijo cero, tags 00/FF, truncamiento, overflow de nodos/profundidad/valor, duplicados directos y ausencia de panic/excepción no controlada | Hyperion `src/tlv.rs` tests; Card Studio ya cubre parte en `tests/test_agent_a_kernel.py:TlvTests` |
| Unknown tags | Preservar unknown primitives en parser genérico; rechazarlos solo cuando un template estricto lo exija; nunca incluir sus valores en evidencia | Hyperion `src/tlv.rs`; tomkp `src/emv-tags.test.ts`; simulator `FaultInjectionTest.java:224` |
| Malformed TLV | Length delta, long-form no canónica, indefinite, tag truncado, constructed incorrecto, padding, tag omitido/duplicado | Simulator `FaultInjectionTest.java:188-221`; Hyperion `src/tlv.rs` |
| DOL | Datos ausentes/cortos/largos, output cap, tags duplicados/inválidos, política exacta y padding por especificación | Hyperion `src/dol.rs` tests; tomkp `src/emv-application.test.ts:806-909` como contraste |
| APDU | Casos sin datos/Le, Lc inconsistente, 61xx/6Cxx, cadenas acotadas, respuesta <2 bytes y SW por contexto | Hyperion `src/apdu.rs`; simulator `PaymentApplicationProtocolTest.java` y `PaymentApplicationProtocolT0Test.java` |
| PSE/PPSE | Contact PSE, contactless PPSE, PPSE sin READ RECORD, FCI incorrecto, Directory Entries y Kernel ID | Hyperion `src/apdu.rs`; simulator `PaymentSystemEnvironmentTest.java`, `ProximityPaymentSystemEnvironmentTest.java` |
| Multiple AID | Varias aplicaciones, misma prioridad, partial AID que conserva ADF completo, duplicados dentro/entre entries, sin common AID | Hyperion `src/selection.rs` tests; tomkp `src/emv-application.test.ts:771-803`; Card Studio ya exige AID explícito |
| Unsupported interface/kernel | AID incompatible con contact/contactless, Kernel ID desconocido, MAGNETIC_SIMULATED bloqueado y lector no sintético | Hyperion `src/selection.rs`; Card Studio `test_magnetic_and_non_simulated_interfaces_are_blocked` ya existe |
| GPO/AIP/AFL | Formatos 77/80, AIP ausente o longitud incorrecta, AFL ausente, SFI inválido, records invertidos/duplicados y límites | Hyperion `src/gpo.rs`, `src/afl.rs`; dcemv GPO como caso negativo `SPEC_WINS` |
| READ RECORD | Orden AFL, template vacío/anidado, duplicados, tags de terminal/host/GAC inyectados, conflictos entre records y atomicidad del DataStore | Hyperion `src/record.rs` tests |
| TVR/TSI | Reinicio por operación, bits simbólicos, RFU enmascarado y TSI solo después del procesamiento correspondiente | Hyperion `src/state.rs` tests |
| Restrictions/CVM | Fechas BCD inválidas, expiración, effective date, AUC, reglas X/Y, unsupported CVM, continue/fail y 9F34 sin PIN | Hyperion `src/restrictions.rs`, `src/cvm.rs`; dcemv CVM solo arquitectura |
| TRM/TAA | Floor, exception file, lower/upper velocity, merchant online, random boundary 4999/5000, denial > online > default | Hyperion `src/trm.rs`, `src/taa.rs`; conservar la determinism actual de Card Studio |
| GENERATE AC | Formatos 77/80, CID AAC/TC/ARQC/AAR, AC/ATC/IAD ausentes o longitud incorrecta, CDOL incorrecto y segundo GAC | Hyperion `src/gac.rs`, `src/cid.rs`; simulator `PaymentApplicationProtocolTest.java` |
| ODA | AIP bits, CAPK ausente/checksum, certificado/signature truncado, SDA/DDA/CDA fallido y TVR/TSI; material solo TEST aprobado | Hyperion `src/oda.rs`; simulator `PaymentApplicationProtocolTest.java` |
| Issuer | ARPC válido/inválido, EXTERNAL AUTHENTICATE repetido, ARC ausente, scripts antes/después, comando APDU inválido y segundo GAC | Hyperion `src/issuer.rs`; simulator `PaymentApplicationProtocolTest.java` |
| Contact/contactless parity | Mismas invariantes de error/idempotencia, pero APDU y outcomes específicos por kernel; T=0/T=1/contactless | Simulator `SimulatorTest.java`; Card Studio actual solo prueba paridad LAB |
| C-8 | Límite que exige alternate interface, CVM required, online required, Try Again con restart/UI coherente, relay timing y RECOVER AC | Hyperion `src/c8.rs`; simulator `ContactlessTransactionTest.java` |
| Card/reader removed | Antes de SELECT, durante GPO/record/GAC y después de respuesta posible; preservar outcome UNCERTAIN cuando corresponda | Card Studio `test_reader_uncertain_outcome_is_preserved` y `test_reader_certain_failure_is_not_decline`; ampliar por etapa |
| Timeout before send | FAILED + SAFE_TO_RETRY, `command_sent=false`, execution/retry explícito sin APDU duplicado | Card Studio ya existe en `test_timeout_before_send_is_safe_and_after_send_is_uncertain` |
| Timeout after send | UNCERTAIN + QUERY_REQUIRED/READ_BACK, nunca DECLINED ni retry ciego, en SELECT/GPO/record/GAC/host | Card Studio ya cubre el caso genérico; Hyperion `HostTimeout`; simulator `delayTest` |
| UNCERTAIN | Conservarlo tras query desconocida, restart y host timeout; nunca fabricar aprobación | Card Studio A y `tests/test_agent_b_terminal.py:test_restart_unknown_query_conserves_uncertain` ya cubren base |
| Duplicate/idempotency | Mismo request replay sin reejecutar, operation conflict, idempotency conflict y concurrent duplicate | A ya cubre secuencial; B `test_concurrent_duplicate_executes_only_once` y host duplicate cubren coordinación |
| Restart | Resultado conocido cargado, IN_PROGRESS, sesión APDU interrumpida, read-back y ausencia de reenvío | A `test_result_loader_recovers_known_result_after_restart`; B restart tests; extender a estados EMV reales |
| Reversal | Original aprobado, importe/moneda, duplicate reversal, timeout de reversal y recuperación sin doble envío | B `test_reversal_and_duplicate_reversal` y `card_studio/l3/scenarios.py`; definir después comportamiento APDU |
| Partial refund | Refund parcial, acumulado exacto, exceso, referencia original y conflicto de idempotencia | B `test_partial_refund_limits_and_original_references` y L3; no duplicar en A salvo cambio de kernel |
| Host timeout | Host ya recibió ARQC pero terminal no conoce respuesta: UNCERTAIN, host query, sin reversal ni segunda autorización ciega | B `test_host_idempotency_restart_conflict_timeout_and_safe_retry`; Hyperion `HostTimeout` |
| Redaction | APDU/TLV/PIN/PAN/track/claves/ARPC/script no aparecen en logs, `Debug`, excepciones ni replay; metadatos permitidos sí | Hyperion `src/trace.rs`, `src/tlv.rs`, `src/gac.rs`; Card Studio `test_result_details_are_simulation_marked_and_redacted` |

## AGENT_A_GAP_RESULT

| Component | Current | Target | Gap | Priority | Reference |
|---|---|---|---|---|---|
| BER-TLV | EXISTS: parser/encoder estricto LAB con límites y unknown tags | EXISTS: parser EMV con reglas contextuales y provenance | PARTIAL: tags inválidos, unicidad y hardening contextual | P1 | Card Studio `tlv/codec.py`; Hyperion `src/tlv.rs` |
| DOL | MISSING: no DOL/DataStore | EXISTS: PDOL/CDOL/DDOL/TDOL según flujo | MISSING: parser, builder, padding, ownership y límites | P0 | Hyperion `src/dol.rs` |
| APDU | PARTIAL: cuatro comandos privados | EXISTS: builders/parsers y status contextuales | PARTIAL: modelo general, Le, 61xx/6Cxx y SW por contexto | P0 | Hyperion `src/apdu.rs`; simulator `EmvApplet.java` |
| SELECT | PARTIAL: SELECT directo de AID de perfil | EXISTS: SELECT por DF con FCI final validado | PARTIAL: Le/FCI y candidate flow real | P0 | Hyperion `src/apdu.rs`, `src/selection.rs` |
| PSE/PPSE | MISSING | EXISTS: contact PSE y contactless PPSE | MISSING: entornos y diferencias de records | P0 | Hyperion `src/apdu.rs`; simulator PSE/PPSE tests |
| AID selection | PARTIAL: perfil, prioridad y anti-ambigüedad | EXISTS: Directory Entries, exact/partial, kernel mapping | PARTIAL: FCI, duplicados y selección real | P0 | Hyperion `src/selection.rs` |
| GPO | MISSING: INITIALIZE privado no es GPO | EXISTS: tag 83/PDOL y respuestas 77/80 | MISSING | P0 | Hyperion `src/gpo.rs`; Card Studio `emv/protocol.py` |
| AIP | MISSING: tag 82 contiene marcador no AIP | EXISTS: dos bytes y capability bits | MISSING | P0 | Hyperion `src/aip.rs` |
| AFL | MISSING | EXISTS: validación y record plan | MISSING | P0 | Hyperion `src/afl.rs` |
| READ RECORD | MISSING: GET DATA privado no es READ RECORD | EXISTS: lectura AFL, template 70 y DataStore atómico | MISSING | P0 | Hyperion `src/record.rs` |
| TVR | MISSING | EXISTS: cinco bytes, bits y RFU | MISSING | P0 | Hyperion `src/state.rs` |
| TSI | MISSING | EXISTS: dos bytes, bits por procesamiento | MISSING | P0 | Hyperion `src/state.rs` |
| Processing Restrictions | PARTIAL: estado y comprobación de interfaz | EXISTS: versión, fechas, AUC y TVR | PARTIAL: reglas EMV ausentes | P0 | Hyperion `src/restrictions.rs` |
| CVM | PARTIAL: método sintético desde perfil, sin PIN | EXISTS: CVM List/reglas/resultados y capacidades | PARTIAL: 8E, X/Y, condiciones, 9F34 y TVR | P0 | Hyperion `src/cvm.rs` |
| TRM | PARTIAL: floor, interfaz y muestra determinista | EXISTS: TVR/TSI, excepciones y velocity | PARTIAL: contadores no volátiles y bits | P0/P1 | Hyperion `src/trm.rs` |
| TAA | PARTIAL: precedencia local de acciones | EXISTS: TVR con TAC/IAC y online capability | PARTIAL: action codes y AAC/TC/ARQC reales | P0 | Hyperion `src/taa.rs` |
| GENERATE AC | MISSING: INS AE privado sin CDOL real | EXISTS: primer/final GAC con CDOL | MISSING | P0 | Hyperion `src/apdu.rs`, `src/gac.rs` |
| Cryptogram result | MISSING: texto funcional, no CID/AC/ATC | EXISTS: AAC/TC/ARQC y objetos asociados | MISSING | P0 | Hyperion `src/cid.rs`, `src/gac.rs` |
| ODA boundary | MISSING | EXISTS o BLOCKED por material: selección/resultado y trust boundary explícitos | MISSING: AIP/CAPK/certificados/SDA/DDA/CDA | P0 | Hyperion `src/oda.rs` |
| Issuer Authentication | MISSING | EXISTS: ARC/ARPC y EXTERNAL AUTHENTICATE | MISSING | P0 | Hyperion `src/issuer.rs`; simulator protocol tests |
| Issuer Scripts | MISSING | EXISTS: fases antes/después y TVR/TSI | MISSING | P0 | Hyperion `src/issuer.rs`; dcemv State 11 |
| Contact | PARTIAL: transporte LAB, no flujo EMV | EXISTS: flujo contact y T=0/T=1 según alcance | PARTIAL: protocolo completo | P0 | Hyperion `src/fsm.rs`; simulator `SimulatorTest.java` |
| Contactless | PARTIAL: mismo protocolo LAB con límites | EXISTS: selección kernel y flujo contactless | PARTIAL: PPSE/TTQ/CTQ/kernels reales | P0 | dcemv `KernelContactless/Kernels`; simulator contactless tests |
| C-8 relevant behavior | MISSING salvo límite genérico por interfaz | EXISTS: outcomes/start/UI/límites; relay/torn según alcance | MISSING: comportamiento estructurado | P0/P1 | Hyperion `src/c8.rs` |
| State machine | PARTIAL: FSM LAB estricta e historial | EXISTS: FSM con ODA, GAC1/2, issuer y C-8 | PARTIAL: estados/eventos reales | P0 | Card Studio `kernels/state.py`; Hyperion `src/fsm.rs` |
| Session lifecycle | EXISTS: open/execute/online/query/cancel/close | EXISTS: preservar contrato y ampliar recuperación | PARTIAL: sesión APDU en curso/restart | P1 | Card Studio `internal_emv.py`, `session.py` |
| Error handling | PARTIAL: técnico separado de DECLINED | EXISTS: parse/SW/transporte contextual sin falsos rechazos | PARTIAL: SW y mandatory data por contexto | P0 | Card Studio `internal_emv.py`; Hyperion `src/error.rs` |
| Cancellation | PARTIAL: idempotente, no interrumpe exchange bloqueado | EXISTS: cancelación en vuelo segura e idempotente | PARTIAL | P1 | Card Studio `internal_emv.py` |
| Timeouts | PARTIAL: before/after send correctos en kernel | EXISTS: timeout por etapa y host con recuperación | PARTIAL: cobertura por etapa/integración online | P1 | Card Studio A y B tests |
| UNCERTAIN | EXISTS: conservado con QUERY_REQUIRED | EXISTS: preservar en todos los puntos inciertos | PARTIAL: etapas EMV aún inexistentes | P1 | Card Studio `internal_emv.py`; B restart tests |
| Recovery/query | EXISTS: resultado conocido o NOT_FOUND; loader externo | EXISTS: query/read-back sin fabricar resultado | PARTIAL: read-back APDU/host y sesión en curso | P1 | Card Studio `internal_emv.py`; B tests |
| Cancel/close | EXISTS: idempotentes | EXISTS: conservar | EXISTS: sin gap contractual LAB; revisar cancelación en vuelo | P1 | Card Studio `test_idempotency_query_duplicate_execution_cancel_and_close` |
| Restart | PARTIAL: restaura TransactionResult | EXISTS: resultado e IN_PROGRESS/read-back | PARTIAL: estado APDU y host | P1 | A result loader; B restart tests |
| Reversal | PARTIAL fuera del kernel: validación B/L3 | EXISTS: semántica service/host y recuperación sin duplicados | REVIEW_REQUIRED: comportamiento kernel/APDU por tipo | P1 | B terminal tests; `l3/scenarios.py` |
| Partial refund | EXISTS fuera del kernel en B/L3 | EXISTS: conservar límites y referencias | NOT_REQUIRED en A salvo cambio de semántica kernel | P1 | B terminal tests; `l3/runner.py` |
| Host online/timeout | PARTIAL: complete_online textual; host timeout cubierto por B | EXISTS: ARPC/final GAC, timeout e idempotencia | PARTIAL: integración issuer real | P0/P1 | Hyperion `src/issuer.rs`; B host tests |
| Trace redaction | PARTIAL: detalles sanitizados y metadata TLV | EXISTS: traza APDU contextual/replay redactada | PARTIAL: secuencia, SW, políticas y replay | P1 | Hyperion `src/trace.rs`; Card Studio `tlv/evidence.py` |
| Config/profile | PARTIAL: digest, capacidades, riesgo y CVM | EXISTS: perfiles scheme/kernel con provenance | PARTIAL: DOL/TAC/IAC/ODA/CAPK/signature | P1 | Hyperion `src/config.rs` |
| RNG/Unpredictable Number | MISSING: no existe GPO real ni fuente UN | EXISTS: fuente inyectada y pruebas de salida débil | MISSING/BLOCKED hasta definir frontera | P1 | Hyperion RNG tests; contraste tomkp RNG |
| Refund/reversal semantics | PARTIAL: transaction types declarados, flujo LAB idéntico | EXISTS: reglas explícitas por tipo | REVIEW_REQUIRED | P1 | Card Studio `internal_emv.py`; B tests |
| Compliance/certification/L3 readiness | NOT_REQUIRED para esta revisión | NOT_REQUIRED: sin declaración | NOT_REQUIRED | NOT_REQUIRED | Alcance de este documento |
