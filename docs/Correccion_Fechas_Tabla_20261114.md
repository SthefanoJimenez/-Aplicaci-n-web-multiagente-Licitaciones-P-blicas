# Fechas de documentos y alineación de convocatorias

Revisión del 14 de noviembre de 2026, hora de Lima. Base: `ea8273dacebbf48fb4f410a09bd8ca22d9c4142e`.

## Comportamiento corregido

- Los campos de inicio y vencimiento no declaraban un máximo. Chromium podía considerar válida una fecha con año de seis cifras en su validación nativa. El servidor ya la rechazaba: la captura no demuestra que se hubiera guardado. Ambos formularios ahora declaran el intervalo 0001–9999 y explican que el año usa cuatro cifras. La comprobación de navegador confirma que el envío inválido se bloquea antes del POST; el servidor conserva su validación y muestra un mensaje más preciso.
- El símbolo y el importe podían saltar de línea por el ajuste de la tabla. Se mantienen juntos en una línea, alineados a la derecha, con cifras tabulares. La tabla distribuye el ancho según el contenido y conserva el desplazamiento horizontal interno para pantallas pequeñas. Se comprobaron también importes de distinta longitud y un importe sintético de 14 cifras enteras.
- Antigüedad se centra tanto en el encabezado como en sus celdas: Hoy, 1 día y varios días.

No cambia el orden regional de día y mes: el control nativo mantiene el formato del navegador. No se modifican los cálculos de elegibilidad, las dependencias, `render.yaml` ni el workflow de CI.

## Validación

- Suite completa: **569 aprobadas, 4 omitidas**, sin fallos; XML en el paquete de entrega. Dos omisiones requieren PostgreSQL de prueba y dos corresponden a PowerShell en este entorno Linux.
- Cuatro regresiones adicionales comprueban años de seis cifras en alta y edición: no se guardan documentos nuevos ni se alteran las fechas o los bytes del PDF anterior.
- **22 comprobaciones de navegador** con datos ficticios: contextos en-US y es-PE; ocho anchos entre 320 y 1920 px; importes sin cortes, alineación derecha, antigüedad centrada y nueve columnas conservadas; carga y edición con el mismo máximo; bloqueo del POST con año de seis cifras y guardado válido con recuperación del verde.
- A 768 px se observó un desborde previo de navegación (824 px), presente también al restaurar los estilos originales de tabla; el cambio no lo aumenta. Las comprobaciones de esta entrega se refieren a las columnas y los formularios corregidos, no a una auditoría completa de accesibilidad.

Las pruebas usan SQLite temporal, sin Neon, cuentas reales ni claves de modelos. Las operaciones de Render permanecen fuera de esta corrección.

## Evidencia recibida de Arturo

Las capturas de Windows de la versión ea8273d confirman el ciclo Personal aún no vigente → guardado → amarillo en 9100101, seguido de inicio 01/01/2026 y vencimiento 31/12/2027 → guardado → verde. Es una demostración local con datos ficticios. La observación de DD/MM/AAAA en el navegador usado para Render se registra aparte del formato MM/DD/AAAA del navegador integrado de VS Code; no se considera un fallo del orden de fechas de la aplicación.
