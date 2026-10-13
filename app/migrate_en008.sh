#!/usr/bin/env bash
# EN-008: renombra los archivos de la app con "git mv", para que Git conserve el
# historial de cada uno. Ejecutar UNA sola vez, desde la carpeta que contiene
# app.py y orquestador.py, en una rama de trabajo limpia (sin cambios pendientes).
# Después, copie encima los archivos nuevos del paquete EN-008.
set -euo pipefail

if [ ! -f orquestador.py ]; then
  echo "No encuentro orquestador.py: ejecute el script desde la carpeta de la app (o ya se aplicó)." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "Hay cambios sin confirmar. Haga commit o stash antes de migrar." >&2
  exit 1
fi

mv_if_exists() { if [ -e "$1" ]; then git mv "$1" "$2"; echo "  $1 -> $2"; fi; }

echo "Capa de servicios"
mkdir -p services
mv_if_exists datos.py services/data.py
mv_if_exists llm.py services/llm.py

echo "Módulos"
mv_if_exists orquestador.py orchestrator.py
mv_if_exists reglas.py rules.py
mv_if_exists agentes agents
mv_if_exists agents/clasificador.py agents/classifier.py
mv_if_exists agents/analista.py agents/market_analyst.py

echo "Prompts y datos"
mv_if_exists prompts/clasificador_reformular.txt prompts/classifier_rephrase.txt
mv_if_exists prompts/clasificador_elegir.txt prompts/classifier_choose.txt
mv_if_exists datos data
mv_if_exists data/catalogo.csv data/catalog.csv
mv_if_exists data/adjudicaciones.csv data/awards.csv
mv_if_exists docs/grafo_tramo1.png docs/graph_stage1.png
mv_if_exists docs/grafo_tramo2.png docs/graph_stage2.png

echo "Plantillas y estáticos"
mv_if_exists templates/_paso.html templates/_step.html
mv_if_exists templates/describir.html templates/describe.html
mv_if_exists templates/candidatos.html templates/candidates.html
mv_if_exists templates/mercado.html templates/market.html
mv_if_exists templates/resultado.html templates/result.html
mv_if_exists static/css/estilos.css static/css/styles.css
mv_if_exists static/img/licitacion-hero.jpg static/img/tender-hero.jpg

echo "Pruebas"
mv_if_exists tests/consultas_hu001.csv tests/queries_hu001.csv
mv_if_exists tests/ejecutar_consultas_hu001.py tests/run_queries_hu001.py
mv_if_exists tests/resultados_hu001.csv tests/results_hu001.csv

echo
echo "Listo. Ahora copie encima los archivos del paquete EN-008 y ejecute:"
echo "  pytest && python tests/control_hu002.py"
