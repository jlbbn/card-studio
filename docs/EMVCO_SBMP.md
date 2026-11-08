# EMVCo SBMP — alcance y brecha de Card Studio

Estado del producto: **SANDBOX / NO EVALUADO POR EMVCo / SIN SECN**.

Este documento registra el análisis local de los dos entregables finales proporcionados por el responsable. No es un certificado, una evaluación de laboratorio ni evidencia de cumplimiento. Prohibido presentar Card Studio, Card Bridge o sus componentes como aprobados, evaluados o certificados por EMVCo mientras no exista una carta o certificado emitido por EMVCo.

## 1. Fuentes analizadas

| Documento | Versión identificada | SHA-256 |
|---|---|---|
| `EMVCo-SBMP-17-P01-V2.0_EMVCo_SBMP_Security_Evaluation_Process.pdf` | EMVCo SBMP Security Evaluation Process v2.0, mayo de 2026 | `a85c44113257da396062ee26af134b2782c28022b8b740d0f551c7ec6129e09e` |
| `Product_Registration_Questionnaire-SBMP_Product-Providers_March_2025.docx` | Product Registration Questionnaire, marzo de 2025 | `958df20d11ddc4f47da081ef2fc51cd7259c558fb965c9be9711b0261186318e` |

Los originales permanecen en `/home/admin1/Descargas`. El proceso es la fuente de procedimiento; el cuestionario es un formulario y no demuestra que se haya registrado ningún producto.

## 2. Conclusión de aplicabilidad

Los documentos recibidos describen la evaluación de seguridad de productos **Software-Based Mobile Payment (SBMP)**. No son una versión nueva del código de Card Studio, un kernel EMV Level 2, requisitos funcionales de tarjetas chip/contactless ni una certificación de aceptación por una red de pagos.

La aplicación de escritorio actual no debe clasificarse automáticamente como Mobile Payment Application (MPA). Card Bridge sí se ejecuta en Android, pero su presencia en un dispositivo móvil tampoco basta para declarar que el conjunto es un producto SBMP. El alcance debe acordarse con EMVCo a partir de los activos, las funciones de seguridad, la arquitectura y el uso previsto.

La evaluación SBMP es, por tanto, una **ruta futura condicionada**. No sustituye evaluaciones funcionales EMV, certificación de kernels, aprobación de redes, adquirencia ni validación física de lectores y tarjetas.

## 3. Inventario real del repositorio

| Elemento | Estado comprobado en el código | Frontera para una evaluación |
|---|---|---|
| Card Studio de escritorio | Python/GTK para Linux y macOS; tarjeta virtual, saldo MXN simulado, recargas y cobros sandbox | No es una MPA por el solo hecho de operar pagos simulados; versión de producto aún no definida |
| Protocolo propio | Identificadores `SL-…`, APDU propietario y selección del applet Swift Ledger | No implementa ni demuestra un kernel EMV; applet, proveedor y versión deben identificarse si entran en alcance |
| Adaptadores físicos | PC/SC en Linux/macOS, Cardpeek y puente SUNMI por ADB | Las pruebas simuladas no demuestran compatibilidad ni seguridad física |
| Card Bridge para Android | `android:versionName="1.0"`, NFC ISO-DEP, HTTP limitado a `127.0.0.1:5380`, `debuggable=false`, `allowBackup=false` y `usesCleartextTraffic=false` | Componente candidato cuya categoría y activos requieren acuerdo de alcance; no tiene SECN registrado en este repositorio |
| Cliente Swift Ledger | Firma HMAC-SHA256, timestamp, nonce, idempotencia y comprobación explícita de `simulation=true` | La API y sus controles de servidor están fuera de este repositorio; cualquier evaluación debe definir si son componentes, dependencias o controles externos |
| Persistencia local | JSON atómico con archivos `0600`; configuración no secreta | No se ha demostrado protección de activos de pago ni cumplimiento de requisitos SBMP |
| Pruebas | Unitarias y adaptadores simulados; sin red, tarjetas reales ni lectores físicos | No sustituyen una evaluación independiente de laboratorio |

No se encontró en este repositorio evidencia de TEE/vTEE, CDCVM, MFA, attestation, white-box cryptography, anti-instrumentation ni un paquete de Software Protection Tools evaluable. No se afirma que esas funciones sean obligatorias para todo producto; su aplicabilidad depende del diseño y alcance aprobados.

## 4. Controles existentes que no equivalen a certificación

El código sí contiene controles útiles para el sandbox:

- llaves de terminal fuera de `settings.json` y de los paquetes;
- rechazo de URL con usuario/contraseña, query o fragmento;
- firma HMAC del cuerpo canonicalizado y uso de nonce;
- estados de sincronización idempotente;
- puente Android escuchando únicamente en loopback;
- paquetes Android no depurables y sin backup;
- pruebas de firma y operaciones sandbox.

Estos controles describen el estado actual, no una evaluación SBMP. Tampoco demuestran por sí solos los requisitos mínimos citados por el proceso para productos SDK/MPA: MA-SEC-REQ-2.1, 2.3, 2.4 y 2.5. El contenido de esos requisitos no fue proporcionado y no se ha hecho un análisis de conformidad.

## 5. Novedades y reglas relevantes del proceso v2.0

1. **Componentes identificables.** TEE/vTEE, CDCVM, MFA, Software Protection Tools, attestation, SDK y MPA deben identificarse de forma única cuando estén en alcance.
2. **Funciones y activos mínimos.** EMVCo no emite certificados para componentes sin funciones de seguridad o sin activos que proteger, como un SDK puramente funcional.
3. **Ataque preliminar.** La versión 2.0 introduce esta clasificación: no compromete directamente un activo en alcance, pero crea oportunidad para otros ataques. Rootear un dispositivo se cita como ejemplo.
4. **Categorías para SDK/MPA.** Categoría 1: sin ataques parciales/completos; Categoría 2: ataques parciales y/o completos clasificados de Low a High según la metodología; Categoría 3: otros productos con cumplimiento limitado. Ninguna categoría puede asignarse localmente sin evaluación.
5. **Auditoría de procesos.** Según el producto y sus activos, puede incluirse la infraestructura de desarrollo, producción y entrega.
6. **Vigencia y revisión.** El certificado y la inclusión en la lista de productos evaluados tienen un periodo de un año, sujeto a revisión anual, retiro o sustitución del producto.
7. **Cambios.** Una modificación puede requerir Security Impact Analysis (SIA), evaluación Delta si hay impacto de seguridad o Derivative si no lo hay. La clasificación no se decide unilateralmente en este documento.
8. **Productos en campo.** El proceso restringe los cambios cubiertos por un certificado existente cuando el producto ya fue emitido en campo; cualquier actualización debe revisarse con EMVCo y el laboratorio.

## 6. Documentos relacionados que faltan

El proceso v2.0 remite a las versiones vigentes en su fecha de publicación. Antes de diseñar o declarar requisitos técnicos hay que obtener y revisar, según el alcance:

| Referencia | Versión citada por el proceso |
|---|---|
| SBMP Security Requirements | 1.5, enero de 2025 |
| CDCVM Security Requirements | 1.0, septiembre de 2018 |
| MFA Security Requirements | 1.0, junio de 2023 |
| Security Evaluation Laboratory Requirements for SBMP | 2.4, enero de 2026 |
| SBMP Security Evaluation Methodology | 2.1, mayo de 2026 |
| SBMP Guidelines for TEE-based Mobile Payment | 1.0, enero de 2018 |
| SBMP Guidelines for Software Protection Tools | 1.0, enero de 2019 |
| SBMP Guidelines for Attestation | 1.0, julio de 2018 |
| Security Evaluation Bulletin 15 — Evaluation Review Fees | 2, octubre de 2021 |
| Security Evaluation Bulletin 17 — Evaluation and Testing Deliverables | 2, julio de 2025 |

Estos documentos no están incluidos en el par recibido. No debe inferirse cumplimiento a partir de sus títulos o números de requisito.

## 7. Ruta de preparación propuesta

### E0 — Decisión de producto y alcance

- Confirmar si Card Studio permanece como sandbox o se creará un producto de pago móvil separado.
- Definir producto, configuración, plataformas, activos, funciones de seguridad, usuarios y frontera entre cliente, Card Bridge, applet y API.
- Solicitar la discusión inicial con EMVCo antes de seleccionar una categoría de producto.
- Salida: alcance escrito y aprobado; sin código ni afirmaciones de certificación.

### E1 — Identidad y expediente

- Asignar nombre y versión únicos al producto y a cada componente.
- Preparar un cuestionario por cada configuración de hardware y versión de SO que entre en alcance.
- Registrar proveedores y SECN existentes, sin inventar valores.
- Crear guía de seguridad para usuarios que mantenga el producto dentro de la configuración evaluada.
- Salida: borrador completo de `EMVCO_REGISTRO_BORRADOR.md`, inventario de componentes y guía versionada.

### E2 — Diseño de seguridad y trazabilidad

- Obtener los requisitos aplicables y crear una matriz requisito → activo → control → implementación → prueba → evidencia.
- Documentar arquitectura, flujos, ciclo de vida, sitios de desarrollo/producción/entrega y dependencias externas.
- Realizar análisis de amenazas y vulnerabilidades, incluidos bypass de plataforma, reversing, modificación, interfaces, extracción en runtime, análisis estático/dinámico, fault/side-channel, RNG, replay/relay/downgrade, IPC y fuzzing según aplique.
- Salida: diseño y productos de prueba reproducibles; todavía sin declarar cumplimiento.

### E3 — Evaluación independiente

- Firmar el acuerdo EMVCo y seleccionar laboratorio reconocido mediante el proceso indicado por EMVCo.
- Entregar diseño, productos de prueba, evidencias previas y guías.
- Permitir que el laboratorio derive y ejecute las pruebas, documente vulnerabilidades residuales y proponga categoría cuando corresponda.
- Salida: informe de laboratorio completo; las pruebas unitarias locales son solo evidencia complementaria.

### E4 — Revisión y certificado

- Mantener el cuestionario sincronizado con el informe.
- Usar el canal de reporte común o member-specific definido por el laboratorio, con distribución cifrada y autorización de destinatarios.
- Esperar revisión del Secretariat y aprobación del SEWG.
- Salida: certificado y SECN emitidos por EMVCo, o rechazo/solicitud de evidencia adicional. No crear un SECN local.

### E5 — Mantenimiento

- Monitorear amenazas, vulnerabilidades y cambios de componentes.
- Elaborar SIA para cambios y distinguir Delta/Derivative con el laboratorio y EMVCo.
- Preparar revisión anual con los plazos del proceso: actividades de renovación no antes de seis meses y entrega del informe no antes de cuatro meses antes de la fecha de revisión anual.
- Salida: certificado vigente, renovado, actualizado o retirado según la resolución oficial.

## 8. Decisiones pendientes del responsable

- Entidad legal registrada como Product Provider.
- Si el alcance futuro es una MPA, un SDK, Card Bridge como componente u otro producto.
- Configuraciones de hardware/SO que se evaluarían.
- Laboratorio, subcontratistas, contacto y firmante autorizado.
- Activos y funciones de seguridad reales del producto candidato.
- Componentes externos y SECN verificables, si existen.
- Payment Systems autorizados a recibir reportes comunes.
- Versión de producto y guía de seguridad que se someterían.

Mientras estas decisiones y evidencias no existan, el estado permanece **NO EVALUADO / SIN SECN** y el sandbox no cambia a pagos reales.
