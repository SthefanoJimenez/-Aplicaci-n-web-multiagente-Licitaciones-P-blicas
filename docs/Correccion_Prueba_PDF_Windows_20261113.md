# Corrección de la prueba de PDF en Windows — 13/11/2026

## Incidencia observada

La salida y captura de PowerShell de Arturo confirman que la integración avanzó de
39a70ed a 0949322 y creó backup/arturo-integracion-20261113-214400. La suite terminó
con **507 aprobadas, 4 omitidas y 1 fallida en 148.87 segundos**. El aplicador detuvo
la subida a la rama personal, por lo que la integración quedó local.

La prueba test_el_validador_cuenta_paginas_aunque_setrlimit_falle_como_en_macos
importaba el módulo real resource dentro de un subproceso. Windows no dispone de
ese módulo y la prueba terminaba con ModuleNotFoundError antes de ejecutar el
validador. El validador de la aplicación ya captura la ausencia del módulo.

## Corrección mínima

- La prueba de límites rechazados instala un módulo resource simulado en el
  subproceso. Así comprueba el mismo comportamiento en cualquier sistema anfitrión.
- Se añade una prueba de extremo a extremo del proceso aislado sin resource,
  reproduciendo la condición de Windows y comprobando tres páginas.
- Se mantienen las pruebas de rechazo de JavaScript y de límites de recursos.

Solo cambia app/tests/test_pdf_validation_plataformas.py, además de este documento.
La aplicación, el validador, los módulos del equipo y la configuración conservan
su contenido de la integración 0949322.

## Validación

- Pruebas específicas de PDF: **6 aprobadas**.
- Suite completa: **509 aprobadas, 4 omitidas, 71.95 segundos**, ejecutada en Linux
  con bases temporales y sin claves reales mediante Probar-Integracion.py.
- Las condiciones de módulo ausente y límites rechazados se reproducen en
  subprocesos; esto no sustituye la ejecución de aceptación en la PC de Windows.
- Pendiente: aplicar este paquete y repetir la suite en Windows. El aplicador
  subirá solo la rama personal si la suite termina correctamente.

## Otros bloqueos de la jornada

Se documentan en el apartado de Arturo de Semana 11: acceso a cuentas recuperado
tras configurar el entorno local; revisión del RUC aceptado por longitud y
rechazado por SUNAT; ajuste del botón de evaluación y persistencia; revisión de
columnas e iconos; resolución de tres conflictos de integración; archivo del
aplicador no encontrado en el primer intento; y esta incompatibilidad de la
prueba en Windows. No se atribuyen a terceros ni se inventan tiempos de resolución.
