# Cambio y recuperación de contraseña

Las funciones nuevas de contraseña se incluyen desactivadas por defecto. El
ingreso y el registro existentes siguen disponibles, y sus tablas no requieren
las tablas nuevas. Habilite `PASSWORD_MANAGEMENT_ENABLED=1` primero en un
entorno aislado. En el ingreso solo se muestra «Olvidé mi contraseña» si la
configuración de correo también es válida.

Preparación local sobre el código suministrado de `83bbcce`, 17/11/2026. No está publicado en Render.

## Uso

- **Mi empresa → Cambiar contraseña:** solicita la contraseña actual y dos copias de la nueva. Cierra todas las sesiones y exige ingresar de nuevo.
- **Iniciar sesión → Olvidé mi contraseña:** envía un enlace al correo de registro. No se agrega un teléfono ni un contacto de emergencia.
- El enlace vence a los 30 minutos y solo sirve una vez. Solicitar otro enlace no anula los anteriores hasta que se cambia la contraseña.
- Se conserva la política de registro existente: de 4 a 128 caracteres con mayúscula, minúscula, número y símbolo. Las contraseñas anteriores siguen permitiendo ingresar.

## Configuración para Render Free

El archivo render.yaml del proyecto declara el plan Free. Render documenta que ese plan bloquea los puertos SMTP 25, 465 y 587. Por eso se añadió envío por una API HTTPS, sin abrir puertos ni cambiar el plan. La preparación anterior, que solo ofrecía SMTP, no bastaba para ese alojamiento.

```dotenv
PUBLIC_BASE_URL=https://licitafacil-7l4c.onrender.com
EMAIL_TRANSPORT=resend
EMAIL_FROM=LicitaFacil <cuenta-verificada@su-dominio.pe>
RESEND_API_KEY=clave-del-proveedor
```

Se necesita una cuenta de proveedor y un remitente autorizados para el buzón de destino. No se creó una cuenta, no se contrató un plan y no se enviaron correos desde esta preparación. El endpoint fijo es `https://api.resend.com/emails`, con TLS, límite de espera y sin seguir redirecciones. No se incluyen claves en el paquete.

Referencias consultadas el 17/11/2026: https://render.com/docs/free y https://resend.com/docs/api-reference/emails/send-email .

## Configuración SMTP para otros alojamientos

Configurar estas variables en el entorno del servicio. No guardar secretos en Git ni en capturas:

```dotenv
PUBLIC_BASE_URL=https://licitafacil-7l4c.onrender.com
EMAIL_TRANSPORT=smtp
SMTP_HOST=servidor-del-proveedor
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_FROM=LicitaFacil <cuenta-verificada@su-dominio.pe>
SMTP_USERNAME=usuario-del-proveedor
SMTP_PASSWORD=secreto-del-proveedor
```

Con TLS implícito usar `SMTP_SECURITY=ssl` y el puerto indicado por el proveedor, normalmente 465. No existe modo SMTP sin TLS. El origen debe ser HTTPS sin ruta, parámetros ni credenciales. No se construye desde la cabecera Host recibida.

Sin configuración válida, la recuperación informa que no está disponible (503). El ingreso y el cambio autenticado siguen funcionando. Falta verificar la entrega real con el proveedor elegido: los tests simulan tanto la API HTTPS como SMTP. Usar SMTP únicamente en un alojamiento que permita el puerto del proveedor; para Render Free, usar la opción HTTPS anterior.

## Persistencia y seguridad

Solo al habilitar las funciones, la inicialización de cuentas crea dos tablas
de forma aditiva: `recuperaciones_contrasena` y `limites_recuperacion`.
No altera columnas de usuarios, perfiles ni documentos. Usa la misma base y
fábrica de sesiones que las cuentas existentes. En las pruebas previas se
empleó SQLite temporal; valide con PostgreSQL aislado antes de activar el
cambio y la recuperación para las cuentas reales.

Los secretos de recuperación tienen 256 bits aleatorios. Solo se guarda su SHA-256 y una huella del hash de contraseña vigente. La actualización condicional del usuario evita que dos enlaces cambien la contraseña simultáneamente. Un cambio borra los enlaces pendientes y las sesiones de esa cuenta en la misma transacción. No cambia el perfil ni los documentos de otras empresas.

La respuesta a la solicitud es la misma para correos registrados y ausentes. La consulta y el envío se ejecutan en una cola acotada de dos trabajadores y ocho tareas por proceso. Límites persistentes: tres solicitudes por correo y diez por dirección remota en 15 minutos; los intentos de ingreso mantienen su límite anterior. No se confía directamente en X-Forwarded-For: revisar la topología del proxy antes de ajustar la IP del cliente.

Un reinicio puede interrumpir una entrega en cola. En ese caso se debe solicitar otro enlace. La cola es de entrega básica; no equivale a una cola durable con reintentos garantizados. Si el SMTP falla se revoca el enlace generado y se escribe un error sin destinatarios, enlaces ni credenciales. Tras un cambio se intenta enviar una notificación sin contraseña. Su entrega no condiciona el cambio ya confirmado.

El correo transporta el secreto en el fragmento del enlace. JavaScript lo lleva al cuerpo POST y limpia la barra de direcciones. El fragmento no se envía en la petición GET ni aparece en el registro HTTP de acceso. El formulario usa CSRF, `Cache-Control: no-store`, `Referrer-Policy: no-referrer` y restricciones de formulario y de marcos.

## Verificación

```powershell
python -m pytest app/tests/test_password_recovery.py app/tests/test_dates_lima.py app/tests/test_experience_query_codes.py -q
python -m pytest -q
```

Ejecutar en una copia de pruebas sin credenciales de Neon/modelos. El paquete incluye un lanzador de demo temporal y las evidencias de la ejecución local.

Antes de activar en Render: probar el enlace completo en navegador con un buzón
de prueba propio, el correo de aviso, el origen final HTTPS y una base PostgreSQL
aislada. No modificar una contraseña real para demostrar el flujo.

Referencia de diseño: [OWASP Forgot Password Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html), consultada el 17/11/2026. Esta referencia no implica una auditoría externa de seguridad.
