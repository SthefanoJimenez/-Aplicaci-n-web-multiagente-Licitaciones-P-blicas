# Contexto y Reglas del Proyecto: LicitaFácil (Sistema Multiagente de Licitaciones Públicas)

## 1. Visión General del Proyecto
- **Nombre de la aplicación**: `LicitaFácil`
- **Propósito**: Aplicación web multiagente que ayuda a empresarios peruanos (especialmente MYPES y proveedores del Estado, con foco regional en **La Libertad**) a evaluar si es rentable vender un bien al Estado peruano, basándose en datos históricos reales del **OECE** (2023–2025).
- **Alcance actual**: Cubre los **Pasos 1, 2 y 3** (de un flujo planeado de 5 pasos) correspondientes a las Historias de Usuario **HU-001** (Clasificación CUBSO), **HU-002** (Rango de precios unitarios) y **HU-003** (Análisis de competencia). El ingreso del costo del usuario y la emisión del veredicto final (Pasos 4 y 5) tienen su lógica base en `app/rules.py`, pero en la interfaz (`market.html`) aparecen marcados como *"disponible en la siguiente versión"*.

## 2. Stack Tecnológico y Versiones
- **Backend / Web**: Python + `Flask 3.1.3` + `Gunicorn 26.2.0` (configurado en `app/Procfile`).
- **Orquestación Multiagente y Observabilidad**: `LangGraph 1.2.14` (`StateGraph`) + `LangSmith 0.14.4` (`@traceable`).
- **LLM / IA**: `google-genai 2.28.0` (modelo por defecto: `gemini-3.1-flash-lite`, temperatura `0`, salida `application/json`, sin function calling automático).
- **Recuperación de Información y Datos**: `scikit-learn 1.8.0` (`TfidfVectorizer` con n-gramas de caracteres `char_wb` de 3 a 5 y similitud coseno), `pandas 3.0.6`, `numpy`.
- **Testing y Auditoría**: `pytest 9.1.1`, `openpyxl 3.1.5` (generación de libros de control cruzado contra fórmulas de Excel).

## 3. Arquitectura y Estructura de Archivos (`app/`)
- `app/app.py`: Punto de entrada Flask. Define filtros Jinja2 (`pen`, `number`, `percent`, `unit_text`) y las rutas HTTP (`GET /`, `POST /candidatos`, `GET /candidatos`, `POST /confirmar`, `GET /mercado`).
- `app/config.py`: Variables de entorno (`GEMINI_API_KEY`, `SECRET_KEY`, `GEMINI_MODEL`) y constantes (`MAX_DESCRIPTION_LENGTH = 300`, `RETRIES = 2`, `RETRY_WAIT_SECONDS = 2`).
- `app/orchestrator.py`: Orquestador basado en **LangGraph** dividido en dos grafos independientes:
  1. `IDENTIFICATION_STAGE` (`identify_good`): Ejecuta `classifier_agent` -> arista condicional `has_candidates` -> `show_candidates` o `no_evidence`.
  2. `MARKET_STAGE` (`analyze_market`): Ejecuta `market_analyst_agent` -> arista condicional `evidence_level` -> `show_range`, `reference_range` o `no_evidence`.
- `app/agents/classifier.py`: **Agente Clasificador (HU-001)**. Combina LLM + búsqueda léxica TF-IDF + validación determinista:
  1. Reformula la descripción coloquial a lenguaje técnico CUBSO vía Gemini (`prompts/classifier_rephrase.txt`). Si falla esta llamada, continúa con la descripción original.
  2. Preselecciona hasta 30 candidatos por texto (`description` y `rephrased_description`) usando TF-IDF de n-gramas de caracteres (`char_wb`, 3–5) sobre `itemcubso + unidad_medida`.
  3. Envía la lista preseleccionada a Gemini (`prompts/classifier_choose.txt`) para elegir hasta 3 códigos ordenados por probabilidad.
  4. Valida estrictamente (`validate()`) que los códigos existan en `data.VALID_CODES`, descartando alucinaciones o duplicados.
- `app/agents/market_analyst.py`: **Agente Analista de Mercado (HU-002 y HU-003)**. 100% determinista (nunca invoca al LLM):
  - Filtra adjudicaciones por `codigoitem` y `unidad_medida` del catálogo.
  - Calcula percentiles `P25`, `mediana (P50)` y `P75` mediante interpolación lineal (`quantile(..., interpolation="linear")`).
  - Calcula dispersión relativa $IQR_{rel} = \frac{P75 - P25}{P50}$.
  - Calcula indicadores de competencia: `n_with_bidders`, `median_bidders` (excluyendo nulos), `n_single_bidder`, `pct_single_bidder` y `n_la_libertad` (adjudicaciones en el departamento `"LA LIBERTAD"`).
- `app/rules.py`: Reglas de negocio (SP-002):
  - Umbrales de muestra: `MIN_CATALOG = 5`, `MIN_VERDICT = 10`, `MIN_HIGH_CONFIDENCE = 30`.
  - Umbrales de dispersión: `LOW_DISPERSION_MAX = 0.25`, `MEDIUM_DISPERSION_MAX = 0.50`.
  - Niveles de evidencia: `no_evidence` ($n < 5$), `insufficient_evidence` ($5 \le n < 10$), `with_verdict` ($n \ge 10$).
  - Regla de veredicto por costo: `cost < P25` -> `participate` ("Participa"); `P25 <= cost <= P75` -> `participate_with_caution` ("Participa con cautela"); `cost > P75` -> `not_profitable` ("No rentable").
- `app/services/data.py`: Carga en memoria al inicio `catalog.csv` (2,023 bienes con $\ge 5$ adjudicaciones) y `awards.csv` (43,464 filas de adjudicaciones con `float_precision="round_trip"`).
- `app/services/llm.py`: Cliente singleton de `google.genai`, reintentos ante errores HTTP `(429, 500, 503)` y limpieza de bloques Markdown JSON.

## 4. Convenciones de Código y Reglas de Desarrollo
1. **Idioma mixto intencional**:
   - **Código fuente** (nombres de funciones, variables, clases, claves de diccionarios): en **inglés** (`identify_good`, `analyze_market`, `filter_awards`, `compute_price_range`).
   - **Columnas de datos CSV**: en **español original del OECE** (`codigoitem`, `itemcubso`, `unidad_medida`, `anio`, `codigoconvocatoria`, `precio_unitario`, `n_postores`, `entidad_departamento`) para mantener trazabilidad.
   - **Docstrings, comentarios, prompts, rutas Flask y UI**: en **español** (`/candidatos`, `/confirmar`, `/mercado`).
2. **Determinismo en cálculos numéricos**:
   - **PROHIBIDO** usar el LLM en HU-002 y HU-003 (las pruebas unitarias en `test_hu002.py` y `test_hu003.py` usan el fixture `no_model` que lanza `AssertionError` si se invoca a Gemini).
   - Mantener `float_precision="round_trip"` al leer `awards.csv` y método de interpolación `"linear"` para los percentiles.
3. **Ejecución de pruebas**:
   - Ejecutar `pytest` desde la raíz o carpeta `app/` (no consume API de Gemini gracias a mocks).
