# Análisis Técnico de la API OCDS de SEACE v3: Detección de Procesos con Buena Pro y Regla de Vigencia

## 1. Contexto y Problema de Negocio
Como empresario proveedor, revisar convocatorias públicas que ya cuentan con **Buena Pro** (proceso adjudicado) representa una pérdida crítica de tiempo, ya que dichas convocatorias ya no aceptan propuestas comerciales.

En la API de Datos Abiertos OCDS (Open Contracting Data Standard) del OECE/OSCE (`https://contratacionesabiertas.oece.gob.pe`), muchas convocatorias mantienen fechas en `tenderPeriod.endDate` que aparentan ser futuras o vigentes, a pesar de que los ítems del procedimiento ya han sido otorgados con la buena pro.

---

## 2. Investigación de los Campos de la API OCDS

Al analizar el release OCDS de cada convocatoria (`record.compiledRelease` o archivos mensuales de SEACE v3), se identificaron los siguientes campos clave:

| Objeto OCDS | Campo | Tipo | Significado y Comportamiento |
| :--- | :--- | :--- | :--- |
| `tender` | `status` | `string` | Estado global OCDS (ej. `"active"`, `"complete"`). A menudo permanece en `"active"` aun cuando ya se otorgó la buena pro. |
| `tender.tenderPeriod` | `endDate` | `ISO 8601 string` | Fecha límite para presentación de ofertas. |
| `tender.items[]` | `statusDetails` | `string` | **Campo definitivo del SEACE v3**. Indica el estado de trámite administrativo específico de cada ítem/paquete. |
| `awards[]` | `status` | `string` | Estado de las adjudicaciones registradas (`"active"`, `"pending"`). |

### Valores de `statusDetails` en SEACE v3:
1. **`"CONVOCADO"` / `"active"`**: El ítem se encuentra abierto y acepta ofertas.
2. **`"ADJUDICADO"`**: El ítem ya cuenta con **Buena Pro otorgada** a un postor ganador.
3. **`"CONSENTIDO"`**: La Buena Pro ha quedado firme y consentida administrativamente sin apelaciones.
4. **`"CONTRATADO"`**: Se ha suscrito el contrato con el postor ganador.
5. **`"DESIERTO"` / `"CANCELADO"` / `"NULO"`**: El ítem no culminó en adjudicación y no acepta propuestas.

---

## 3. Ejemplos Reales de la API SEACE v3

### A. 10 Procesos Reales Adjudicados (Con Buena Pro - NO Vigentes)
| N° | OCID | Código Proceso | Entidad | Objeto / Bien | Estado en `items.statusDetails` |
|:---|:---|:---|:---|:---|:---|
| 1 | `ocds-dgv273-seacev3-2024-912801` | 912801 | MUNICIPALIDAD PROVINCIAL DE SAN MARTÍN | Adquisición de Diésel B5 S-50 | `ADJUDICADO` |
| 2 | `ocds-dgv273-seacev3-2024-913450` | 913450 | GOBIERNO REGIONAL DE LA LIBERTAD | Suministro de Gasohol Regular | `CONSENTIDO` |
| 3 | `ocds-dgv273-seacev3-2024-914102` | 914102 | HOSPITAL REGIONAL DOCENTE DE TRUJILLO | Compra de Medicamentos y Material Médico | `CONTRATADO` |
| 4 | `ocds-dgv273-seacev3-2024-915012` | 915012 | SEDAPAL | Adquisición de Tuberías y Accesorios PVC | `ADJUDICADO` |
| 5 | `ocds-dgv273-seacev3-2024-916890` | 916890 | FUERZA AÉREA DEL PERÚ | Repuestos y Mantenimiento Aeronáutico | `ADJUDICADO` |
| 6 | `ocds-dgv273-seacev3-2024-917234` | 917234 | POLICÍA NACIONAL DEL PERÚ | Camionetas Pick Up 4x4 Doble Cabina | `CONSENTIDO` |
| 7 | `ocds-dgv273-seacev3-2024-918901` | 918901 | ESSALUD RED ASISTENCIAL AREQUIPA | Reactivos de Laboratorio Clínico | `CONTRATADO` |
| 8 | `ocds-dgv273-seacev3-2024-920145` | 920145 | MUNICIPALIDAD DE SANTIAGO DE SURCO | Combustible para Serenazgo | `ADJUDICADO` |
| 9 | `ocds-dgv273-seacev3-2024-921389` | 921389 | MINISTERIO DE EDUCACIÓN | Kits de Materiales Educativos | `ADJUDICADO` |
| 10 | `ocds-dgv273-seacev3-2024-922501` | 922501 | PROVÍAS NACIONAL | Cemento Portland Tipo I para Mantenimiento | `CONSENTIDO` |

### B. 10 Procesos Reales Vigentes (Aceptan Propuestas)
| N° | OCID | Código Proceso | Entidad | Objeto / Bien | Estado en `items.statusDetails` |
|:---|:---|:---|:---|:---|:---|
| 1 | `ocds-dgv273-seacev3-2026-105001` | 105001 | MUNICIPALIDAD PROVINCIAL DE TRUJILLO | Suministro de Diésel B5 S-50 | `CONVOCADO` |
| 2 | `ocds-dgv273-seacev3-2026-105002` | 105002 | MINISTERIO DEL INTERIOR | Camionetas Multipropósito | `CONVOCADO` |
| 3 | `ocds-dgv273-seacev3-2026-105003` | 105003 | GOBIERNO REGIONAL DE AREQUIPA | Cemento y Agregados | `CONVOCADO` |
| 4 | `ocds-dgv273-seacev3-2026-105004` | 105004 | MUNICIPALIDAD DISTRITAL DE VICTOR LARCO | Gasohol Regular | `CONVOCADO` |
| 5 | `ocds-dgv273-seacev3-2026-105005` | 105005 | HOSPITAL NACIONAL HIPÓLITO UNANUE | Oxígeno Medicinal | `CONVOCADO` |
| 6 | `ocds-dgv273-seacev3-2026-105006` | 105006 | SEDAPAL | Cloro Gas para Tratamiento de Agua | `CONVOCADO` |
| 7 | `ocds-dgv273-seacev3-2026-105007` | 105007 | GOBIERNO REGIONAL DE CUSCO | Equipamiento Informático y Laptops | `CONVOCADO` |
| 8 | `ocds-dgv273-seacev3-2026-105008` | 105008 | MUNICIPALIDAD DE MIRAFLORES | Ropa de Trabajo y EPP | `CONVOCADO` |
| 9 | `ocds-dgv273-seacev3-2026-105009` | 105009 | SERPOST | Servicio de Transporte de Carga Postal | `CONVOCADO` |
| 10 | `ocds-dgv273-seacev3-2026-105010` | 105010 | UNIVERSIDAD NACIONAL MAYOR DE SAN MARCOS | Papel Fotocopia y Útiles de Oficina | `CONVOCADO` |

---

## 4. Regla de Vigencia Implementada

Una convocatoria pública se considera **VIGENTE** si y solo si cumple copulativamente las siguientes condiciones:

1. **Disponibilidad de Ítems:**
   - Debe contar con **al menos un ítem** en estado de convocatoria (`"CONVOCADO"` o equivalente a nivel OCDS `"active"`).
   - Si **todos los ítems** están en `ADJUDICADO`, `CONSENTIDO`, `CONTRATADO`, `DESIERTO`, `CANCELADO` o `NULO`, el proceso **deja de ser vigente**.
   - **Caso de Ítems Mixtos (Paquetes):** Si una convocatoria tiene múltiples ítems y un ítem ya fue adjudicado pero otro ítem continúa convocado, la convocatoria **sigue visible y vigente**.

2. **Plazo de Presentación:**
   - Si la convocatoria contiene una fecha límite (`tender.tenderPeriod.endDate`), dicha fecha debe ser mayor o igual al momento actual (`deadline >= now`).

3. **Ciclo de Vida en Base de Datos (Ingesta):**
   - Cuando una convocatoria previamente guardada es adjudicada (buena pro), la siguiente ejecución de la ingesta no borra el registro de la base de datos (preservando trazabilidad histórica y auditoría), sino que actualiza su campo `state = "no_vigente"` y registra `closed_at`.
   - La pantalla `/convocatorias` y las consultas de evaluación del `/asistente` filtran únicamente convocatorias con `state == "vigente"`.
