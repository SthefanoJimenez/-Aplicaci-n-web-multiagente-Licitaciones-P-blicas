# HU-010: referencia técnica confundida con personal — 14/11/2026

Las capturas de Arturo muestran en Windows la ficha 1265895,
SIE-SIE-31-2026-MPA-1 de la Municipalidad Provincial de Abancay. La etapa,
cronograma y elegibilidad conviven en la ficha. El amarillo por falta de monto
es coherente con la ausencia de cantidad y CUBSO confirmados.

## Error reproducido

El requisito de la página 34 mostrado en las capturas solicita un certificado de
conformidad referido al Reglamento Técnico del cemento. El detector encontraba
«técnico» dentro de «Reglamento Técnico» y añadía indebidamente documentos de
Personal. Se reprodujo el fallo con tres variantes de esa referencia, incluida
una con salto de línea.

## Corrección acotada

En el detector de personal se excluye la expresión «Reglamento(s) Técnico(s)» y se
delimitan las palabras de los cargos. Se conserva el resto del texto de la página
para seguir detectando requisitos reales de técnicos o personal. No cambia la
extracción del PDF ni el código de consorcios, cronograma o ingesta del equipo.

La corrección no elimina la revisión del certificado como documento de tipo Otro.
Tampoco completa el monto ni convierte automáticamente el resultado en verde.

## Validación

- Antes del ajuste: tres regresiones fallaron y el caso de personal real pasó.
- Después: 60 pruebas específicas de HU-010 y sus regresiones aprobadas.
- Suite completa: **513 aprobadas, 4 omitidas, 51.97 segundos**, en Linux con bases
  temporales y sin claves reales.
- Pendiente: aplicar la corrección en la PC de Arturo y comprobar de nuevo la
  ficha 1265895 en Windows.

## Límites de la evidencia recibida

El cronograma muestra una fecha de publicación del documento y remite al SEACE
para otras fechas; no acredita una fecha límite de ofertas. El perfil de prueba
vende zapatos y la convocatoria corresponde a cemento. Las capturas comprueban
la navegación y la presentación del caso, no una habilitación para postular.
No muestran un recálculo completado con datos confirmados de esa convocatoria.
