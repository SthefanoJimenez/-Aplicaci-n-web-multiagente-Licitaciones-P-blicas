# Revisión de HU-008 y HU-015 — 9 de noviembre de 2026

Responsable: Arturo José Díaz Pretell. Base revisada: main, 8e3d9f9.

## Cambios preparados

- Registro: de 4 a 128 caracteres, con al menos una mayúscula A-Z, una minúscula a-z, un número 0-9 y un símbolo. El espacio y las letras acentuadas no cuentan como símbolo. La confirmación debe coincidir. La regla se aplica en Flask y en los avisos de JavaScript.
- La contraseña mínima cambia a petición de Arturo para esta entrega académica. Una clave de cuatro caracteres ofrece poca resistencia aunque mezcle tipos; no se presenta como una recomendación de seguridad para producción.
- Las cuentas existentes pueden ingresar con su contraseña actual. No hay cambio masivo de credenciales ni migración de datos.
- Los dos formularios del asistente envían el token CSRF de HU-008. La prueba del flujo ahora representa a un usuario autenticado, y una prueba con cliente normal verifica los tokens realmente renderizados.
- Se actualiza el estado documentado de HU-015 y se distinguen resultados históricos, correcciones preparadas y validación desplegada.

## Comprobación

Suite completa: **321 aprobadas y 4 omitidas**, Linux, 51,89 segundos. Dos omisiones requieren PostgreSQL de prueba y dos requieren PowerShell. Bases temporales, PDF sintéticos y claves externas vacías. Pruebas adicionales: **31 comprobaciones DOM** del registro y sintaxis de JavaScript.

Se comprueban los extremos de longitud, cada tipo de carácter que falta, confirmación distinta, correo inválido, ingreso con contraseñas anteriores, envío del chat y reinicio con token. Las pruebas documentales existentes vuelven a aprobar.

## Qué falta para cerrar las historias

| Actividad | Responsable / dependencia | Estado |
| --- | --- | --- |
| Aplicar esta revisión, probarla en Windows y subir la rama nueva | Arturo | Pendiente en su PC; paquete preparado |
| Validar registro, ingreso, persistencia y documentos con Neon/Render | Arturo con Calluchi (EN-004, despliegue) | Requiere entorno desplegado y acceso de prueba |
| Evaluación que use los documentos y sus fechas | Arturo (HU-015 / HU-010) con Calluchi (HU-014) y Sthefano (HU-009) | Criterio 3 pendiente; HU-010 queda fuera de esta revisión por indicación de Arturo |
| RUC/MiAPI | Responsable de implementación por acordar; propuesta: Arturo para perfil | Confirmar alcance RUC frente a DNI y obtener token/cupo; aún no implementado |
| PDF/DOCX | Grupo, según se trate de bases, subida o exportación | Alcance sin confirmar; HU-015 sigue admitiendo PDF |
| Revisión y aprobación del cambio compartido | Grupo | No se fusiona main desde el paquete |

Las capturas de dos listados distintos prueban separación visual. La autorización de URLs ajenas se comprueba además en test_hu015.py. Las capturas de carpetas son complementarias y no reemplazan esas pruebas.
