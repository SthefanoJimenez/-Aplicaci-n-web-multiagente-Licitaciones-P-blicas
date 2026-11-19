"""Entrega por HTTPS o SMTP con TLS. Nunca imprime enlaces ni credenciales."""
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
import logging
import smtplib
import ssl
from threading import BoundedSemaphore
from urllib.parse import urlsplit
import requests

from services import accounts

_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recuperacion")
_slots = BoundedSemaphore(8)


def settings(app):
    import config
    values = {name: app.config.get(name, getattr(config, name)) for name in (
        "PUBLIC_BASE_URL", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "SMTP_FROM", "SMTP_SECURITY", "EMAIL_TRANSPORT", "EMAIL_FROM", "RESEND_API_KEY")}
    try:
        origin = urlsplit(values["PUBLIC_BASE_URL"])
        origin.port  # Valida también los puertos mal formados.
    except ValueError:
        return None
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        return None
    values["SMTP_FROM"] = values["EMAIL_FROM"] or values["SMTP_FROM"]
    if not values["SMTP_FROM"]:
        return None
    if values["EMAIL_TRANSPORT"] == "resend":
        if not values["RESEND_API_KEY"]:
            return None
    elif values["EMAIL_TRANSPORT"] == "smtp":
        if (not values["SMTP_HOST"] or not str(values["SMTP_PORT"]).isdigit()
                or not 1 <= int(values["SMTP_PORT"]) <= 65535
                or values["SMTP_SECURITY"] not in {"starttls", "ssl"}):
            return None
    else:
        return None
    values["PUBLIC_BASE_URL"] = values["PUBLIC_BASE_URL"].rstrip("/")
    return values


def send_email(values, recipient, token=None):
    message = EmailMessage()
    message["Subject"] = "Restablecer su contraseña de LicitaFácil" if token else "Su contraseña de LicitaFácil cambió"
    message["From"] = values["SMTP_FROM"]
    message["To"] = recipient
    if token:
        link = values["PUBLIC_BASE_URL"] + "/restablecer-contrasena#" + token
        body = (
        "Recibimos una solicitud para restablecer su contraseña de LicitaFácil.\n\n"
        "Abra este enlace (vence en 30 minutos y se puede usar una sola vez):\n" + link +
        "\n\nSi no lo solicitó, ignore este correo. Su contraseña no ha cambiado.\n"
        "LicitaFácil nunca le pedirá su contraseña por correo.")
    else:
        body = ("Su contraseña de LicitaFácil se actualizó y se cerraron las sesiones abiertas.\n"
                "Si no realizó el cambio, abra LicitaFácil y solicite un nuevo enlace desde «Olvidé mi contraseña».\n"
                "LicitaFácil nunca le pedirá su contraseña por correo.")
    message.set_content(body)
    if values["EMAIL_TRANSPORT"] == "resend":
        # Endpoint fijo: la clave no se reenvía a redirecciones o hosts configurables.
        response = requests.post("https://api.resend.com/emails",
            headers={"Authorization": "Bearer " + values["RESEND_API_KEY"]},
            json={"from": values["SMTP_FROM"], "to": [recipient],
                  "subject": str(message["Subject"]), "text": body},
            timeout=8, allow_redirects=False)
        if response.status_code != 200 or not response.json().get("id"):
            raise RuntimeError("El proveedor de correo no confirmó la entrega.")
        return
    context = ssl.create_default_context()
    transport = smtplib.SMTP_SSL if values["SMTP_SECURITY"] == "ssl" else smtplib.SMTP
    kwargs = {"timeout": 8}
    if values["SMTP_SECURITY"] == "ssl":
        kwargs["context"] = context
    with transport(values["SMTP_HOST"], int(values["SMTP_PORT"]), **kwargs) as smtp:
        if values["SMTP_SECURITY"] == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if values["SMTP_USERNAME"]:
            smtp.login(values["SMTP_USERNAME"], values["SMTP_PASSWORD"])
        smtp.send_message(message)


def deliver(factory, values, email):
    token = None
    try:
        token = accounts.issue_password_reset(factory, email)
        if token:
            send_email(values, email, token)
    except Exception:
        # No se registran excepciones SMTP: pueden contener destinatarios o secretos.
        logging.getLogger(__name__).error("No se pudo entregar un correo de recuperación.")
        if token:
            try:
                accounts.revoke_password_reset(factory, token)
            except Exception:
                logging.getLogger(__name__).error("No se pudo revocar el enlace no entregado.")


def submit(factory, values, email):
    """Misma cola para correos existentes y ausentes; respuesta HTTP sin esperar al SMTP.

    Cola corta por proceso: un reinicio puede interrumpir la entrega; el usuario
    puede solicitar otro enlace. Los límites y secretos sí persisten en la base.
    """
    if not _slots.acquire(blocking=False):
        return
    try:
        future = _workers.submit(deliver, factory, values, email)
        future.add_done_callback(lambda _: _slots.release())
    except RuntimeError:
        _slots.release()
        logging.getLogger(__name__).error("La cola de recuperación no está disponible.")


def notify_changed(values, email):
    """Notificación sin secretos, con la misma cola acotada de entrega."""
    if not _slots.acquire(blocking=False):
        return
    def deliver_notice():
        try:
            send_email(values, email)
        except Exception:
            logging.getLogger(__name__).error("No se pudo entregar la notificación de cambio.")
        finally:
            _slots.release()
    try:
        _workers.submit(deliver_notice)
    except RuntimeError:
        _slots.release()
