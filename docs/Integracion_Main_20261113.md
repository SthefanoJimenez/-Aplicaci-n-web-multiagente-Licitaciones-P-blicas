# Integración de main con la entrega de Arturo — 13/11/2026

Base prioritaria: main 845d1f7. Segunda línea: feature/arturo-hu010-elegibilidad-20261111, 39a70ed.

Se combinan las dos líneas en una rama local de integración. No se modifica main ni se escribe en Neon.

## Resolución

- app/app.py conserva el registro del consorcio, schedule y timeline de main; agrega los servicios, filtros y evaluación privada ya entregados en la rama de Arturo.
- app/services/db.py conserva todas las columnas de cronograma y publicación de documentos; incorpora items para la estimación de HU-010. La migración sigue siendo aditiva.
- app/templates/tender.html conserva íntegra la sección de etapa y cronograma y el enlace al consorcio; agrega el panel de elegibilidad anterior.
- Los cambios combinados automáticamente en ingesta, normalización, perfil y navegación se revisaron. El enlace a Consorcios permanece.

Los módulos de consorcios, cronograma, lectura de bases, validación de PDF y sus pruebas nuevas conservan el contenido de main. No se corrigen problemas nuevos que no estuvieran resueltos en la entrega anterior de Arturo. Permanecen fuera de este ajuste el reconocimiento del catálogo completo en el asistente, los tipos no postulables, la relectura de bases dentro de la misma etapa y el consumo automático del consorcio/cronograma por el motor de elegibilidad.

## Validación

Suite completa de la combinación: **508 pruebas aprobadas y 4 omitidas**, 64,15 s, con bases temporales y claves desactivadas. Las omitidas requieren PostgreSQL descartable o PowerShell. Se amplía la prueba de migración para comprobar juntas las columnas de ambas ramas y se añade un recorrido que muestra cronograma y elegibilidad en la misma ficha y crea un consorcio desde el módulo conservado.

## Cumplimiento de las actividades

- HU-008: desarrollo del perfil, identidad y acceso desde el perfil implementado; falta aceptación del despliegue conjunto.
- HU-015: gestión privada y conexión al inventario implementadas; falta evidencia conjunta con una convocatoria real y documentos adecuados.
- HU-010: motor y reglas base implementados; el uso automático del consorcio registrado y la fecha extraída del cronograma sigue pendiente, además de la comprobación con convocatorias reales.
- RN-003: no se ha entregado la medición requerida de tiempos, tokens y espacio por arquitectura.
- DO-004: no se ha entregado el manual final del flujo desplegado.

Los resultados locales no certifican un despliegue ni la autenticidad de los PDF o del RUC. RUC/SUNAT sigue siendo un extra sin consulta oficial implementada. No se cambia el estado de las actividades en Notion por esta integración.
