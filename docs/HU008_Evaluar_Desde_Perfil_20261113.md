# HU-008: Evaluar mi producto desde el perfil guardado

## Necesidad

HU-008 pide recordar la empresa y evaluarla sin volver a solicitar sus datos. El asistente y la elegibilidad de convocatorias ya recuperaban el perfil; los accesos «Evaluar mi producto» aún abrían una descripción vacía.

## Recorrido implementado

1. Los accesos desde la navegación, la portada, Mi empresa y la ficha informativa de un bien abren `/evaluar/mi-producto`, protegido por inicio de sesión.
2. Si la empresa ya confirmó un bien del catálogo, la entrada recupera ese código y abre directamente su mercado: rango de precios, competencia y demás resultados existentes.
3. Sin un bien confirmado, busca coincidencias utilizando giro y rubros guardados. Presenta hasta tres bienes y exige una selección explícita; no elige automáticamente el primero.
4. Si no puede identificar un bien con el perfil, permite precisar una descripción o actualizar el perfil. Se conserva el recorrido manual de HU-001.
5. Al confirmar se recuerda el código para esa empresa, incluso después de cerrar sesión. Cambiar giro o rubros invalida la elección anterior y exige confirmar de nuevo.
6. «Elegir otro producto de mi empresa» permite sustituir el bien recordado. La elección anterior permanece hasta confirmar la nueva.
7. El costo unitario se solicita al calcular la rentabilidad. Las ventas mensuales del perfil no se interpretan como costo ni como precio de venta.

## Persistencia y privacidad

Se agrega la tabla privada `productos_empresa`, con una fila por empresa: código del catálogo y huella de giro/rubros. La inicialización de cuentas crea la tabla de forma aditiva e idempotente en SQLite/PostgreSQL, sin migrar ni reemplazar los perfiles existentes.

La empresa se resuelve exclusivamente desde el usuario autenticado. Los IDs agregados a URL o formulario no seleccionan otra cuenta. La confirmación conserva la validación de código ofrecido y CSRF.

La búsqueda automática desde el perfil es local y no tiene llamadas al modelo ni decoradores de trazas. El perfil completo, el RUC, la razón social y sus datos financieros no se copian a la cookie. La búsqueda manual conserva su aviso de uso de un modelo; solo se guarda el código confirmado, sin persistir su descripción libre ni su costo.

La búsqueda local necesita términos del giro/rubros presentes en el nombre del bien, con normalización de tildes y plural simple. No garantiza identificar productos ambiguos o sin correspondencia en el catálogo. Por eso se requiere confirmación y existe la alternativa de precisar el bien.

## Validación

- Suite completa: **465 aprobadas, 4 omitidas** por dependencias de entorno (dos sin PostgreSQL de prueba, dos sin PowerShell en Linux).
- Doce pruebas nuevas: acceso privado; uso de giro/rubros; ausencia de datos del perfil en la cookie; confirmación explícita y CSRF; persistencia tras cierre de sesión; aislamiento entre empresas; invalidación por cambio de rubros; sustitución del bien; perfil incompleto; código retirado; fallo de base; coincidencias locales sin modelo.
- Chromium: entrada desde Mi empresa, confirmación, mercado, costo y veredicto, cierre de sesión, segunda cuenta sin perfil, recuperación tras nuevo ingreso, cambio del producto y modificación del perfil.
- Ocho tamaños de ventana entre 320 y 1920 píxeles sin desbordamiento de la pantalla nueva; sin errores JavaScript.
- Pruebas con cuentas ficticias y SQLite temporal, sin Neon ni claves reales. Se conservan los iconos de convocatorias y la barra de navegación corregida.

La entrega conecta la evaluación de producto con el perfil. La consulta del RUC a SUNAT continúa pendiente y no se presenta como implementada.
