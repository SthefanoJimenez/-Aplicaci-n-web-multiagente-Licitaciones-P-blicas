# Mi empresa: reglas del perfil y consulta de RUC

- **RUC:** opcional, único entre cuentas y de 11 dígitos si se indica. Consultarlo completa la razón social y la deja en solo lectura para ese RUC. Si se modifica el número, se puede volver a consultar o completar a mano.
- **Experiencia:** años completos declarados, de 0 a 200. El extremo de 200 es un control técnico, no una regla de contratación. La acreditación aplicable depende de las bases de cada procedimiento.
- **Rubros:** nombres (por ejemplo, `Acero A-36`) o códigos de familia de cuatro dígitos (por ejemplo, `3011`), separados con comas. No use puntos. Los códigos escritos se resuelven después contra el catálogo que usa el proyecto.
- **Importes:** desde cero, hasta 14 cifras enteras y dos decimales opcionales, con punto o coma decimal. No se quitan signos para convertir un importe inválido en otro.
- **Tipo de proveedor, categorías y RNP:** opciones de la versión del equipo. El guardado con RUC consultado conserva todas las casillas válidas; descarta las opciones de categorías y RNP ajenas al catálogo, igual que la versión del equipo.

La validación ocurre tanto en el navegador como en el servidor. El aviso aparece junto al campo y el servidor devuelve errores recuperables. Los datos de prueba no acreditan representación ni elegibilidad.

## Probar el perfil y la consulta sin enviar datos a terceros

Desde la raíz del repositorio, con el entorno de Python del proyecto:

```powershell
python scripts/probar_cambios_local.py app/tests/test_profile_fields.py app/tests/test_hu008.py app/tests/test_ruc_lookup.py
python scripts/demostrar_hu010.py --ruc-demo
```

Abra [Mi empresa](http://127.0.0.1:5050/perfil). La terminal muestra las credenciales ficticias. El RUC ficticio de la demo es `20123456789`. La demo usa bases SQLite temporales y una respuesta simulada, sin el token de MiAPI Cloud.

Para usar la consulta real, configure `MIAPI_RUC_TOKEN` en el entorno del servidor, nunca en el repositorio ni en el navegador. Sin token la app acepta la declaración manual. En la demo puede usar `--ruc-miapi` en lugar de `--ruc-demo` con el token configurado; cada consulta real puede consumir cupo. El límite interno es de cinco intentos diarios por cuenta y un máximo global de 20 por defecto, configurable con `MIAPI_RUC_DAILY_LIMIT`. Ajuste ese máximo al cupo del plan para cualquier despliegue.
