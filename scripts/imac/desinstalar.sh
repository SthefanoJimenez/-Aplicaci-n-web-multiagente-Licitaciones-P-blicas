#!/bin/bash
LABEL="pe.licitafacil.ingesta"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Tarea $LABEL detenida." || echo "La tarea no estaba cargada."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" && echo "Archivo de la tarea eliminado."