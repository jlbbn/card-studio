# Borrador local — EMVCo SBMP Product Registration Questionnaire

Estado: **BORRADOR NO ENVIADO / ALCANCE NO CONFIRMADO / SIN SECN**.

Este archivo prepara información local; no sustituye el formulario DOCX/PDF oficial ni una presentación al Secretariat. No se ha contactado con EMVCo ni con un laboratorio. Los campos desconocidos permanecen como `PENDIENTE` para evitar inventar datos administrativos, versiones o certificados.

## Regla de configuración

El formulario exige un cuestionario separado para cada configuración de producto, incluidos hardware y versión de SO. Este borrador no agrupa Linux, macOS y Android como si fueran una única configuración evaluada.

## 1. Información general

| Campo oficial | Valor local |
|---|---|
| Company Name | `PENDIENTE: entidad legal registrada como SBMP Product Provider` |
| Security Laboratory Name | `PENDIENTE: laboratorio reconocido seleccionado mediante el proceso EMVCo` |
| Subcontracting Security Laboratory(ies) | `PENDIENTE; declarar todos los que correspondan` |

## 2. Tipo de evaluación

| Campo | Valor local |
|---|---|
| New product evaluation | Candidato solamente si EMVCo confirma que el producto entra en SBMP |
| Parent SECN para producto lineal | `NO CONSTA` |
| Update evaluation | `NO APLICA MIENTRAS NO EXISTA UN CERTIFICADO CONFIRMADO` |
| SECN | `NO CONSTA` |
| Delta / Derivative | `NO DETERMINADO; requeriría SIA de un producto previamente evaluado` |
| Producto/componente emitido en campo | `PENDIENTE DE CONFIRMACIÓN DEL RESPONSABLE` |
| Renewal evaluation | `NO APLICA MIENTRAS NO EXISTA UN CERTIFICADO CONFIRMADO` |

No marcar simultáneamente opciones incompatibles en el formulario final.

## 3. Información del producto

### 3.1 Identificación

| Campo oficial | Valor local |
|---|---|
| Product name | `Swift Ledger Card Studio` — nombre provisional para el expediente |
| Product version | `PENDIENTE: el repositorio no define una versión única del producto completo` |
| Supported Operating System | Linux y macOS para la aplicación de escritorio; Android para Card Bridge. Separar por configuración en el formulario final |
| Product type | `PENDIENTE DE ACUERDO DE ALCANCE CON EMVCo` |

No seleccionar `Mobile Application` solo porque exista un componente Android. El producto actual es sandbox y no procesa pagos EMV o fondos reales. Tampoco seleccionar SDK, TEE, CDCVM, MFA, Attestation o Software Protection Tool sin una implementación y evidencia correspondientes.

### 3.2 Componentes

| Component type | Component name | Component version | Component Provider | SECN |
|---|---|---|---|---|
| `PENDIENTE: alcance` | Swift Ledger Card Studio Desktop | `PENDIENTE` | `PENDIENTE: entidad legal` | `NO CONSTA` |
| `PENDIENTE: alcance` | SwiftLedger Card Bridge para Android | `1.0` (`versionCode=1`) | `PENDIENTE: entidad legal` | `NO CONSTA` |
| `PENDIENTE: alcance` | Applet/protocolo propietario Swift Ledger | `PENDIENTE` | `PENDIENTE` | `NO CONSTA` |
| Dependencia o componente por decidir | API sandbox Swift Ledger | `PENDIENTE; código fuera de este repositorio` | `PENDIENTE` | `NO CONSTA` |
| Dependencia o componente por decidir | Adaptadores PC/SC, Cardpeek y ADB/SUNMI | Según configuración; `PENDIENTE` | Proveedores por inventariar | `NO CONSTA` |

La tabla es un inventario inicial, no una declaración de que todos los elementos pertenezcan al Target of Evaluation. Añadir TEE, MFA, attestation, bibliotecas criptográficas, herramientas de protección y controles de servidor únicamente si existen y entran en el alcance acordado.

### 3.3 Guías de usuario y seguridad

| Documento local | Estado para registro |
|---|---|
| `README.md` | Guía general actual; no es por sí sola una guía de seguridad evaluada |
| `docs/MACOS.md` | Guía de instalación/ejecución macOS actual; requiere versión y controles de configuración si entra en alcance |
| `docs/EMVCO_SBMP.md` | Análisis interno de brecha; no es guía de seguridad para usuarios |
| Guía de seguridad del producto | `PENDIENTE: crear, identificar y versionar para conservar la configuración evaluada` |

## 4. EMVCo Payment Systems

`PENDIENTE DE AUTORIZACIÓN DEL RESPONSABLE`.

El formulario indica seleccionar al menos un Payment System para un reporte común. Las opciones oficiales son American Express, Discover, JCB, Mastercard, UnionPay y Visa. Para reportes member-specific se selecciona el destinatario correspondiente. Ninguna selección local autoriza por sí misma el envío de reportes ni implica aceptación por esas redes.

## 5. Contacto

| Campo | Valor local |
|---|---|
| Contact Name | `PENDIENTE` |
| Contact email | `PENDIENTE` |
| Date Completed | `PENDIENTE: fecha del formulario final, no de este borrador` |

## 6. Autorización

| Campo | Valor local |
|---|---|
| Name | `PENDIENTE: persona con facultad para autorizar la evaluación` |
| Email | `PENDIENTE` |
| Date Authorized | `PENDIENTE` |

## 7. Evidencia necesaria antes de completar el formulario oficial

- [ ] Acuerdo de alcance SBMP y categoría de producto.
- [ ] Configuración exacta de hardware, SO y arquitectura.
- [ ] Nombre y versión únicos del producto.
- [ ] Inventario de componentes, proveedores, versiones y SECN verificables.
- [ ] Entidad legal, laboratorio y subcontratistas.
- [ ] Confirmación de si el producto ya fue emitido en campo.
- [ ] Guía de seguridad de usuario versionada.
- [ ] Payment Systems y tipo de distribución de reporte autorizados.
- [ ] Contacto y autorizador formal.
- [ ] Revisión de consistencia entre cuestionario, diseño e informe de laboratorio.

El formulario oficial completado debe convertirse a PDF y enviarse por el canal indicado por EMVCo únicamente con autorización del responsable. Este borrador no se envía ni se genera como PDF automáticamente.
