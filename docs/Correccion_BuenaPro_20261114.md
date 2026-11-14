# Buena pro en el listado y en la ficha — 14/11/2026

La integración inicial filtraba buena pro en el asistente, pero el servicio del listado todavía podía devolver esas convocatorias. Además, la evaluación compartida no consideraba su etapa: una ficha directa podía quedar verde si los demás datos eran compatibles.

La revisión conserva el acceso informativo a la ficha, excluye buena pro del servicio de oportunidades y añade una comprobación excluyente en el motor compartido. Así no se presenta una adjudicación como oportunidad para enviar ofertas, incluso si la fuente todavía la marca vigente o no publica un plazo.

También se conserva el cronograma disponible de main cuando una base anterior todavía no tiene la columna nueva de ítems: la ausencia de esa columna no borra la etapa ni el día de presentación de ofertas del contexto de evaluación.

Verificación: tres casos nuevos cubren buena pro con/sin plazo publicado y la compatibilidad anterior a la migración de ítems. Suite completa: **565 aprobadas y 4 omitidas** en Python 3.12.14, con SQLite temporal y claves vacías.

Esta corrección actualiza un commit local de preparación; no reescribe ramas remotas. La primera entrega da31524 y el ajuste de puerto previamente guardado pueden conservarse en los respaldos locales.
