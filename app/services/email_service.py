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