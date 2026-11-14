# Corrección del checkout de GitHub Actions — 14/11/2026

El primer push de la integración 9d08a2e se publicó correctamente, pero la ejecución EN-009 falló en actions/checkout antes de preparar Python o ejecutar pytest. El registro terminó con: `No url found for submodule path '-Aplicaci-n-web-multiagente-Licitaciones-P-blicas' in .gitmodules`.

Ese nombre estaba registrado como gitlink (modo 160000) desde b00dc6d y apuntaba a 0261730, un commit anterior del mismo proyecto. No había un .gitmodules que lo configurase. El fallo se reprodujo con `git submodule foreach --recursive true` (salida 128).

Se retira únicamente esa referencia del índice y se ignora la carpeta duplicada para evitar que vuelva a registrarse por accidente. Se comprobó que un archivo local dentro de esa carpeta se conserva al retirar la referencia. Los archivos del programa y sus pruebas no cambian.

La operación de limpieza de credenciales que usa checkout pasó después de la corrección. La validación inicial no cubrió esa operación y por eso las pruebas locales no detectaron el fallo del checkout.

También se fija el runner en Ubuntu 24.04, compatible con el Python 3.12.9 solicitado por este workflow. El manifiesto oficial de actions/python-versions incluye esa combinación; no ofrece 3.12.9 para Ubuntu 26.04, al que migró ubuntu-latest. Este ajuste afecta al entorno de GitHub Actions y no modifica Render.
