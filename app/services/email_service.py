import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


async def enviar_notificacion_asesor(telefono_cliente: str):
    """Envía email al asesor cuando un cliente solicita atención"""
    settings = get_settings()

    # Link de WhatsApp con mensaje prellenado
    mensaje_prellenado = "¡Hola! Gracias por escribirnos a DECOIARTE.COM, con gusto te atiendo 🏠✨"
    import urllib.parse
    mensaje_encoded = urllib.parse.quote(mensaje_prellenado)
    whatsapp_link = f"https://wa.me/{telefono_cliente}?text={mensaje_encoded}"

    # HTML del email
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
                    padding: 30px; border-radius: 15px; text-align: center; margin-bottom: 20px;">
            <h1 style="color: white; margin: 0; font-size: 28px;">🏠 DECOIARTE.COM</h1>
            <p style="color: rgba(255,255,255,0.9); margin: 10px 0 0 0;">Nueva solicitud de asesoría</p>
        </div>

        <div style="background: #f8f9fa; border-radius: 10px; padding: 25px; margin-bottom: 20px;">
            <h2 style="color: #333; margin-top: 0;">🔔 Nuevo cliente solicita asesor</h2>
            <p style="color: #666; font-size: 16px;">
                Un cliente está esperando tu atención personalizada.
            </p>
            <div style="background: white; border-radius: 8px; padding: 15px; border-left: 4px solid #667eea;">
                <p style="margin: 0; color: #333;">
                    <strong>📱 Número del cliente:</strong><br>
                    <span style="font-size: 20px; color: #667eea;">+{telefono_cliente}</span>
                </p>
            </div>
        </div>

        <div style="text-align: center; margin: 30px 0;">
            <a href="{whatsapp_link}" 
               style="background: #25D366; color: white; padding: 15px 40px; 
                      border-radius: 50px; text-decoration: none; font-size: 18px;
                      font-weight: bold; display: inline-block;">
                💬 Escribirle por WhatsApp
            </a>
        </div>

        <p style="color: #999; font-size: 12px; text-align: center;">
            DECOIARTE.COM — Remodelación con Inteligencia Artificial
        </p>

    </body>
    </html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 Nuevo cliente solicita asesor — +{telefono_cliente}"
        msg["From"] = settings.gmail_user
        msg["To"] = settings.gmail_user

        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_user, settings.gmail_app_password)
            server.sendmail(settings.gmail_user, settings.gmail_user, msg.as_string())

        logger.info(f"Email enviado para cliente {telefono_cliente}")

    except Exception as e:
        logger.error(f"Error enviando email: {e}")

def _enviar_smtp(para: str, asunto: str, html: str) -> None:
    settings = get_settings()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = settings.gmail_user
    msg["To"] = para
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(settings.gmail_user, settings.gmail_app_password)
        server.sendmail(settings.gmail_user, [para], msg.as_string())


async def enviar_correo(para: str, asunto: str, cuerpo_html: str) -> bool:
    """Correo general (pedidos: comprador, tienda y administrador). Nunca rompe
    el flujo: si falla, queda en los registros y el aviso sigue en el panel."""
    import asyncio
    if not para:
        return False
    html = f"""<html><body style="font-family:Segoe UI,Arial,sans-serif;background:#F4F4F7;padding:24px;">
      <div style="max-width:560px;margin:auto;background:#fff;border-radius:14px;padding:24px;color:#1F2937;">
        <div style="font-size:20px;font-weight:800;color:#7C3AED;margin-bottom:12px;">DecoIArte</div>
        {cuerpo_html}
        <p style="font-size:12px;color:#6B7280;margin-top:22px;">Este correo lo envía DecoIArte automáticamente.</p>
      </div></body></html>"""
    try:
        await asyncio.to_thread(_enviar_smtp, para, asunto, html)
        logger.info(f"Correo enviado a {para}: {asunto}")
        return True
    except Exception as e:
        logger.error(f"No se pudo enviar el correo a {para} ({asunto}): {e}")
        return False