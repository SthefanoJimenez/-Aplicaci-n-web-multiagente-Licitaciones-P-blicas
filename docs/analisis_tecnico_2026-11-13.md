# Análisis técnico integral — LicitaFácil

**Fecha:** 13 de noviembre de 2026
**Repositorio:** `-Aplicaci-n-web-multiagente-Licitaciones-P-blicas` (rama `main`, commit `845d1f7`)
**Método:** lectura completa del código de `app/`, perfilado de los datasets con pandas, ejecución de la suite de pruebas, inspección del historial Git, de la configuración de despliegue y de los artefactos de `analisis/`.

---

## 1. Resumen ejecutivo

LicitaFácil **no es un prototipo de taller: es un producto funcional y sorprendentemente amplio** para un proyecto académico. Tiene 38 rutas, cuentas con sesiones en servidor, documentos PDF privados con validación aislada, consorcios, ingesta diaria desde la API OCDS del OECE, RAG sobre las bases, cronograma de etapas y un flujo de evaluación de 5 pasos con veredicto. La suite tiene **355 pruebas (350 pasan)**.

Dicho eso, hay **cuatro problemas que deben resolverse antes de presentar el sistema como listo para usuarios reales**:

| # | Problema | Impacto |
|---|---|---|
| 1 | El asistente de postulación **inventa requisitos y números de página de las bases** cuando no encuentra texto, y los presenta como citas literales | Riesgo reputacional y legal: un MYPE podría decidir no postular (o postular) por una cita falsa |
| 2 | **No se verifica el correo al registrarse** y las invitaciones a consorcios se resuelven solo por correo | Cualquiera puede registrarse con el correo de otra empresa y ver/aceptar sus invitaciones, accediendo a ventas, personal y montos ajenos |
| 3 | El parseo de **archivos descargados del SEACE** (ZIP/RAR/7z/PDF/DOCX) no tiene límites de tamaño, de miembros ni sandbox | Un archivo manipulado puede agotar disco/RAM o escribir fuera del temporal |
| 4 | La documentación del repositorio describe un producto de **hace 10 commits** | Onboarding roto: `.agent/` afirma que el costo y el veredicto "estarán disponibles en la siguiente versión" y el README no menciona ninguna de las funciones posteriores a HU-003 |

El resto son problemas de grado medio (endurecimiento de sesión, robustez de la ingesta, precisión estadística, higiene del repositorio) con correcciones acotadas y bien identificadas. **La arquitectura en capas es correcta y el código está bien documentado en español**; el problema no es la estructura, es el acabado.

**Veredicto:** listo como demostración y trabajo de curso; **no listo para producción** con datos de terceros sin cerrar los puntos 1–3.

---

## 2. El producto: alcance funcional real

El README describe solo las HU-001/002/003. El código real cubre mucho más:

| Área | Historias / especificaciones | Estado |
|---|---|---|
| Identificación CUBSO (TF-IDF + Gemini) | HU-001, SP-004 | Completo |
| Rango de precios y competencia | HU-002, HU-003, SP-002 | Completo |
| Costo, veredicto y explicación | HU-005, HU-006, OE-4, OE-5 | Completo (determinista) |
| Panorama del mercado (filtros, mapa, CSV) | DO-002, EN-003 | Completo |
| Convocatorias vigentes y descarga de bases | HU-007, EN-007 | Completo |
| Registro, sesión y perfil de empresa | HU-008 | Completo |
| Requisitos de calificación desde las bases | HU-009, HU-010 | Completo (extracción heurística) |
| Asistente de postulación con LangGraph | HU-010, HU-014 | Completo, **con salvedades graves** |
| Consorcios | HU-012 | Completo |
| Catálogo y solicitud de rubros | HU-013, EN-011 | Completo |
| Documentos privados PDF | HU-015 | Completo y bien probado |
| Etapas y cronograma de las bases | HU-018 | Completo |
| RAG de bases | EN-012 | Completo (sin índice vectorial) |
| Ingesta diaria OCDS | EN-007, EN-011, SP-005/006 | Completo |

**Métricas del repositorio:** 283 archivos versionados; 100 en `app/`; 17.652 líneas (8.092 de Python de producción, 5.287 de Python de pruebas, 2.723 de plantillas, 3.900 de CSS, 631 de JS). 38 rutas HTTP registradas.

---

## 3. Arquitectura

```
┌────────────────────────────────────────────────────────────────────┐
│ PRESENTACIÓN   Flask 3.1 + Jinja2 + CSS/JS progresivo (38 rutas)   │
│   home · evaluar · candidatos · mercado · costo · veredicto        │
│   panorama · datos · convocatorias · rubros · perfil · documentos  │
│   consorcio · asistente · auth (registro/ingreso/salir)            │
├────────────────────────────────────────────────────────────────────┤
│ DOMINIO                                                            │
│   orchestrator.py  ── 2 StateGraph (identificación, mercado)       │
│   agents/          ── classifier · market_analyst · evaluator      │
│                        · writer (solo classifier usa LLM)          │
│   rules.py         ── umbrales, dispersión, confianza, veredicto   │
│   services/assistant.py ── 6 nodos LangGraph (sin LLM)             │
├────────────────────────────────────────────────────────────────────┤
│ SERVICIOS                                                          │
│   data (CSV en memoria) · analytics (pandas) · charts (SVG)        │
│   llm (Gemini + retries) · rag (embeddings) · requirements         │
│   tenders · bases_download · bases_reader · schedule               │
│   accounts · company_documents · consortia · rubros · ingest       │
│   ocds_source · db · formatting · pdf_validation · seed_demo       │
├────────────────────────────────────────────────────────────────────┤
│ PERSISTENCIA                                                       │
│   A) CSV estáticos → pandas en memoria (catálogo y adjudicaciones) │
│   B) PostgreSQL/Neon (convocatorias, documentos, requisitos, RAG,  │
│      cuentas, perfiles, documentos PDF, consorcios) · SQLite local │
├────────────────────────────────────────────────────────────────────┤
│ BATCH / EXTERNO                                                    │
│   GitHub Actions + Programador Windows/macOS → ingest_job.py       │
│   API OCDS OECE → ZIP mensual → normalización → upsert por OCID    │
│   Gemini: 2 llamadas por clasificación + embeddings de las bases   │
└────────────────────────────────────────────────────────────────────┘
```

**Dos universos de datos conviven a propósito:** `awards.csv` (43.464 adjudicaciones de los 2.023 bienes del catálogo, usado para el veredicto) y `awards_detail.csv` (60.024 adjudicaciones, usado para panorama y detalle). Está documentado en `app/services/data.py:21-38`, pero es una fuente de confusión para cualquiera que compare cifras entre pantallas.

**Dos bases de datos:** las cuentas, documentos y consorcios viven en `app/instance/cuentas.sqlite3` en local o en `ACCOUNTS_DATABASE_URL`/`DATABASE_URL` en producción; las convocatorias en `DATABASE_URL`. `seed_demo.py` siembra **otra** base (`app/data/dev_tenders.db`) y escribe `app/.env` por su cuenta (`app/seed_demo.py:174-185`), lo que rompe la expectativa de "una sola base local".

---

## 4. Los datos (verificado con pandas)

| Tabla | Filas | Observaciones |
|---|---|---|
| `catalog.csv` | 2.023 bienes | 2.023 códigos únicos, 4 columnas; la suma de `n_adjudicaciones` es exactamente 43.464 |
| `awards.csv` | 43.464 | 7 columnas; 2023: 16.318 · 2024: 14.730 · 2025: 12.416 |
| `awards_detail.csv` | 60.024 | 21 columnas; añade entidad, proveedor, monto, proceso, fecha de buena pro |
| `cleaning_funnel.csv` / `data_quality.csv` | pequeños | Insumos de la pantalla "Cómo preparamos los datos" |

**Calidad observada:**

- `n_postores` nulo en **782 filas (1,8 %)** de `awards.csv` y 1.132 de `awards_detail.csv`. El analista los excluye con `dropna`, correcto, pero conviene mostrarlo en la UI.
- `departamento_entrega`/`provincia_entrega` nulos en **891 filas**; `fecha_buenapro` nula en 10. Se manejan con `fillna`, pero el panorama dice "sin fecha" sin desglosar.
- **1.187 filas duplicadas exactas** en `awards.csv`. No son un error de datos: son líneas de ítem distintas con el mismo precio (el archivo no trae `n_item`). Consecuencia real: **los percentiles están ponderados por línea, no por proceso**; 43.464 líneas corresponden a 41.259 pares (convocatoria, ítem). El texto de `writer.py:39-40` habla de "% de los procesos" cuando en realidad es "% de las líneas".
- Existe la columna `atipico` pero **el veredicto no la usa**: los precios atípicos entran al cálculo de P25/P50/P75. En cemento hay 26 precios > S/ 100 con mediana S/ 30,93; el P75 apenas cambia (33,5 vs 32,94), pero es una decisión que debería ser explícita y probada.
- Años 2023–2025 mezclados **sin deflactar ni ponderar por recencia**. Un precio de 2023 y uno de 2025 no son comparables en soles constantes; hoy se promedian igual.

---

## 5. Pruebas y calidad de la suite

```
355 pruebas: 350 aprobadas · 1 fallida · 4 omitidas · 79,7 s
```

La única falla es **de portabilidad, no de producto**:

```
app/tests/test_pdf_validation_plataformas.py::test_el_validador_cuenta_paginas_aunque_setrlimit_falle_como_en_macos
ModuleNotFoundError: No module named 'resource'
```

La prueba asume Linux/macOS (`import resource`) y se ejecuta en Windows, donde ese módulo no existe. Debe marcarse con `skipif(sys.platform == "win32")` o simularse. Las 4 omitidas requieren PostgreSQL real (`RAG_TEST_POSTGRES_URL`).

**Distribución:** las pruebas están bien repartidas y son de integración real (clientes Flask, SQLite temporal, PDF sintéticos, API OCDS falsa), no solo unitarias de conveniencia:

| Archivo | Pruebas | Archivo | Pruebas |
|---|---|---|---|
| `test_hu008.py` (cuentas) | 38 | `test_en011.py` (rubros) | 19 |
| `test_hu015.py` (documentos) | 32 | `test_panorama.py` | 19 |
| `test_requisitos.py` | 27 | `test_hu002.py` | 15 |
| `test_hu005_hu006.py` | 26 | `test_hu018…py` | 15 |
| `test_en007.py` (ingesta) | 24 | `test_hu007.py` | 14 |
| `test_en012.py` (RAG) | 24 | `test_hu012.py` | 14 |
| `test_hu012_consorcio.py` | 22 | `test_hu001.py` | 21 |

**Huecos de cobertura relevantes:**

- la rama de `assistant.py:286-295` (citas inventadas) **nunca se ejecuta** en pruebas: `test_assistant.py` siempre siembra chunks;
- no hay pruebas de *zip bomb*, *path traversal*, symlinks en archivos del SEACE;
- no hay pruebas de inyección de prompt ni de JSON con tipos incorrectos (`{"codigos": "12771"}`);
- no hay pruebas de `dispersion=None`, de medianas con n=1, ni de concurrencia multi-máquina en la ingesta;
- PostgreSQL solo se prueba si se provee la URL.

**Entorno:** el `PYTHON_VERSION` de Render es 3.12.9, pero aquí se ejecutó con **Python 3.14.5** y `scikit-learn 1.9.1` frente al `1.8.0` fijado en `requirements.txt`. Hay deriva de entorno no detectada por CI (no existe workflow de pruebas, solo de ingesta y diagnóstico).

---

## 6. Hallazgos priorizados

### P0 — corregir antes de cualquier demo con datos reales

**P0-1. El asistente fabrica citas de las bases.**
Si una convocatoria no tiene *chunks* indexados, `evaluate_tenders_node` inventa el requisito, la página y el monto:
- `app/services/assistant.py:286-295`: asigna `page_ref = 4`, "Registro de Hidrocarburos (OSINERGMIN)" y `req_exp_amount = value * 0.5` **sin leer ninguna base**;
- `app/services/assistant.py:436`: lo imprime como `«…» (**Bases, pág. 4**)`;
- además, si el requisito es desconocido (`has_osinergmin is None`) y no se detecta monto, el semáforo queda **verde** con "Cumple preliminarmente" (`:305-340`).
También el parser de montos toma el primer `S/` del chunk (`:275-281`), por lo que "S/ 150.000" se convierte en 150,0.
**Corrección:** eliminar la rama de requisitos sintéticos; cuando no haya evidencia, devolver "no evaluable" (gris) y enlazar a las bases. Añadir la prueba del caso "sin chunks".

**P0-2. Identidad basada en correo no verificado → acceso horizontal en consorcios.**
`/registro` entra directamente sin verificar el correo (`app/auth.py:144-161`). Las invitaciones se resuelven con `ConsortiumMember.invited_email == email` (`app/services/consortia.py:294-295, 358-364`), y al aceptar se exponen ventas, personal y montos de las demás empresas (`consortia.py:305-329`). Registrarse con el correo de un competidor basta para aceptar su invitación.
**Corrección:** verificación de correo con token de un uso antes de permitir aceptar invitaciones; invitar por `company_id` cuando la cuenta ya exista.

**P0-3. Parseo de archivos descargados sin sandbox ni límites.**
- `app/services/bases_reader.py:92-132`: extrae ZIP/RAR/7z a disco y lee miembros con `archive.read()` **sin límite de tamaño, número de miembros ni ratio de compresión** (zip bomb, path traversal, symlinks según la herramienta externa);
- `app/services/ocds_source.py:229-235`: mismo patrón con el ZIP mensual de la API;
- los PDF de las bases se parsean en el proceso de ingesta sin timeout ni `RLIMIT`, aunque **los PDF subidos por usuarios sí tienen aislamiento** (`company_documents.py:72-103`, `pdf_validation.py`): la protección existe y no se aplicó a la ruta más expuesta.
**Corrección:** reutilizar `pdf_validation` para las bases, validar miembros de archivo (ruta normalizada dentro del temporal, tope de 200 MB descomprimidos y de 1.000 miembros).

**P0-4. La ingesta puede quedar "en curso" para siempre.**
`run_ingestion` solo captura `SourceError` al descargar (`app/services/ingest.py:163-171`). Un `OSError` de disco al escribir (`ocds_source.py:201-203`) o un `MemoryError` termina el proceso sin marcar `fallida`, y la fila de `actualizaciones` queda `en_curso` indefinidamente.
**Corrección:** `try/finally` que cierre el registro siempre, y recuperación de ejecuciones huérfanas al arrancar.

### P1 — antes de abrir el registro al público

| # | Hallazgo | Evidencia |
|---|---|---|
| P1-1 | El límite de intentos de login cuenta también los **accesos correctos**: 8 inicios válidos en 15 min bloquean al usuario; sin `ProxyFix`, todos comparten `remote_addr` detrás de Render | `accounts.py:97-107`; `auth.py:180-183` |
| P1-2 | DoS no autenticado por *hashing*: `create_user` calcula scrypt **antes** de comprobar el duplicado y `/registro` no tiene límite | `accounts.py:72-83` |
| P1-3 | La API OCDS no tiene reintentos ni *backoff*: un 429/5xx/timeout aborta la ingesta | `ocds_source.py:94-100, 197-205` |
| P1-4 | Un estado `no_legible` (sin bases publicadas) **nunca se reintenta** aunque después aparezcan documentos | `rag.py:13, 78-81`; `ingest.py:91-94` |
| P1-5 | Cualquier documento de la convocatoria se marca como "bases" (incluido un "Resumen ejecutivo") y luego se reporta como "no legible" | `ocds_source.py:412-427` |
| P1-6 | `dispersion=None` se convierte en `0` → "dispersión baja" → **confianza alta** con datos insuficientes; `rules.dispersion` divide por `p50` sin guarda | `evaluator.py:58, 67`; `rules.py:31-33` |
| P1-7 | `parse_cost` interpreta "12,503" como 12,503 y "1,234" como 1,234: un usuario que escribe miles sin decimales obtiene un veredicto erróneo | `evaluator.py:25-28` |
| P1-8 | Sin índice vectorial: en PostgreSQL `ORDER BY distance` sobre toda la tabla; en SQLite se cargan todos los chunks en memoria; el asistente además **carga todos los chunks** de cada convocatoria y no usa el RAG | `db.py:174`; `rag.py:293-298`; `assistant.py:189, 212-215` |
| P1-9 | `/convocatorias/solicitar-rubro` escribe en la base **sin sesión** ni límite, con `empresa` libre | `app.py:396-408` |
| P1-10 | `debug=True` en el arranque directo | `app.py:581-582` |
| P1-11 | Cookies `Secure` solo si existe `RENDER`; sin HSTS/CSP/`X-Frame-Options` globales; `onclick` en `assistant.html` impide CSP estricta | `auth.py:83-84, 121-126` |

### P2 — deuda técnica y precisión

- **Estadística:** `n_awards` cuenta líneas, no procesos; medianas por departamento con n=1 se presentan como mercado (`analytics.py:309-313`, `writer.py:43-48`); años sin deflactar; dos definiciones de año (`anio` vs `fecha_buenapro`); `atipico` sin usar.
- **LLM:** el `usage_metadata` se calcula y se descarta (`llm.py:57-78`): sin telemetría de tokens ni presupuesto; sin caché para consultas repetidas; un `JSONDecodeError` no se reintenta ni usa *fallback* (`llm.py:84-85`); el *prompt* interpola texto del usuario sin delimitadores (`classifier.py:74, 85`).
- **Robustez de ingesta:** sin *lock* distribuido entre máquinas (`scripts/windows/ingesta_diaria.ps1:32-41`); `--releer` ilimitado y sin captura de errores (`rag.py:124-149`); tokens de embeddings estimados con `len/4`, sin deduplicación por hash.
- **Web:** `app.py` es un monolito de 582 líneas con reglas de negocio en las vistas (`tenders_list`, `verdict`); 9 bloques `try/except SQLAlchemyError` duplicados; helper `unavailable()` triplicado; `MAX_CONTENT_LENGTH` con código muerto (`documents.py:18-20`); `_describe_page` no pasa `retry`; `/candidatos/directo` acepta GET y **modifica la sesión** (prefetch/crawlers alteran el flujo).
- **Base de datos:** sin migraciones reales (`create_all` + `ALTER TABLE` manual en `db.py:226-247`); tablas de cuentas sin `create_all` en producción fuera de la primera petición; `requisitos` es una tabla muerta (nunca se escribe; los requisitos se leen de `fragmentos`); `schedule_document_id` sin FK.
- **Repositorio:** 340 MB de artefactos de análisis versionados (PDF de bases de hasta 46 MB), `.git` de 323 MB; 5 `.DS_Store` versionados; sin LICENSE, CONTRIBUTING ni CHANGELOG.

---

## 7. Documentación: el desfase más caro de arreglar

| Documento | Estado real |
|---|---|
| `README.md` | Bien escrito, pero **describe solo HU-001/002/003** (los "2 tramos"): no menciona costo/veredicto, cuentas, documentos, consorcios, convocatorias, asistente ni RAG, y su estructura de carpetas ya no coincide con la real. |
| `.agent/PROJECT_DOCUMENTATION.md` | Obsoleto y **contradictorio**: dice que el README "solo tiene 1 línea y está en UTF-16LE" y que no existe `.env.example`. Ambos existen. Su sección 5 describe pasos 4 y 5 como pendientes. |
| `.agent/rules/project-context.md` | Lista un stack y 5 rutas que ya no corresponden; habla de "2 agentes" cuando hay 5 módulos y 3 grafos. |
| `app/.env.example` | Solo documenta 6 variables; el código usa **más de 20** (`DATABASE_URL`, `ACCOUNTS_DATABASE_URL`, `GEMINI_FALLBACK_MODEL`, `OCDS_USER_AGENT`, `OCDS_MONTHS`, `PILOT_RUBROS`, `RUBROS_INGESTA`, `EMBEDDING_MODEL`, `RAG_MAX_BASES_PER_RUN`, `RAG_MAX_BASES_MB`, `CRONOGRAMA_MAX_POR_EJECUCION`…). |
| `docs/` | `HU015.md` y `analisis_buena_pro_ocds.md` son buenos; el resto de la documentación de HU vive dispersa en mensajes de commit y docstrings. |
| `pyrightconfig.json` / `.vscode/settings.json` | Rutas absolutas de macOS del autor original; inutilizables en Windows. |

**El código, en cambio, está muy bien documentado:** docstrings en español con contratos de entrada/salida, referencias a especificaciones y decisiones justificadas (por qué el costo no se guarda, por qué `float_precision="round_trip"`, por qué se usa un `User-Agent` de navegador). Es el mejor activo del repositorio después de las pruebas.

---

## 8. Sobre "multiagente"

Es la afirmación de marketing más débil del proyecto:

- Hay **5 módulos en `app/agents/`**, pero **solo `classifier` invoca un LLM**. `market_analyst`, `evaluator` y `writer` son funciones deterministas (decisión correcta y bien argumentada).
- Hay **3 grafos LangGraph** (`orchestrator.py:51-68, 135-160`; `assistant.py:460-484`), con 14 nodos, todos DAG sin ciclos, sin memoria, sin herramientas, sin *routing* por LLM. Las aristas condicionales devuelven **constantes de texto** que la app vuelve a inspeccionar (`app.py:172-181`).
- El grafo del asistente tiene una sola bifurcación y **el historial de conversación nunca se consulta** en la lógica (`assistant.py:45-53, 490-507`).
- **LangSmith** está decorando 7 funciones y pasa `run_name`, pero no hay evaluación, *feedback* ni *dataset* de trazas. Sin `LANGSMITH_API_KEY` es un no-op.

**Conclusión:** LangGraph aporta legibilidad y testabilidad (no es gratis: es una dependencia pesada), pero el sistema es un **pipeline con un LLM en un paso**, no un sistema multiagente. Para la sustentación conviene decirlo así; para la rúbrica, conviene demostrar el grafo y sus trazas.

---

## 9. Plan de acción sugerido

**Semana 1 — cerrar lo que puede hacer daño**
1. Eliminar las citas y requisitos sintéticos del asistente; introducir el estado "no evaluable". Añadir pruebas del caso sin *chunks*. (P0-1)
2. Verificación de correo + invitaciones por `company_id`. (P0-2)
3. Límites y sandbox en `bases_reader` y en el ZIP mensual, reutilizando `pdf_validation`. (P0-3)
4. `try/finally` en la ingesta y recuperación de ejecuciones huérfanas. (P0-4)
5. Corregir el límite de login (contar solo fallos, `ProxyFix`) y añadir límite a `/registro`. (P1-1, P1-2)

**Semana 2 — precisión y confianza**
6. `dispersion=None` y `p50=0` con guardas explícitas; corregir `parse_cost` para miles sin decimales; decidir y probar el tratamiento de `atipico`. (P1-6, P1-7)
7. Unificar la semántica línea/proceso en textos y métricas (o renombrar la etiqueta).
8. Reintentos HTTP con *backoff* en OCDS; reintento de `no_legible`; `is_bases` más estricto. (P1-3, P1-4, P1-5)
9. Índice vectorial (`ivfflat`/`hnsw`) y búsqueda real en el asistente; presupuesto de *tokens* y caché. (P1-8)
10. Exigir sesión en `/convocatorias/solicitar-rubro`; `debug` por variable de entorno; cabeceras de seguridad. (P1-9, P1-10, P1-11)

**Semana 3 — higiene y documentación**
11. Reescribir `README` y `.agent/*` con el alcance real; completar `.env.example`; arreglar `pyrightconfig.json`.
12. Sacar `analisis/salidas/sp005/bases/` del control de versiones (Git LFS o regenerable), borrar `.DS_Store`.
13. Añadir workflow de CI que ejecute `pytest` en 3.12 (hoy no existe) y marcar la prueba de `resource` como específica de plataforma.
14. Dividir `app.py` en *blueprints* por área; unificar los helpers de error.

---

## 10. Lo que está bien hecho (y conviene preservar)

- **Determinismo donde importa:** los cálculos de precios y el veredicto no pasan por el LLM, y hay pruebas que fallan si se invoca al modelo (`no_model`). Es la decisión de arquitectura más valiosa del proyecto.
- **Privacidad del costo:** no se guarda, no viaja al modelo, no se traza (`evaluator.py:1-6`, `app.py:209-213`).
- **Seguridad de los PDF privados:** límites por archivo y por empresa, validación de cabecera/cierre/estructura, rechazo de cifrado/JavaScript/adjuntos, proceso aislado con timeout, `Content-Security-Policy: sandbox`. Es la parte mejor resuelta del backend.
- **Control de acceso de recursos:** las consultas de documentos y consorcios resuelven la empresa desde la sesión y responden 404 a lo ajeno (`company_documents._owned_query`, `consortia._role`).
- **Verificación empírica:** `analisis/` contiene 24 scripts reproducibles, 8 notebooks de salida, un control cruzado contra fórmulas de Excel con 0 discrepancias y un informe de ingesta. Pocos proyectos de curso tienen esta trazabilidad.
- **Docstrings y justificaciones:** cada decisión no obvia está explicada en el código, en español, con referencia a la especificación. Eso hace que el análisis y las correcciones sean rápidos.
- **Manejo de degradación:** sin `DATABASE_URL` la app funciona con avisos (RN-002); sin `GEMINI_API_KEY` hay búsqueda directa por TF-IDF; sin bases legibles se explica por qué. El sistema rara vez se rompe: se degrada.

---

## Anexo — métricas del análisis

| Métrica | Valor |
|---|---|
| Archivos versionados | 283 |
| Rutas HTTP | 38 |
| Python de producción | 8.092 líneas |
| Python de pruebas | 5.287 líneas |
| Plantillas / CSS / JS | 2.723 / 3.900 / 631 líneas |
| Pruebas | 355 (350 ok, 1 fallo de plataforma, 4 omitidas) |
| Duración de la suite | 79,7 s |
| Cobertura de datos | 2.023 bienes · 43.464 adjudicaciones · 60.024 en detalle |
| Artefactos versionados en `analisis/` | 340 MB (mayor: PDF de 46,6 MB) |
| Tamaño de `.git` | 323 MB |
| Modelo LLM | `gemini-3.1-flash-lite`, temperatura 0, JSON |
| Embeddings | `gemini-embedding-001`, 768 dims |
| Base de datos | PostgreSQL (Neon) + pgvector en producción; SQLite en local/pruebas |
