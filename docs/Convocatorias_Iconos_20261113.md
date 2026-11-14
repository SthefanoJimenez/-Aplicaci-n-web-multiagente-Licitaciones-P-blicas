# Convocatorias: tabla original y acciones con iconos

## Motivo

La agrupación de datos en cinco columnas dificultaba reconocer la estructura acordada por el grupo. Se recuperan las nueve columnas originales, en el mismo orden: Código, Objeto y Convocatoria, Entidad compradora, Departamento, Publicación, Antigüedad, Valor estimado, Elegibilidad de mi empresa y Acciones.

## Comportamiento

- Elegibilidad vuelve a tener su propia columna. Sin sesión muestra «Inicie sesión para evaluar»; con sesión conserva el semáforo y su motivo.
- Acciones usa cuatro iconos: ver requisitos, descargar bases, abrir el enlace original del SEACE y copiar datos para buscar en SEACE. Se conservan sus destinos y condiciones de disponibilidad.
- Cada icono tiene un nombre accesible y una explicación visible con el ratón o el foco del teclado. Escape cierra la explicación. La descarga identifica Word/PDF cuando esa información está disponible.
- Los botones miden 44 × 44 píxeles y se distribuyen en dos filas. Los SVG están incluidos en la plantilla: no requieren otra biblioteca.
- En ventanas estrechas se desplaza la tabla horizontalmente dentro de su contenedor, sin ocultar columnas ni convertir las filas en tarjetas. El contenedor también admite foco de teclado.
- Se conservan la navegación corregida, los filtros amplios, la paginación, las rutas existentes y los cambios de RUC y elegibilidad de la revisión anterior.

## Validación

- Suite existente: **453 aprobadas, 4 omitidas**. Las omisiones en Linux corresponden a dos pruebas sin PostgreSQL de prueba y dos sin PowerShell.
- Chromium: diez anchos entre 320 y 1920 píxeles, con sesión y sin sesión (20 comprobaciones). Nueve columnas en todos los tamaños, sin desbordamiento horizontal de la página, sin celdas superpuestas y con objetivos de 44 × 44.
- Tooltips con ratón y Tab, cierre con Escape y ubicación dentro de la ventana, también después de desplazar la tabla.
- Copia al portapapeles comprobada; destinos de ficha, bases y enlace externo conservados.
- Filtros, limpieza y paginación 50 + 3 comprobados con 53 convocatorias ficticias en SQLite temporal. Sin llamadas a Neon ni claves de servicios reales.
- Inspección visual de la tabla, el semáforo y los tooltips en escritorio y móvil.

El ajuste cambia presentación y controles; no actualiza los datos de convocatorias ni agrega pegado automático en la web del SEACE.
