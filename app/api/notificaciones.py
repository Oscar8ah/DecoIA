import logging
import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from app.utils.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["notificaciones"])


class RegistroNuevo(BaseModel):
    nombre: str
    email:  str
    plan:   str
    precio: str


@router.post("/notificar-registro")
async def notificar_registro(data: RegistroNuevo):
    """
    Envía notificación WhatsApp a Oscar cuando se registra un nuevo cliente.
    Se llama desde el frontend después de crear la empresa en Supabase.
    """
    settings = get_settings()

    mensaje = (
        f"🎉 *¡Nuevo cliente en DecoIArte!*\n\n"
        f"🏢 *Negocio:* {data.nombre}\n"
        f"📧 *Email:* {data.email}\n"
        f"📦 *Plan:* {data.plan}\n"
        f"💰 *Valor:* {data.precio}\n\n"
        f"👉 Actívalo en: decoiarte.com/admin"
    )

    url = f"https://graph.facebook.com/v25.0/{settings.whatsapp_phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": settings.whatsapp_asesor_number,
        "type": "text",
        "text": {"body": mensaje}
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                logger.info(f"Notificación enviada — nuevo cliente: {data.email}")
            else:
                logger.error(f"Error notificación: {response.status_code} — {response.text[:100]}")
    except Exception as e:
        logger.error(f"Error enviando notificación: {type(e).__name__} — {e}")

    # Siempre retornar ok — no bloquear el registro si falla la notificación
    return {"status": "ok"}