
set -eu

HOUR="${1:-08}"
MINUTE="${2:-30}"
LABEL="pe.licitafacil.ingesta"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO/scripts/imac/ingesta_diaria.sh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(python -c 'import sys; print(sys.executable)' 2>/dev/null || python3 -c 'import sys; print(sys.executable)')"

if [ ! -f "$REPO/app/.env" ]; then
    echo "Falta app/.env con DATABASE_URL (y GEMINI_API_KEY para la lectura de bases). No se instaló nada."
    exit 1
fi
"$PYTHON" -c "import flask, sqlalchemy, requests" 2>/dev/null || {
    echo "El Python $PYTHON no tiene las dependencias de la app (pip install -r app/requirements.txt)."
    exit 1
}
case "$REPO" in
    "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
        echo "Aviso: el repositorio está en Documentos, Escritorio o Descargas. macOS puede bloquear el acceso"
        echo "de las tareas programadas a esas carpetas. Si el registro dice 'Operation not permitted', dé"
        echo "'Acceso total al disco' a /bin/bash (Configuración del Sistema → Privacidad y seguridad) o mueva"
        echo "el repositorio a otra carpeta, por ejemplo ~/Proyectos." ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/app/logs"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$SCRIPT</string></array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>$((10#$HOUR))</integer><key>Minute</key><integer>$((10#$MINUTE))</integer></dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$PATH</string>
        <key>LICITAFACIL_PYTHON</key><string>$PYTHON</string>
        <key>INGEST_TRIGGER</key><string>programada_imac</string>
    </dict>
    <key>StandardOutPath</key><string>$REPO/app/logs/launchd.log</string>
    <key>StandardErrorPath</key><string>$REPO/app/logs/launchd.log</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST" >/dev/null || { echo "El archivo de la tarea quedó mal formado: $PLIST"; exit 1; }
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

printf 'Tarea "%s" instalada: todos los días a las %02d:%02d.\n' "$LABEL" "$((10#$HOUR))" "$((10#$MINUTE))"
echo "Python: $PYTHON"
echo "Registro: $REPO/app/logs/ingesta_<fecha>.log"
echo
echo "Probarla ahora:        launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Ver si está cargada:   launchctl print gui/$(id -u)/$LABEL | grep -E 'state|last exit'"
echo "Ver las ejecuciones:   cd app && python ingest_job.py --ejecuciones"
echo
echo "Si la iMac está en reposo a esa hora, la tarea corre al despertar; si está apagada, ese día no corre."
WAKE=$(( (10#$HOUR * 60 + 10#$MINUTE + 1435) % 1440 ))       # 5 minutos antes de la tarea
printf 'Para que despierte sola 5 minutos antes:  sudo pmset repeat wakeorpoweron MTWRFSU %02d:%02d:00\n' \
    "$((WAKE / 60))" "$((WAKE % 60))"