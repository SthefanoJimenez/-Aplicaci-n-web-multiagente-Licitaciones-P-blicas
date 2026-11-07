   app/logs/ingesta_AAAA-MM-DD.log
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$REPO/app"
LOGS="$APP/logs"
mkdir -p "$LOGS"
LOG="$LOGS/ingesta_$(date +%Y-%m-%d).log"
LOCK="$LOGS/.ingesta.lock"

# Python: el que se registró al instalar la tarea; si no, el del entorno virtual o el del sistema
PYTHON="${LICITAFACIL_PYTHON:-}"
for candidate in "$PYTHON" "$REPO/.venv/bin/python" "$APP/.venv/bin/python" "$(command -v python3)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done

# Una sola ingesta a la vez (por si alguien la corre a mano mientras corre la programada)
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +180 2>/dev/null)" ]; then
        rm -rf "$LOCK" && mkdir "$LOCK"          # quedó de una ejecución que se cortó hace más de 3 horas
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') Ya hay una ingesta en curso; esta se omite." >> "$LOG"
        exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

cd "$APP" || exit 1
export INGEST_TRIGGER="${INGEST_TRIGGER:-programada_imac}"
export PYTHONUNBUFFERED=1
{
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') · inicio · $PYTHON"
    # caffeinate -i: la iMac no entra en reposo mientras dura la ingesta y la lectura de bases
    caffeinate -i "$PYTHON" ingest_job.py
    code=$?
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') · fin · código $code"
} >> "$LOG" 2>&1
find "$LOGS" -name 'ingesta_*.log' -mtime +30 -delete 2>/dev/null    # se guardan 30 días de registros
exit $code