# Integración de los avances de Arturo sobre main — 14/11/2026

## Base y alcance

Se preparó una integración consolidada, con un único commit directamente sobre `9c3e80bae624fe64d3668eca6eb3e577e0067edd` (`Ultimos ajustes`, Benson). Se consultaron otra vez las referencias de GitHub durante la preparación. Publicar el paquete es un paso posterior: esta preparación no cambia las ramas remotas.

La integración conserva el chat flotante HU-014 de Calluchi y los ajustes recientes de Benson sobre fuentes, bases, cronograma, procedimientos y panorama. Los informes anteriores de esta carpeta describen otras revisiones; este documento corresponde a la combinación del 14/11.

## Revisión de ramas

| Rama / avance | Referencia revisada | Resultado en esta integración |
| --- | --- | --- |
| main | `9c3e80b` | Base vigente del equipo. |
| dev | `21d57c5` | Su historia ya está incluida en main. |
| feature/arturo-hu008-perfil | `d442dbf` | La versión inicial ya estaba en main; se conservan además sus mejoras posteriores desde HU-010. |
| feature/arturo-hu015-documentos | `e3e9f76` | Sus commits ya estaban en main aunque el PR siga abierto; no se duplican. |
| fix/arturo-hu008-hu015-revision-20261109 | `2234a3b` | Sus cambios están contenidos en HU-010 y se incorporan con esa revisión. |
| feature/arturo-hu010-elegibilidad-20261111 | `c6ca761` | Se incorporan elegibilidad, garantías orientativas, perfil/producto, documentos y regresiones; se resuelven los cruces con el main posterior. |
| feature/arturo-en009-ci | `34362b9` | Se incorpora el workflow de pytest que faltaba en main. |
| feature/arturo-bootstrap-ocds | `d821206` | Su sustitución antigua del diseño no se aplica sobre la interfaz posterior del equipo. Se recupera el foco del campo inválido; main ya tiene enlace para saltar al contenido y validación/carga equivalentes. El rediseño Bootstrap queda identificado como alternativa antigua, no como diseño integrado. |

Se usa una integración por contenido (squash): las ramas históricas no se reescriben y pueden seguir apareciendo como no fusionadas por ascendencia aunque su contenido esté incorporado. El commit consolidado permite revisar el cambio completo en main.

## Incompatibilidades resueltas

- **Asistente:** no se recupera la pantalla `assistant.html` retirada por main. Se mantiene el widget, su grafo, catálogo completo, cronograma, respuesta JSON, resumen y detalle. La ficha puede abrirlo con la convocatoria seleccionada.
- **Un solo veredicto:** listado, ficha y widget usan `services.eligibility`; el modelo de lenguaje no decide los colores. Se conservan página, texto y motivo de las bases, monto y garantía en el detalle.
- **Perfil y cuentas:** el RUC conserva el carácter opcional de main. Si se proporciona, debe tener once dígitos y no pertenecer a otra cuenta. El chat no sustituye un RUC existente y una experiencia distinta exige actualizar Mi empresa.
- **Historial:** la conversación y su contexto quedan en la base de cuentas, por empresa, en vez de incluir texto privado en la cookie. Reiniciar el chat conserva perfil, documentos y declaraciones de elegibilidad.
- **Buena pro:** se excluye del listado de oportunidades y del asistente. La consulta directa de una ficha adjudicada se conserva como información y su evaluación no puede quedar verde.
- **Vigencia:** se contrasta con la fecha límite; cuando la API no la publica, se usa el día de presentación de ofertas identificado en el cronograma de las bases, sin inventar una hora.
- **Esquema:** se añaden los ítems de las convocatorias y las tablas privadas necesarias conservando datos anteriores. Si una base antigua ya contiene RUC duplicados, no se borran ni se reasignan: se registra el aviso y debe resolverlos el responsable de la base antes de añadir la restricción única. Hasta entonces no se garantiza la exclusión de duplicados ante escrituras simultáneas; los guardados normales siguen comprobándolos.
- **Interfaz:** se combinan los estilos de elegibilidad y controles de contraseña con el diseño actual; se conservan los filtros, gráficos y consorcios de main. La demostración de HU-010 usa el puerto 5050.
- **EN-009:** las pruebas se ejecutarán en GitHub Actions en pushes y PR, sin claves. La ejecución remota solo podrá observarse cuando se publique el commit.

## Verificación

La suite completa terminó con **565 pruebas aprobadas y 4 omitidas**. La entrega incluye el resultado XML y una aplicación del bundle en un checkout aislado. Se usa Python 3.12.14; Render declara 3.12.9 (la misma versión menor). Las pruebas usan SQLite temporal, claves vacías y respuestas de modelos simuladas; no se ha accedido a Neon ni a Gemini.

Hay regresiones que comprueban el mismo color en ficha/listado/widget con Personal vencido, aún no vigente y vigente; el perfil permanece intacto cuando se declara otro importe en el chat. También se verifican formularios, CSRF, aislamiento entre cuentas, migraciones aditivas, consorcios, cronograma, filtros, fuentes y cálculos del panorama. Los cuatro casos omitidos por condiciones de plataforma/disponibilidad siguen identificados en el XML; no se presentan como aprobados.

## Publicación e identidad

El nuevo commit usa la identidad ya existente en el repositorio: **Arturo José Díaz Pretell**, `Arjonil@users.noreply.github.com`. No se utiliza Codex como autor ni se añade una firma de coautor en ese commit. Los commits publicados con otros autores se conservan: no se cambia la historia compartida ni se hace force push.

La preparación crea una rama de respaldo local y una rama con el commit consolidado, ejecuta las pruebas y no publica. El publicador comprueba que sigue siendo el commit validado y que main no avanzó; utiliza un push normal. Si GitHub tiene nuevos cambios, se detiene para actualizar la integración.

Esto valida la integración del código disponible, no cierra por sí solo todos los criterios académicos de HU-010 ni reemplaza la revisión de las bases oficiales. Quedan fuera los acuerdos de la reunión que todavía no se han recibido en audio.

La primera entrega, `da31524`, fue validada en Windows por Arturo con 560 pruebas aprobadas y 6 omitidas. Esta actualización añade tres casos por la corrección de buena pro y conserva un solo commit sobre el mismo main. Para actualizar una copia que ya tenga da31524, el instalador reconoce ese commit y conserva una rama de respaldo antes de cargar la revisión. El paquete debe prepararse de nuevo para validar el nuevo commit en ese equipo.
