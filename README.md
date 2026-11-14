# LicitaFácil — Aplicación Web Multiagente de Licitaciones Públicas

**LicitaFácil** es una aplicación web multiagente diseñada para que empresarios y MYPES del Perú (con foco especial en la región **La Libertad**) evalúen si es rentable vender sus bienes al Estado peruano, basándose en datos históricos reales de adjudicaciones registradas por el **OECE** entre **2023 y 2025**.

---

## Arquitectura Multiagente

El sistema está construido sobre **Flask**, orquestado con **LangGraph** (`StateGraph`) e instrumentado con **LangSmith** (`@traceable`):

1. **Tramo 1 — Identificación del Bien (`HU-001`)**:
   - **Agente Clasificador (`app/agents/classifier.py`)**:
     1. Reformula la descripción coloquial del usuario a terminología técnica del catálogo estatal **CUBSO** mediante **Google Gemini** (`gemini-3.1-flash-lite`).
     2. Preselecciona hasta 30 bienes candidatos por consulta mediante vectorización **TF-IDF de n-gramas de caracteres** (`char_wb`, 3–5) y similitud coseno sobre los **2,023 bienes** de `catalog.csv`.
     3. Solicita a Gemini elegir hasta 3 códigos ordenados por probabilidad.
     4. Aplica validación determinista (`validate()`) contra el catálogo oficial para descartar cualquier código inexistente.
2. **Tramo 2 — Análisis de Mercado y Competencia (`HU-002` y `HU-003`)**:
   - **Agente Analista de Mercado (`app/agents/market_analyst.py`)**:
     - Agente **100% determinista** (sin llamadas al LLM) que procesa **43,464 adjudicaciones** (`awards.csv`).
     - **Rango de precios (`HU-002`)**: Calcula $P_{25}$, mediana ($P_{50}$), $P_{75}$ mediante interpolación lineal y la dispersión intercuartílica relativa $\frac{P_{75} - P_{25}}{P_{50}}$.
     - **Competencia (`HU-003`)**: Calcula la mediana de postores por proceso, el porcentaje de procesos con postor único y el número de adjudicaciones convocadas por entidades de **La Libertad**.
     - **Reglas de evidencia (`app/rules.py`)**: Evalúa el nivel de evidencia según el número de adjudicaciones ($n < 5$ sin evidencia, $5 \le n < 10$ rango referencial, $n \ge 10$ apto para veredicto).
3. **Asistente de postulación (`HU-014`)**:
   - **Widget flotante** en la esquina inferior derecha de todas las pantallas, visible con sesión iniciada (`app/templates/_assistant_widget.html`, `app/static/js/assistant-widget.js`). La conversación se guarda en la sesión.
   - **Grafo LangGraph** (`app/services/assistant.py`): perfil guardado → rubros del catálogo → búsqueda de convocatorias vigentes → evaluación con documentos, consorcio, requisitos de las bases y cronograma → respuesta.
   - **Semáforo honesto**: verde solo cuando lo verificado se cumple; «por verificar» cuando las bases no se pudieron leer. Gemini redacta, pero el sistema valida que solo cite convocatorias reales y cierra con la **Ley N.° 32069** y el **OECE**. Detalle en [`docs/HU014.md`](docs/HU014.md).

---

## Estructura del Proyecto

```text
├── render.yaml                  # Blueprint para despliegue en 1 clic en Render
├── pytest.ini                   # Configuración de pytest
├── .agent/                      # Reglas de contexto y documentación técnica profunda
└── app/
    ├── app.py                   # Rutas Flask y filtros de plantillas Jinja2
    ├── config.py                # Configuración y variables de entorno
    ├── orchestrator.py          # Grafos de estado (LangGraph) para Tramo 1 y Tramo 2
    ├── rules.py                 # Reglas de negocio (umbrales, dispersión, confianza, veredicto)
    ├── Procfile                 # Comando Gunicorn optimizado con --preload
    ├── requirements.txt         # Dependencias de producción
    ├── requirements-dev.txt     # Dependencias de desarrollo y testing
    ├── .env.example             # Plantilla de variables de entorno
    ├── agents/
    │   ├── classifier.py        # Agente Clasificador (Gemini + TF-IDF + Guardrail)
    │   └── market_analyst.py    # Agente Analista de Mercado (Pandas determinista)
    ├── services/
    │   ├── data.py              # Carga en memoria de catalog.csv y awards.csv
    │   └── llm.py               # Cliente de Google GenAI con reintentos e instrumentación
    ├── prompts/                 # Prompts de sistema para reformulación y elección CUBSO
    ├── data/                    # Datasets de producción (catalog.csv y awards.csv)
    ├── templates/               # Vistas HTML (Jinja2)
    ├── static/                  # Hojas de estilo CSS y JavaScript progresivo
    └── tests/                   # Suites unitarias (HU-001, HU-002, HU-003) y control Excel
```

---

## Ejecución en Desarrollo Local

1. **Crear y activar un entorno virtual (Python 3.11+ recomendado):**
   ```bash
   python -m venv .venv
   # En Windows (PowerShell):
   .\.venv\Scripts\Activate.ps1
   # En Linux / macOS:
   source .venv/bin/activate
   ```

2. **Instalar dependencias:**
   ```bash
   pip install -r app/requirements-dev.txt
   ```

3. **Configurar variables de entorno:**
   Copia el archivo `app/.env.example` a `app/.env` y coloca tu clave de Gemini (y opcionalmente LangSmith):
   ```bash
   cp app/.env.example app/.env
   ```

4. **Iniciar el servidor de desarrollo:**
   ```bash
   cd app
   python app.py
   ```
   La aplicación estará disponible en `http://127.0.0.1:5000`.

5. **Ejecutar las pruebas automatizadas (no consumen cuota de la API de Gemini):**
   ```bash
   pytest
   ```

---

## Guía de Despliegue en Render (Recomendado)

El repositorio incluye un archivo **`render.yaml`** preconfigurado para el plan **Free (512 MB RAM)** o **Starter** de Render, optimizado con `--preload` en Gunicorn para que los trabajadores compartan en memoria los DataFrames de Pandas y la matriz TF-IDF sin agotar la memoria RAM.

### Opción A: Despliegue Automático con Blueprint (`render.yaml`)
1. Sube tus cambios a tu repositorio en **GitHub**.
2. Entra a [dashboard.render.com](https://dashboard.render.com/) y haz clic en **New +** $\rightarrow$ **Blueprint**.
3. Conecta tu repositorio de GitHub. Render detectará automáticamente el archivo `render.yaml`.
4. Render te solicitará ingresar las variables secretas marcadas con `sync: false`:
   - `GEMINI_API_KEY`: Tu API Key de Google AI Studio.
   - `LANGSMITH_API_KEY`: Tu API Key de LangSmith (puedes dejarla vacía si aún no deseas enviar trazas, o cambiar `LANGSMITH_TRACING` a `false`).
5. Haz clic en **Apply** y espera ~2 minutos a que termine el build.

### Opción B: Configuración Manual como Web Service en Render
Si prefieres crearlo desde **New +** $\rightarrow$ **Web Service**:
- **Root Directory**: `app` *(¡Importante!)*
- **Runtime**: `Python 3`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60 --preload`
- **Environment Variables**:
  - `PYTHON_VERSION` = `3.12.9`
  - `GEMINI_API_KEY` = `<tu-clave-de-gemini>`
  - `GEMINI_MODEL` = `gemini-3.1-flash-lite`
  - `SECRET_KEY` = `<cadena-aleatoria-segura>`
  - `LANGSMITH_TRACING` = `true`
  - `LANGSMITH_API_KEY` = `<tu-clave-de-langsmith>`
  - `LANGSMITH_PROJECT` = `licitafacil-prod`
