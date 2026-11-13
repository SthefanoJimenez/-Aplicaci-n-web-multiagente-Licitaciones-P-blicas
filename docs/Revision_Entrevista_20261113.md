# Revisión posterior a la entrevista — 13 de noviembre de 2026

Rama de trabajo: `feature/arturo-hu010-elegibilidad-20261111`.
Base de esta actualización: `6ca3f49` (interfaz de Convocatorias vigentes).

## HU-008: identidad de la empresa

| Criterio nuevo | Implementación y comprobación |
| --- | --- |
| RUC y razón social en el perfil, validados en navegador y servidor | `Company.ruc` y `Company.name`. RUC de exactamente 11 dígitos ASCII; razón social entre 2 y 200 caracteres. Pruebas HTTP de valores inválidos y comprobación de validez nativa en el navegador. |
| Un RUC no puede asociarse a dos cuentas | Validación dentro de la transacción e índice único de base de datos. Se comprueban el rechazo, la separación de cuentas y la ausencia de guardados parciales. |
| Asistente y evaluación usan la identidad guardada | La identidad proviene de la cuenta autenticada; aparece en el panel, la respuesta del asistente y la ficha. Un RUC recibido en el contexto del chat no sustituye el perfil. |

El RUC se completa en Mi empresa; el registro inicial con correo y contraseña conserva su recorrido.
Validar 11 dígitos no confirma inscripción, estado, condición ni representación en SUNAT. No se incorporó un algoritmo de dígito verificador porque la actividad de Notion lo condiciona a que el equipo lo confirme.

### Migración de cuentas anteriores

`accounts.initialize_schema` crea las tablas que falten y agrega una columna nullable y un índice único a empresas existentes, tanto en SQLite como en PostgreSQL. `create_all` por sí solo no agrega columnas.
La aplicación inicializa ese esquema al abrir la base de cuentas. Se conservan las empresas sin RUC como NULL hasta que el usuario complete el perfil. No se inventan RUC ni se combinan empresas.

La migración se prueba dos veces sobre SQLite con un usuario anterior, su hash y contenido binario privado: el ingreso sigue funcionando y los bytes se conservan. No se ejecutó esta migración en la base compartida de Neon desde este entorno. Para revisión del responsable se incluye `scripts/sql/hu008_ruc_postgresql.sql`, equivalente al cambio automático.

## HU-010: fuentes de los requisitos

- La experiencia mínima se toma de las bases mediante el mismo analizador que usa el motor de elegibilidad. Se retiraron los mínimos de 50000/100000 soles y el supuesto del 50 % del valor que aún existían en el grafo anterior.
- Sin requisitos extraídos, el grafo conserva información pendiente y no inventa páginas ni requisitos por el rubro del título. El RNP se contrasta cuando las bases lo requieren; el consorcio no sustituye el RNP propio exigido.
- Se prueban convocatorias con el mismo requisito de experiencia y distintos valores publicados: variar el valor no cambia el umbral. Cambiar el requisito en las bases sí modifica el resultado.
- El cálculo configurable de garantías conserva sus fuentes y artículos en la ficha; el asistente también cita los artículos 113, 114 y 139 del reglamento cuando muestra el cálculo.

### Dependencia condicional de EN-014

En main y esta rama no hay un módulo de EN-014 ni un conjunto de contratos con RUC del proveedor. Los CSV históricos disponibles identifican al proveedor por nombre y no permiten una vinculación segura por RUC. No se atribuyen adjudicaciones a una empresa solo porque coincida su nombre.

El criterio de Notion dice «Si EN-014 está disponible». El contraste automático con contratos queda condicionado a la entrega e integración de ese módulo por el grupo. Mientras tanto, la experiencia es declarada y debe sustentarse con los documentos de la empresa. Este paquete no presenta un cruce automático como si existiera.

## HU-015: documentos frente a la convocatoria

El motor compartido del listado, la ficha y el asistente consume los documentos de la cuenta y su vigencia a la fecha límite. El recorrido de navegador comprueba que los PDF siguen disponibles después de actualizar RUC y razón social. Las pruebas existentes comprueban también vencimiento, borrado y aislamiento entre empresas.

## Navegación

La marca y la cuenta ocupan la primera fila; las cinco secciones comparten la segunda en cinco espacios iguales. Al reducir la pantalla se usa una distribución de dos columnas y el último enlace ocupa el ancho completo. No se reducen los textos para hacerlos caber ni se ocultan secciones.

Se conserva la propuesta de cinco columnas de Convocatorias. La variante de iconos en Acciones queda para comparación con el grupo; no forma parte de esta revisión.

## Fuentes normativas revisadas

- [DS 001-2026-EF, arts. 113 y 114](https://busquedas.elperuano.pe/dispositivo/NL/2474920-3).
- [Ley 32069 y Reglamento en El Peruano, edición consolidada al 04/02/2026](https://diariooficial.elperuano.pe/Normas/obtenerDocumento?idNorma=25).
- [DS 222-2026-EF, modificación de la implementación del modelo de certificación](https://busquedas.elperuano.pe/dispositivo/EX/2567159-1).

La referencia al artículo 139 corresponde al reglamento, no al artículo 139 de la Ley. No se cambiaron los porcentajes configurados en la entrega de HU-010.

## Verificación de esta revisión

- Suite completa: 453 pruebas aprobadas y 4 omitidas (dos requieren PostgreSQL de prueba y dos PowerShell).
- Navegador: 20 combinaciones de ancho entre 320 y 1920 píxeles, con y sin sesión, sin desbordes, recortes de botones ni errores JavaScript. Se comprueba que las cinco secciones comparten fila a partir de 681 píxeles.
- Recorrido adicional: validación nativa del RUC, guardado de razón social, coincidencia de identidad en perfil/asistente/ficha, artículos de garantías en la respuesta y conservación de los cuatro PDF privados de prueba.
- Respaldo SQLite: copia íntegra de contenido binario sin modificar el original; no crea bases inexistentes ni contacta conexiones remotas.

## Estado de entrega

La actualización se entrega para probar en Windows y revisar con el grupo. El cierre formal requiere aplicar el paquete, comprobar el perfil y una convocatoria con los datos del equipo, revisar la migración de la base de cuentas en el despliegue y después integrar la rama. No se marca una historia como cerrada solo por pasar pruebas con datos sintéticos.
